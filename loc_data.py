import os
import csv
from datetime import datetime
import pandas as pd
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from dotenv import load_dotenv
from database_ddl import Locations
import shared_funcs

load_dotenv()

df_locations = pd.read_csv("https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv")
df_locations.to_csv("metadata/location_data.csv")

with open('metadata/location_data.csv', 'r', newline='') as read_file:
    reader = csv.reader(read_file, delimiter=',')
    location_data = (record for idx, record in enumerate(reader) if idx > 0)

    location_data_clean = list()

    for item in location_data:
        try:
            temp_dict = dict()
            station_mod = '0' + item[2] if len(item[2]) == 4 else item[2]
            temp_dict["station"] = item[1] + station_mod
            temp_dict["usaf"] = item[1]
            temp_dict["wban"] = item[2]
            temp_dict["station_name"] = item[3]
            temp_dict["ctry"] = item[4]
            temp_dict["state"] = item[5]
            temp_dict["icao"] = item[6]
            temp_dict["lat"] = float(item[7])
            temp_dict["lon"] = float(item[8])
            temp_dict["elevation"] = shared_funcs.meters_to_feet(item[9])
            temp_dict["begin"] = datetime.strptime(item[10], '%Y%m%d')
            temp_dict["end"] = datetime.strptime(item[11], '%Y%m%d')
            location_data_clean.append(temp_dict)
        except ValueError:
            continue

url_ext_aws = os.getenv('url_ext_aws')
url = shared_funcs.database_path()
engine = create_engine(url=url_ext_aws)

delete_query = f"DELETE FROM locations;"

with engine.connect() as connection:
    statement = text(delete_query)
    result = connection.execute(statement)
    connection.commit()

session = Session(bind=engine)
session.execute(insert(Locations), location_data_clean)
session.commit()
