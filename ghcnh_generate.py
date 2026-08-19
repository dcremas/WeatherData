"""Download a year of GHCNh station files and build the combined parquet.

The GHCNh replacement for the retired ISD downloader. NOAA superseded ISD with no
data published after 2025-08-24, so new observations come from GHCNh on the NOAA
Open Data bucket instead of the retired ncei.noaa.gov global-hourly directory.

GHCNh rewrites the whole in-progress year in place, so like the ISD script this
re-downloads every station file for year_no rather than trying to fetch a delta.

Run this first, then ghcnh_process.py.
"""
import os
import sys
import time
import threading
import urllib.error
import urllib.request
import pyarrow as pa
import pyarrow.parquet as pq
import shared_funcs

# Defaults to the current year; pass a year to fetch a different one, so the
# backfill can loop without editing this file:  python ghcnh_generate.py 2025
year_no = int(sys.argv[1]) if len(sys.argv) > 1 else 2026

BUCKET_URL = "https://noaa-ghcnh-pds.s3.amazonaws.com/hourly/access/by-year"
raw_path = f"ghcnh_files/{year_no}"
combined_path = f"ghcnh_parquet/{year_no}"

# Only these columns are needed out of the 329 GHCNh publishes. The four
# _Report_Type and three _Source_Code columns are read because GHCNh records
# provenance per variable rather than per row; ghcnh_process.py collapses them
# back to the single report_type/source the observations table expects.
MEASURE_COLS = [
    'temperature', 'dew_point_temperature', 'sea_level_pressure',
    'wind_speed', 'visibility', 'ceiling_height',
    # GHCNh splits precipitation across accumulation periods, where ISD packed
    # whatever depth was reported into the single AA1 field. All the period
    # columns are needed so ghcnh_transform can coalesce them back into one
    # depth; reading only 'precipitation' loses roughly 640 readings per station
    # per year to synoptic-hour multi-hour totals.
    'precipitation', 'precipitation_3_hour', 'precipitation_6_hour',
    'precipitation_12_hour', 'precipitation_24_hour',
]
# GHCNh grades every reading; without these the erroneous ones flow straight
# through. Real examples found in 2025/2026: a 219 C temperature (QC 7) and a
# 465 m/s wind (QC 3), which became 426 F and 1,041 mph in the warehouse.
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

os.makedirs(raw_path, exist_ok=True)
os.makedirs(combined_path, exist_ok=True)

roster = shared_funcs.station_roster()
failures = []
failures_lock = threading.Lock()


def download_file(url, filename, station_label):
    try:
        urllib.request.urlretrieve(url, filename)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as err:
        # Collect rather than raise: a thread that dies here would otherwise be
        # invisible, and a partial download must not look like a complete year.
        with failures_lock:
            failures.append((station_label, str(err)))


files_to_download = [
    {"url": f"{BUCKET_URL}/{year_no}/parquet/GHCNh_{ghcnh}_{year_no}.parquet",
     "filename": f"{raw_path}/GHCNh_{ghcnh}_{year_no}.parquet",
     "station_label": f"{isd} / {ghcnh}"}
    for isd, ghcnh, _name, _state in roster
]

threads = []

scrape_start_time = time.perf_counter()
for file_info in files_to_download:
    thread = threading.Thread(
        target=download_file,
        args=(file_info["url"], file_info["filename"], file_info["station_label"])
    )
    thread.start()
    threads.append(thread)

for thread in threads:
    thread.join()

scrape_stop_time = time.perf_counter()
total_scrape_time = scrape_stop_time - scrape_start_time

if failures:
    raise RuntimeError(
        f"{len(failures)} of {len(files_to_download)} station downloads failed; "
        f"refusing to build a partial year. First few: {failures[:5]}"
    )

# Combining into one parquet, tagged with the ISD station id.
#
# The ISD scripts read their parquet through PySpark and then .collect() the
# whole frame back to the driver, which pays for a JVM and a full round-trip
# without using any distributed execution. Reading with pyarrow directly does
# the same work in-process, and lets each 329-column file be projected down to
# the columns above as it is read.
combine_start_time = time.perf_counter()

tables = []
for isd, ghcnh, _name, _state in roster:
    file_path = f"{raw_path}/GHCNh_{ghcnh}_{year_no}.parquet"
    table = pq.read_table(file_path, columns=SELECT_COLS)

    # Carry the ISD id through as isd_station: locations, regions,
    # obs_baro_impact and the bokeh apps all join observations on the 11-digit
    # ISD station, so the warehouse must keep using it as the key.
    table = table.append_column(
        'isd_station', pa.array([isd] * table.num_rows, type=pa.string())
    )
    tables.append(table)

combined = pa.concat_tables(tables, promote_options='permissive')
pq.write_table(combined, f"{combined_path}/data.parquet")

combine_stop_time = time.perf_counter()
total_combine_time = combine_stop_time - combine_start_time

print(f"Downloaded {len(files_to_download)} station files for {year_no}.")
print(f"Combined parquet holds {combined.num_rows:,} raw rows "
      f"across {len(tables)} stations.")
print(f"The total time for the Scraping is: {total_scrape_time:.2f} seconds.")
print(f"The total time for the Combine is: {total_combine_time:.2f} seconds.")
