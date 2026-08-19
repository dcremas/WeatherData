"""Download a year of GHCNh station files and build the combined parquet in S3.

The AWS counterpart of the repo's ghcnh_generate.py, and the GHCNh equivalent of
awkApiCallS3 in the apple_weatherkit pipeline: it is the extract half of the ELT,
it writes to a staging prefix, and it promotes to the current prefix only once
the result has been checked. ghcnhPostgresqlUpdate reads what this leaves behind.

This function deliberately runs OUTSIDE the VPC. noaa-ghcnh-pds lives in
us-east-1, the VPC's only S3 route is a us-east-2 gateway endpoint, and there is
no NAT gateway - so a VPC-attached copy of this function could not reach NOAA at
all. Its sibling ghcnhPostgresqlUpdate is inside the VPC for the opposite
reason: it needs the private address of the EC2 Postgres. That split mirrors
awkApiCallS3 (no VPC) / awkPostgresqlUpdate (VPC).
"""
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.append('/opt/python')

import shared_funcs

BUCKET_URL = "https://noaa-ghcnh-pds.s3.amazonaws.com/hourly/access/by-year"

S3_BUCKET_NAME = os.environ.get('GHCNH_BUCKET', 'noaa-ghcnh-weatherdata')
S3_METADATA_KEY = os.environ.get('GHCNH_STATIONS_KEY', 'metadata/stations.csv')
S3_STAGING_PREFIX = 'ghcnh_parquet_staging/'
S3_CURRENT_PREFIX = 'ghcnh_parquet/'
S3_RAW_PREFIX = 'ghcnh_files/'

# Keep the raw per-station files as well as the combined parquet. Off by default:
# it is 118 MB and 112 extra PUTs per year per run, and NOAA remains the durable
# source, so the combined parquet is the only artefact the loader needs.
KEEP_RAW_FILES = os.environ.get('GHCNH_KEEP_RAW', '').lower() in ('1', 'true', 'yes')

# 112 simultaneous urllib threads is fine on a laptop but pointless in Lambda,
# where the network share scales with memory rather than thread count, and each
# in-flight response is held in memory. A bounded pool keeps the peak flat.
DOWNLOAD_WORKERS = int(os.environ.get('GHCNH_DOWNLOAD_WORKERS', '16'))

# A healthy year of 112 stations is several million raw rows even in January, by
# which point each station already has a few thousand. Anything below this means
# a truncated or wrong-year download, and must not be promoted over a good file.
MIN_RAW_ROWS = int(os.environ.get('GHCNH_MIN_RAW_ROWS', '100000'))

# Only these columns are needed out of the 329 GHCNh publishes; identical to
# ghcnh_generate.py, and the loader's transform reads exactly this set. The
# _Report_Type and _Source_Code columns are here because GHCNh records provenance
# per variable rather than per row.
MEASURE_COLS = [
    'temperature', 'dew_point_temperature', 'sea_level_pressure',
    'wind_speed', 'visibility', 'ceiling_height',
    'precipitation', 'precipitation_3_hour', 'precipitation_6_hour',
    'precipitation_12_hour', 'precipitation_24_hour',
]
QUALITY_CODE_COLS = [f'{name}_Quality_Code' for name in MEASURE_COLS]
REPORT_TYPE_COLS = [
    'temperature_Report_Type', 'sea_level_pressure_Report_Type',
    'wind_speed_Report_Type', 'visibility_Report_Type',
]
SOURCE_COLS = [
    'temperature_Source_Code', 'sea_level_pressure_Source_Code',
    'wind_speed_Source_Code',
]
SELECT_COLS = (['STATION', 'DATE'] + MEASURE_COLS + QUALITY_CODE_COLS
               + REPORT_TYPE_COLS + SOURCE_COLS)


def station_roster_from_s3(s3_client):
    """Read the 112-station ISD<->GHCNh roster out of S3.

    Same file and same column order as the repo's metadata/stations.csv, which
    deploy/sync_metadata.sh uploads, so the roster has one definition.
    """
    metadata_object = s3_client.get_object(
        Bucket=S3_BUCKET_NAME, Key=S3_METADATA_KEY
    )
    lines = metadata_object['Body'].read().decode('utf-8').splitlines()
    reader = csv.reader(lines)
    next(reader)
    return [(row[0], row[1], row[2], row[3]) for row in reader]


