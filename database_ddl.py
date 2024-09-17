import os
from datetime import datetime
from sqlalchemy.orm import declarative_base
from sqlalchemy import create_engine
from sqlalchemy import Column, Integer, Float, DateTime, String
from dotenv import load_dotenv
import shared_funcs

load_dotenv()


Base = declarative_base()


class Observations(Base):

    __tablename__ = "observations"

    id = Column(Integer, primary_key=True)
    station = Column(String)
    date = Column(DateTime)
    source = Column(String)
    report_type = Column(String)
    wnd = Column(Float)
    cig = Column(Float)
    vis = Column(Float)
    tmp = Column(Float)
    dew = Column(Float)
    slp = Column(Float)
    prp = Column(Float)
    timestamp = Column(DateTime, default=datetime.now())


class Locations(Base):

    __tablename__ = "locations"

    location_id = Column(Integer, primary_key=True)
    station = Column(String)
    usaf = Column(String)
    wban = Column(String)
    station_name = Column(String)
    ctry = Column(String)
    state = Column(String)
    icao = Column(String)
    lat = Column(Float)
    lon = Column(Float)
    elevation = Column(Float)
    begin = Column(DateTime)
    end = Column(DateTime)
    timestamp = Column(DateTime, default=datetime.now())

url_ext_aws = os.getenv('url_ext_aws')
url = shared_funcs.database_path()
engine = create_engine(url=url, pool_size=5, pool_recycle=3600)
Base.metadata.create_all(engine)
