"""Transform the combined GHCNh parquet into observations rows and load them.

The GHCNh replacement for the retired ISD loaders. Run
ghcnh_generate.py first to produce ghcnh_parquet/<year_no>/data.parquet.

Set month_no to load a single month (the normal monthly cadence), or leave it as
None to reload the whole of year_no. Either way the target slice is deleted and
rebuilt, matching how the ISD loaders behaved.

Three differences from the ISD path are handled here, and each one silently
corrupts the load if missed:

  1. Report types lost their hyphen - GHCNh writes FM15/FM12 where ISD wrote
     FM-15/FM-12. They are normalised back to the hyphenated form so the existing
     analytics SQL (which filters report_type IN ('FM-15')) keeps working.
  2. GHCNh records report_type and source per variable, not per row, so a single
     row-level value is derived from the variables in priority order.
  3. GHCNh publishes real units, not ISD's tenths, and visibility switched from
     metres to kilometres - hence the ghcnh_* converters in shared_funcs.
"""
import os
import sys
import time
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from dotenv import load_dotenv
import shared_funcs

load_dotenv()

# Defaults below; all three are overridable from the command line so the backfill
# can loop without editing this file:
#   python ghcnh_process.py                    -> whole of 2026, remote
#   python ghcnh_process.py 2025               -> whole of 2025, remote
#   python ghcnh_process.py 2025 9             -> September 2025 only, remote
#   python ghcnh_process.py 2025 9 local       -> September 2025 only, local
year_no = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
# '-', 'all' or an empty string mean the whole year, so a target can be given
# positionally without also naming a month.
_month_arg = sys.argv[2] if len(sys.argv) > 2 else ''
month_no = int(_month_arg) if _month_arg not in ('', '-', 'all') else None
target = sys.argv[3] if len(sys.argv) > 3 else 'remote'

MIN_MONTH_ROWS = 60_000
MIN_YEAR_ROWS = 600_000

parquet_path = f"ghcnh_parquet/{year_no}/data.parquet"
if not os.path.exists(parquet_path):
    raise RuntimeError(
        f"{parquet_path} not found - run ghcnh_generate.py for {year_no} first."
    )

if target == 'remote':
    url = os.getenv('url_ext_aws')
else:
    url = shared_funcs.database_path('weatherdata')
engine = create_engine(url=url, pool_size=5, pool_recycle=3600)

label = f"{year_no}" if month_no is None else f"{year_no}-{month_no:02d}"

# Transforming the GHCNh rows into the observations schema.
transform_start_time = time.perf_counter()

row_total, data_clean = shared_funcs.ghcnh_transform(
    parquet_path, year_no, month_no,
)

transform_stop_time = time.perf_counter()
total_transform_time = transform_stop_time - transform_start_time

# Checked before the DELETE, so a short or stale source can never empty a good
# slice of the warehouse and then fail on the insert.
min_rows = MIN_MONTH_ROWS if month_no is not None else MIN_YEAR_ROWS
shared_funcs.require_rows(data_clean, min_rows=min_rows, label=label)

# Deleting and rebuilding the slice in ONE transaction.
#
# The ISD loaders committed the delete on its own and only then began inserting,
# which left the slice visibly empty for the whole load - harmless when that was
# seconds, but a 40-minute window when the insert was slow, and any reader during
# it saw a hole rather than either the old or the new data. Committing both
# together means the swap is atomic and a failed load leaves the previous data
# untouched.
swap_start_time = time.perf_counter()

if month_no is None:
    delete_query = (
        "DELETE FROM observations WHERE EXTRACT(year from date) = :yr"
    )
    params = {"yr": year_no}
else:
    delete_query = (
        "DELETE FROM observations WHERE EXTRACT(year from date) = :yr "
        "AND EXTRACT(month from date) = :mo"
    )
    params = {"yr": year_no, "mo": month_no}

with engine.begin() as connection:
    statement = text(delete_query)
    result = connection.execute(statement, params)
    deleted = result.rowcount

    inserted = shared_funcs.copy_observations(
        connection, data_clean, min_rows=min_rows, label=label,
    )

swap_stop_time = time.perf_counter()
total_swap_time = swap_stop_time - swap_start_time

if month_no is not None:
    row_count, min_date, max_date = shared_funcs.verify_month_coverage(
        engine, year_no, month_no,
    )
    print(f"Month now holds {row_count:,} rows, {min_date} through {max_date}.")

print(f"Read {row_total:,} raw GHCNh rows, kept {len(data_clean):,} for {label}.")
print(f"Deleted {deleted:,} existing rows, inserted {inserted:,}, "
      f"committed together.")
print(f"The total time for the Transform is: {total_transform_time:.2f} seconds.")
print(f"The total time for the Swap is: {total_swap_time:.2f} seconds.")