def download_station(year_no, isd, ghcnh):
    """Fetch one station-year parquet, returning its bytes.

    Returns None when GHCNh has no file for this station-year, which is not an
    error: a station commissioned mid-series simply has no early years, and in
    the first days of January the new year's files appear station by station.
    """
    url = f"{BUCKET_URL}/{year_no}/parquet/GHCNh_{ghcnh}_{year_no}.parquet"
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    except (urllib.error.URLError, OSError) as err:
        raise RuntimeError(f"{isd} / {ghcnh} ({year_no}): {err}") from err


def unified_schema(select_cols):
    """The schema every station table is cast to before it is written.

    ghcnh_generate.py leans on pa.concat_tables(promote_options='permissive') to
    reconcile stations whose files differ in which optional columns are present.
    Writing incrementally instead - one station at a time, so a year never sits
    in memory whole - means the schema has to be fixed up front rather than
    negotiated, because ParquetWriter will not accept a changing one.

    STATION, DATE and every provenance/quality column are strings; every
    measurement is a double. isd_station is appended last, matching the column
    order ghcnh_generate.py produces.
    """
    fields = []
    for name in select_cols:
        if name in MEASURE_COLS:
            fields.append(pa.field(name, pa.float64()))
        else:
            fields.append(pa.field(name, pa.string()))
    fields.append(pa.field('isd_station', pa.string()))
    return pa.schema(fields)


def conform(table, schema, isd):
    """Project one station's table onto the unified schema and tag it."""
    arrays = []
    for field in schema:
        if field.name == 'isd_station':
            arrays.append(pa.array([isd] * table.num_rows, type=pa.string()))
        elif field.name in table.column_names:
            arrays.append(table.column(field.name).cast(field.type))
        else:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def read_manifest(s3_client, year_no):
    """What the currently promoted combined parquet for year_no contains.

    A small JSON sidecar written next to each promoted file. It exists so the
    promote guard can compare a fresh download against the last good one without
    downloading and parsing 40 MB of parquet to find out.

    Returns None when nothing has been promoted yet, which is the first run.
    """
    key = f"{S3_CURRENT_PREFIX}{year_no}/manifest.json"
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
    except s3_client.exceptions.NoSuchKey:
        return None
    return json.loads(response['Body'].read().decode('utf-8'))


def build_year(year_no, roster, s3_client):
    """Download every station for year_no and write the combined parquet.

    Returns (rows_written, stations_written, missing_stations).
    """
    schema = unified_schema(SELECT_COLS)
    local_path = f"/tmp/data_{year_no}.parquet"

    scrape_start_time = time.perf_counter()
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        payloads = list(pool.map(
            lambda entry: download_station(year_no, entry[0], entry[1]), roster
        ))
    total_scrape_time = time.perf_counter() - scrape_start_time

    combine_start_time = time.perf_counter()
    rows_written = 0
    stations_written = 0
    missing = []

    max_date = None

    writer = pq.ParquetWriter(local_path, schema)
    try:
        for (isd, ghcnh, _name, _state), payload in zip(roster, payloads):
            if payload is None:
                missing.append(f"{isd} / {ghcnh}")
                continue
            table = pq.read_table(io.BytesIO(payload), columns=SELECT_COLS)
            table = conform(table, schema, isd)
            writer.write_table(table)
            rows_written += table.num_rows
            stations_written += 1

            # How far this year's coverage actually reaches. GHCNh writes DATE
            # as an ISO string, so max() over the column is the newest
            # observation without parsing anything.
            if table.num_rows:
                station_max = pc.max(table.column('DATE')).as_py()
                if station_max and (max_date is None or station_max > max_date):
                    max_date = station_max

            if KEEP_RAW_FILES:
                s3_client.put_object(
                    Body=payload,
                    Bucket=S3_BUCKET_NAME,
                    Key=f"{S3_RAW_PREFIX}{year_no}/GHCNh_{ghcnh}_{year_no}.parquet",
                )
    finally:
        writer.close()

    total_combine_time = time.perf_counter() - combine_start_time

    print(f"{year_no}: downloaded {stations_written} of {len(roster)} stations "
          f"in {total_scrape_time:.2f}s, combined {rows_written:,} raw rows in "
          f"{total_combine_time:.2f}s, newest observation {max_date}.")
    if missing:
        print(f"{year_no}: no GHCNh file for {len(missing)} station(s): "
              f"{missing[:5]}")

    return local_path, rows_written, stations_written, missing, max_date


