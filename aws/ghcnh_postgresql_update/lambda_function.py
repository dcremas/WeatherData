"""Transform the combined GHCNh parquet from S3 and load it into Postgres.

The AWS counterpart of the repo's ghcnh_process.py, and the GHCNh equivalent of
awkPostgresqlUpdate: the transform-and-load half of the ELT, run about fifteen
minutes after ghcnhDownloadS3 has promoted a fresh combined parquet.

Runs INSIDE the VPC, because the warehouse is the self-hosted Postgres 16 on
lambda-playground-1 at its private address. S3 is reached over the us-east-2
gateway endpoint, so no NAT gateway is needed - and that is also why its sibling
ghcnhDownloadS3 sits outside the VPC, where it can still reach NOAA's us-east-1
bucket.

Every guard the repo's loader grew is kept, because the failure they prevent is
worse when nothing is watching:

  * the row-count floor is checked BEFORE the DELETE, so a truncated source
    cannot empty a good slice and then fail on the insert
  * the DELETE and the COPY commit as ONE transaction, so the bokeh apps never
    observe the window half-loaded
  * the load is rejected if the newest observation is stale, which is what an
    upstream that has stopped publishing looks like
"""
import io
import os
import sys
import time

import boto3
from sqlalchemy import create_engine
from sqlalchemy.sql import text

sys.path.append('/opt/python')

import shared_funcs

S3_BUCKET_NAME = os.environ.get('GHCNH_BUCKET', 'noaa-ghcnh-weatherdata')
S3_CURRENT_PREFIX = 'ghcnh_parquet/'

# A two-month window is 150-200k rows across 112 stations. The floor is set well
# below that but far above anything a broken source would produce.
MIN_WINDOW_ROWS = int(os.environ.get('GHCNH_MIN_WINDOW_ROWS', '60000'))
MAX_LAG_DAYS = int(os.environ.get('GHCNH_MAX_LAG_DAYS', '3'))

SQL_DIR = os.path.join(os.path.dirname(__file__), 'sql')
ANALYTICS_SQL = 'analytics_slp_decrease.sql'
LOC_SUBSET_SQL = 'extend_loc_subset.sql'


def database_url():
    """Build the SQLAlchemy URL, reading the warehouse password from S3.

    Host, port, database and user are plain Lambda environment variables, as
    they are on awkPostgresqlUpdate. The password is not: it is a small
    server-side-encrypted object in the pipeline's own bucket, fetched at cold
    start.

    Two more obvious homes for it were tried and rejected:

      SSM Parameter Store. The natural choice, and it cannot be reached from
      this VPC. There is no NAT gateway, and the only endpoint is the S3
      *gateway* endpoint - talking to ssm.us-east-2.amazonaws.com needs an
      *interface* endpoint, billed hourly per availability zone, about
      $21/month across these three subnets to hold one 32-character string. The
      first version of this function hung there until it timed out.

      A Lambda environment variable, which is what awkPostgresqlUpdate does.
      Free and it works, but the value is then readable by anyone who can call
      GetFunctionConfiguration, and it shows in the console's environment pane.

    S3 avoids both problems at no cost: the gateway endpoint is already in place
    and free, the bucket has default encryption and blocks all public access, and
    GetObject on it is granted only to this function's role. The SSM parameter
    remains the canonical copy - deploy/02_deploy_functions.sh syncs it here - so
    rotation is still a single edit in one place.
    """
    dialect = os.environ.get('PG_GHCNH_DIALECT', 'postgresql')
    user = os.environ['PG_GHCNH_USERNAME']
    host = os.environ['PG_GHCNH_HOST']
    port = os.environ.get('PG_GHCNH_PORT', '5432')
    database = os.environ['PG_GHCNH_DB']

    secret_key = os.environ.get('PG_GHCNH_PASSWORD_S3_KEY')
    if secret_key:
        response = boto3.client('s3').get_object(
            Bucket=S3_BUCKET_NAME, Key=secret_key
        )
        password = response['Body'].read().decode('utf-8').strip()
    else:
        # Local runs of this file, where the bucket is not the point.
        password = os.environ['PG_GHCNH_PASSWORD']

    from urllib.parse import quote_plus
    return (f"{dialect}://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{database}")


def read_year_parquet(s3_client, year_no):
    """Pull one year's combined parquet into memory as a seekable buffer.

    Parquet is read back-to-front - the footer carries the schema and the row
    group offsets - so the streaming body boto3 returns cannot be read directly.
    The file is about 35 MB, so buffering it is cheaper and simpler than staging
    it on /tmp.
    """
    key = f"{S3_CURRENT_PREFIX}{year_no}/data.parquet"
    response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
    payload = response['Body'].read()
    print(f"read s3://{S3_BUCKET_NAME}/{key} ({len(payload) / 1024 / 1024:.1f} MB)")
    return io.BytesIO(payload)


