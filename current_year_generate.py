import os
import time
import threading
import urllib.request
import polars as pl


def download_file(url, filename):
    urllib.request.urlretrieve(url, filename)


year_no = 2024

# Create a list of files to download
url_path = f"https://www.ncei.noaa.gov/data/global-hourly/access/{year_no}/"
source_path = f"/Users/dustincremascoli/PycharmProjects/WeatherData/yearly_files_csv/{year_no}"
local_path = f"yearly_files_csv/{year_no}/"
files_to_download =  [
    {"url": f"{url_path}{file.split('.')[0]}.csv",
     "filename": f"{local_path}{file.split('.')[0]}.csv"}
    for file in os.listdir(source_path) if file.split('.')[1] == 'csv'
     ]

# Create a list to store the threads
threads = []

# Create a thread for each file and start the download
scrape_start_time = time.perf_counter()
for file_info in files_to_download:
    thread = threading.Thread(
        target=download_file,
        args=(file_info["url"], file_info["filename"])
    )
    thread.start()
    threads.append(thread)

# Wait for all threads to complete
for thread in threads:
    thread.join()

scrape_stop_time = time.perf_counter()
total_scrape_time = scrape_stop_time - scrape_start_time

select_cols = [
    'STATION', 'DATE', 'SOURCE', 'REPORT_TYPE',
    'WND', 'CIG', 'VIS', 'TMP', 'DEW', 'SLP', 'AA1',
]

# Polars Processing
polars_start_time = time.perf_counter()

polars_df = (
    pl.scan_csv(f"yearly_files_csv/{year_no}/*.csv", dtypes={'SOURCE': str})
    .select(select_cols)
    .collect(streaming=True)
    )

polars_df.write_parquet(f"yearly_files_parquet/{year_no}/data.parquet")

polars_stop_time = time.perf_counter()
total_polars_time = polars_stop_time - polars_start_time

print(f"The total time for the Scraping is: {total_scrape_time:.2f} seconds.")
print(f"The total time for the Polars run is: {total_polars_time:.2f} seconds.")