def lambda_handler(event, context):
    s3_client = boto3.client('s3')
    roster = station_roster_from_s3(s3_client)

    # Which years the loader will need. Normally one; two across a New Year,
    # when the previous month lives in the previous year's file. An explicit
    # "years" in the event overrides, for backfills and manual reruns.
    if event and event.get('years'):
        years = sorted({int(year) for year in event['years']})
    else:
        periods = shared_funcs.load_window(months_back=1)
        years = sorted({year for year, _month in periods})

    # Deliberate override for the coverage-regression guard below, for the case
    # where NOAA has genuinely withdrawn data and you want the shorter file
    # anyway. Never set on the scheduled invocation.
    allow_regression = bool(event and event.get('allow_regression'))

    print(f"Building GHCNh year(s) {years} into s3://{S3_BUCKET_NAME}/")

    summary = []

    for year_no in years:
        local_path, rows, stations, missing, max_date = build_year(
            year_no, roster, s3_client
        )

        # Checked BEFORE the promote, so a short or failed download can never
        # replace a good combined parquet that the loader would then read. This
        # is the same "refuse rather than write bad data" stance the repo's
        # loaders take, moved one step earlier in the chain.
        if rows < MIN_RAW_ROWS:
            raise RuntimeError(
                f"{year_no}: only {rows:,} raw rows from {stations} station(s), "
                f"expected at least {MIN_RAW_ROWS:,} - refusing to promote over "
                f"the current file. The download looks truncated."
            )
        if stations < len(roster) // 2:
            raise RuntimeError(
                f"{year_no}: only {stations} of {len(roster)} stations returned "
                f"a file - refusing to promote a half-empty year."
            )

        # Coverage must not go BACKWARDS. GHCNh republishes the in-progress year
        # in place, and on 2026-08-19 it republished 2026 ending at 2026-07-15
        # having ended at 2026-08-16 the day before - a month of observations
        # withdrawn upstream. Row count and station count both still looked
        # healthy (5.06M rows, 112 of 112), so only the newest date catches it.
        #
        # Refusing here leaves the previous good parquet promoted, so the loader
        # reloads the same window it loaded yesterday and the warehouse holds
        # its ground until NOAA republishes. Without this the loader is the last
        # line of defence, and it only holds because its row-count floor happens
        # to sit above the truncated total.
        previous = read_manifest(s3_client, year_no)
        regressed = (
            previous is not None
            and previous.get('max_date')
            and max_date
            and max_date < previous['max_date']
        )
        if regressed and not allow_regression:
            raise RuntimeError(
                f"{year_no}: NOAA's current files end at {max_date}, but the "
                f"promoted copy already reaches {previous['max_date']} - coverage "
                f"has gone backwards upstream. Refusing to promote; the existing "
                f"parquet stays in place and the loader will reload the same "
                f"window it loaded yesterday. Re-run once NOAA has republished, "
                f'or invoke with {{"years": [{year_no}], "allow_regression": true}} '
                f"to override deliberately."
            )
        if regressed:
            print(f"{year_no}: WARNING - coverage regressed from "
                  f"{previous['max_date']} to {max_date}, promoting anyway "
                  f"because allow_regression was set.")

        staging_key = f"{S3_STAGING_PREFIX}{year_no}/data.parquet"
        current_key = f"{S3_CURRENT_PREFIX}{year_no}/data.parquet"
        manifest_key = f"{S3_CURRENT_PREFIX}{year_no}/manifest.json"

        s3_client.upload_file(local_path, S3_BUCKET_NAME, staging_key)
        s3_client.copy_object(
            Bucket=S3_BUCKET_NAME,
            CopySource=f"{S3_BUCKET_NAME}/{staging_key}",
            Key=current_key,
        )
        size_mb = os.path.getsize(local_path) / 1024 / 1024
        os.remove(local_path)

        entry = {
            'year': year_no,
            'raw_rows': rows,
            'stations': stations,
            'missing_stations': len(missing),
            'max_date': max_date,
            'size_mb': round(size_mb, 1),
            'key': current_key,
        }

        # Written only after the promote succeeds, so the manifest always
        # describes the file that is actually current.
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME, Key=manifest_key,
            Body=json.dumps(entry, indent=2).encode('utf-8'),
            ContentType='application/json',
        )

        print(f"{year_no}: promoted {size_mb:.1f} MB to "
              f"s3://{S3_BUCKET_NAME}/{current_key} (through {max_date})")

        summary.append(entry)

    return {'years': summary}
