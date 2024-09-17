
def celsius_to_fahrenheit(num):
    result = (float(num)/10 * 1.8) + 32
    return round(result, 2)


def millibar_to_hg(num):
    result = float(num)/10 * 0.02953
    return round(result, 2)


# mps is short for Meters Per Second.
def mps_to_mph(num):
    result = float(num)/10 * 2.23694
    return round(result, 2)


def meters_to_miles(num):
    result = float(num) * 0.000621371
    return round(result, 2)


def meters_to_feet(num):
    result = float(num) * 3.28084
    return round(result, 2)


def millimeters_to_inches(num):
    result = float(num)/10 * 0.0393701
    return round(result, 2)


def database_path(curr_db='weatherdata'):
    from configparser import ConfigParser

    config = ConfigParser()
    config.read('config.ini')

    return (
        f"{config[curr_db]['dialect']}+{config[curr_db]['driver']}"
        f"://{config[curr_db]['username']}:{config[curr_db]['password']}"
        f"@{config[curr_db]['host']}:{config[curr_db]['port']}/{curr_db}"
    )


def connection_uri(curr_db='weatherdata'):
    from configparser import ConfigParser

    config = ConfigParser()
    config.read('config.ini')

    return (
        f"postgresql"
        f"://{config[curr_db]['username']}:{config[curr_db]['password']}"
        f"@{config[curr_db]['host']}:{config[curr_db]['port']}/{curr_db}"
    )


def logger(file_name):
    import logging

    logger = logging.getLogger(__name__)
    f_format = logging.Formatter('%(name)s:%(asctime)s:%(message)s')
    f_handler = logging.FileHandler(file_name)
    f_handler.setLevel(logging.DEBUG)
    f_handler.setFormatter(f_format)
    logger.addHandler(f_handler)

    return logger


def convert(seconds):
    min, sec = divmod(seconds, 60)
    hour, min = divmod(min, 60)
    return '%d:%02d:%02d' % (hour, min, sec)


def dir_replicate():
    import os

    dir_files = list()
    fldr_yrs = [yr for yr in os.listdir(f"yearly_files_csv/") if len(yr) == 4]
    for yr in fldr_yrs:
        for file in os.listdir(f"yearly_files_csv/{yr}/"):
            if file.split('.')[1] == 'csv':
                dir_files.append((yr, file.split('.')[0]))
    dir_files.sort(key=lambda x: (x[0], x[1]))

    return dir_files


def db_replicate():
    from sqlalchemy import create_engine
    from sqlalchemy.sql import text

    url = database_path()
    engine = create_engine(url=url)
    select_query = f"SELECT DISTINCT EXTRACT(YEAR from date), station FROM observations ORDER BY 1, 2;"

    with engine.connect() as connection:
        statement = text(select_query)
        result = connection.execute(statement)
        db_records = [(str(x[0]), str(x[1])) for x in result]
    db_records.sort(key=lambda x: (x[0], x[1]))

    return db_records


if __name__ == '__main__':
    from pprint import pp

    dir_struct = dir_replicate()
    db_struct = db_replicate()

    files_clean = list(filter(lambda x: x not in db_struct, dir_struct))

    print(len(files_clean))
    pp(files_clean)
