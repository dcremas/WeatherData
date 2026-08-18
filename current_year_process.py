"""DEPRECATED - loads the ISD parquet built by the retired current_year_generate.py.

Superseded by ghcnh_process.py. See that module for the three format differences
between ISD and GHCNh, and note this script's ORM bulk insert is the slow path
that COPY replaced (~151 rows/sec over a 44 ms link versus ~77,000).

Kept for provenance; do not run it against a live warehouse.
"""
import time
from datetime import date, datetime
import polars as pl
from pyspark.sql import SparkSession
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from database_ddl import Observations
import shared_funcs

year_no = 2025

# This script reloads the whole of year_no, which for the in-progress year grows
# as the year does, so a fixed floor would either be useless in January or wrong
# in December. Completed years run ~1.19M rows across 112 stations, i.e. roughly
# 3,260 rows/day; require half that per elapsed day so a truncated download is
# caught while still tolerating a source that lags a few days behind today.
ROWS_PER_DAY = 3_260
_elapsed_days = (
    (date.today() - date(year_no, 1, 1)).days + 1
    if year_no == date.today().year
    else 365
)
MIN_YEAR_ROWS = int(ROWS_PER_DAY * _elapsed_days * 0.5)

# Deleting the year_no data that is currently in the Postgres Database.
delete_start_time = time.perf_counter()

delete_query = f"DELETE FROM observations WHERE EXTRACT(YEAR from date) = {year_no};"
url = shared_funcs.database_path('weatherdata')
engine = create_engine(url=url)

with engine.connect() as connection:
    statement = text(delete_query)
    result = connection.execute(statement)
    connection.commit()

delete_stop_time = time.perf_counter()
total_delete_time = delete_stop_time - delete_start_time


# Inserting the new year_no data into the Postgres Database.
insert_start_time = time.perf_counter()

spark = (SparkSession.builder.appName("pyspark_parquet")
         .config("spark.sql.crossJoin.enabled", "true")
         .getOrCreate())

weather_data = spark.read.format("parquet").load(f"yearly_files_parquet/{year_no}/data.parquet")
weather_data.createOrReplaceGlobalTempView("weather_data")

data_clean = list()
data = (tuple(i) for i in weather_data.collect())

for item in data:
    temp_dict = dict()
    temp_dict["station"] = item[0]
    temp_dict["date"] = datetime.strptime(item[1], '%Y-%m-%dT%H:%M:%S')
    temp_dict["source"] = item[2]
    temp_dict["report_type"] = item[3]
    temp_dict["wnd"] = shared_funcs.mps_to_mph(item[4].split(',')[3])
    temp_dict["cig"] = shared_funcs.meters_to_miles(item[5].split(',')[0])
    temp_dict["vis"] = shared_funcs.meters_to_miles(item[6].split(',')[0])
    temp_dict["tmp"] = shared_funcs.celsius_to_fahrenheit(item[7].split(',')[0])
    temp_dict["dew"] = shared_funcs.celsius_to_fahrenheit(item[8].split(',')[0])
    temp_dict["slp"] = shared_funcs.millibar_to_hg(item[9].split(',')[0])
    try:
        temp_dict["prp"] = shared_funcs.millimeters_to_inches(item[10].split(',')[1])
    except (IndexError, AttributeError):
        temp_dict["prp"] = 0.0

    if temp_dict["report_type"] in ['FM-12', 'FM-15']:
        data_clean.append(temp_dict)

session = Session(bind=engine)
inserted = shared_funcs.guarded_insert(
    session, Observations, data_clean,
    min_rows=MIN_YEAR_ROWS, label=str(year_no),
)
session.commit()

insert_stop_time = time.perf_counter()
total_insert_time = insert_stop_time - insert_start_time

# Replicating the year_no data that is currently in the Postgres Database to a parquet file.
replicate_start_time = time.perf_counter()

connection_uri = shared_funcs.connection_uri()
query = f"SELECT * FROM observations WHERE EXTRACT(YEAR from date) = {year_no}"

polars_df = pl.read_database_uri(query=query, uri=connection_uri)
polars_df.write_parquet(f"yearly_files_parquet/{year_no}/data_clean.parquet")

replicate_stop_time = time.perf_counter()
total_replicate_time = replicate_stop_time - replicate_start_time

print(f"Inserted {inserted:,} rows for {year_no} (floor was {MIN_YEAR_ROWS:,}).")
print(f"The total time for the Delete is: {total_delete_time:.2f} seconds.")
print(f"The total time for the Insert is: {total_insert_time:.2f} seconds.")
print(f"The total time for the Replicate is: {total_replicate_time:.2f} seconds.")