def run_sql_file(connection, filename):
    """Execute one of the repo's SQL scripts against an open connection.

    The scripts carry their own BEGIN/COMMIT so they are safe to pipe into psql
    by hand. Here the caller already holds a transaction, and a nested BEGIN
    would be a warning at best and a premature COMMIT at worst - so the outer
    pair is stripped and the caller's transaction governs. The files stay
    byte-identical to the ones bash_scripts/run_psql.sh runs, so there is still
    exactly one copy of this SQL.
    """
    with open(os.path.join(SQL_DIR, filename)) as read_file:
        script = read_file.read()

    statements = [
        line for line in script.splitlines()
        if line.strip().upper() not in ('BEGIN;', 'COMMIT;')
    ]
    connection.exec_driver_sql('\n'.join(statements))


def lambda_handler(event, context):
    # An explicit window in the event overrides, for backfills and reruns:
    #   {"periods": [[2026, 6], [2026, 7]]}
    if event and event.get('periods'):
        periods = [(int(year), int(month)) for year, month in event['periods']]
    else:
        periods = shared_funcs.load_window(months_back=1)

    start, end = shared_funcs.window_bounds(periods)
    label = f"{periods[0][0]}-{periods[0][1]:02d} to {periods[-1][0]}-{periods[-1][1]:02d}"
    print(f"Rebuilding observations for {periods} "
          f"[{start:%Y-%m-%d}, {end:%Y-%m-%d})")

    s3_client = boto3.client('s3')

    # Group the window's periods by the year whose parquet holds them, so a
    # January run reads December out of last year's file and January out of this
    # year's rather than looking for both in one.
    by_year = {}
    for year_no, month_no in periods:
        by_year.setdefault(year_no, []).append((year_no, month_no))

    transform_start_time = time.perf_counter()

    row_total = 0
    data_clean = []
    for year_no in sorted(by_year):
        buffer = read_year_parquet(s3_client, year_no)
        rows_read, records = shared_funcs.ghcnh_transform(
            buffer, periods=by_year[year_no]
        )
        row_total += rows_read
        data_clean.extend(records)
        print(f"{year_no}: read {rows_read:,} raw rows, kept {len(records):,}")

    total_transform_time = time.perf_counter() - transform_start_time

    # Before the DELETE, deliberately: an empty or short batch must never be
    # allowed to empty a good window and only then fail.
    shared_funcs.require_rows(data_clean, min_rows=MIN_WINDOW_ROWS, label=label)

    engine = create_engine(url=database_url(), pool_size=5, pool_recycle=3600)

    swap_start_time = time.perf_counter()

    # A half-open range on `date` rather than EXTRACT(year ...)/EXTRACT(month ...),
    # so idx_observations_date can be used for the delete.
    delete_query = "DELETE FROM observations WHERE date >= :start AND date < :end"

    with engine.begin() as connection:
        result = connection.execute(
            text(delete_query), {"start": start, "end": end}
        )
        deleted = result.rowcount

        inserted = shared_funcs.copy_observations(
            connection, data_clean, min_rows=MIN_WINDOW_ROWS, label=label,
        )

    total_swap_time = time.perf_counter() - swap_start_time

    row_count, min_date, max_date = shared_funcs.verify_window_freshness(
        engine, start, end, max_lag_days=MAX_LAG_DAYS,
    )

    # Downstream rebuilds. obs_baro_impact is what the scatter_baro_impact bokeh
    # app reads, and loc_subset is the station x year scaffold box_plots and
    # geo_views drive their final SELECT from; a year that is missing from it
    # cannot be plotted no matter what the app asks for. Both are done after the
    # freshness check, so a stale load does not get published downstream.
    analytics_start_time = time.perf_counter()
    with engine.begin() as connection:
        run_sql_file(connection, ANALYTICS_SQL)
        run_sql_file(connection, LOC_SUBSET_SQL)
    total_analytics_time = time.perf_counter() - analytics_start_time

    message = (
        f"{label}: read {row_total:,} raw GHCNh rows, kept {len(data_clean):,}; "
        f"deleted {deleted:,} and inserted {inserted:,} in one transaction. "
        f"Window now holds {row_count:,} rows, {min_date} through {max_date}. "
        f"transform {total_transform_time:.1f}s, swap {total_swap_time:.1f}s, "
        f"analytics {total_analytics_time:.1f}s."
    )
    print(message)

    return {
        'periods': periods,
        'raw_rows': row_total,
        'loaded_rows': inserted,
        'deleted_rows': deleted,
        'window_rows': row_count,
        'max_date': str(max_date),
    }
