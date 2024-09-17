import os
import csv
from sqlalchemy.orm import declarative_base, Session
from sqlalchemy import create_engine, insert
from sqlalchemy import Column, Integer, String
from sqlalchemy.sql import text
from dotenv import load_dotenv
import shared_funcs

load_dotenv()

headers = ["abbreviation", "time_zone_name", "utc_offset"]

with open("metadata/us_timezone_abbreviations.csv", "r", newline="") as read_file:
    csv_reader = csv.reader(read_file, delimiter='\t')
    reader_data = [record for idx, record in enumerate(csv_reader) if idx > 0]
    data_clean = list()
    for item in reader_data:
        if item[2].split(' ')[-1][0] == '+':
            data_clean.append([item[0], item[1], '+ ' + item[2].split(' ')[-1][1:]])
        else:
            data_clean.append([item[0], item[1], '- ' + item[2].split(' ')[-1]])

with open("metadata/us_tz_abbrev_clean.csv", "w", newline="") as write_file:
    csv_writer = csv.writer(write_file, delimiter="|")
    csv_writer.writerow(headers)
    for item in data_clean:
        csv_writer.writerow(item)


Base = declarative_base()


class TimeZones(Base):

    __tablename__ = "time_zones"

    id = Column(Integer, primary_key=True)
    abbreviation = Column(String)
    time_zone_name = Column(String)
    utc_offset = Column(Integer)

url_ext_aws = os.getenv('url_ext_aws')
url = shared_funcs.database_path()
engine = create_engine(url=url_ext_aws, pool_size=5, pool_recycle=3600)
Base.metadata.create_all(engine)

with open('metadata/us_tz_abbrev_clean.csv', 'r', newline='') as read_file:
    reader = csv.reader(read_file, delimiter='|')
    time_zone_data = [record for idx, record in enumerate(reader) if idx > 0]

    time_zone_data_clean = list()

    for item in time_zone_data:
        temp_dict = dict()
        temp_dict["abbreviation"] = item[0]
        temp_dict["time_zone_name"] = item[1]
        offset = item[2].split(' ')
        temp_dict["utc_offset"] = int(offset[0] + str(int(offset[1])))
        time_zone_data_clean.append(temp_dict)

delete_query = f"DELETE FROM time_zones;"

with engine.connect() as connection:
    statement = text(delete_query)
    result = connection.execute(statement)
    connection.commit()

session = Session(bind=engine)
session.execute(insert(TimeZones), time_zone_data_clean)
session.commit()
