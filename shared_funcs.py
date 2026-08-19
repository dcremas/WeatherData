
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


# ---------------------------------------------------------------------------
# GHCNh unit converters.
#
# The six converters above are for ISD, which packed every measurement as a
# scaled integer in tenths - hence the /10 in each one. GHCNh supersedes ISD and
# publishes real units in typed columns instead, so reusing the ISD converters on
# GHCNh input silently produces values 10x too small. These take real units, and
# return None for a missing reading rather than raising, because GHCNh uses NULL
# where ISD used sentinel codes.
#
# Note the two distance units: GHCNh reports visibility in KILOMETRES but
# ceiling height in METRES, so they need different converters.
# ---------------------------------------------------------------------------

def ghcnh_temp_f(celsius):
    if celsius is None:
        return None
    return round((float(celsius) * 1.8) + 32, 2)


def ghcnh_pressure_hg(hectopascals):
    if hectopascals is None:
        return None
    return round(float(hectopascals) * 0.02953, 2)


def ghcnh_wind_mph(meters_per_second):
    if meters_per_second is None:
        return None
    return round(float(meters_per_second) * 2.23694, 2)


def ghcnh_visibility_miles(kilometers):
    if kilometers is None:
        return None
    return round(float(kilometers) * 0.621371, 2)


def ghcnh_ceiling_miles(meters):
    if meters is None:
        return None
    return round(float(meters) * 0.000621371, 2)


def ghcnh_precip_inches(millimeters):
    """Missing precipitation becomes 0.0, not None.

    This matches what the ISD loaders did, and it matters downstream:
    analytics_slp_decrease.sql filters on `obs.prp <= 10.00`, which a NULL
    would fail, silently dropping the row from obs_baro_impact.
    """
    if millimeters is None:
        return 0.0
    return round(float(millimeters) * 0.0393701, 2)


def ghcnh_station_id(isd_station):
    """Map an 11-character ISD station id to its GHCNh id.

    ISD ids are a 6-digit USAF id followed by a 5-digit WBAN; GHCNh uses
    'USW000' plus the same WBAN. Verified against the GHCNh station list for all
    112 stations in the warehouse.
    """
    return 'USW000' + isd_station[6:]


# GHCNh keeps only the observations ISD called FM-12 (synoptic) and FM-15
# (METAR). FM16 (special report) and the empty 5-minute placeholder rows GHCNh
# emits for the in-progress year are dropped, matching the ISD loaders. The keys
# are GHCNh's unhyphenated spellings; the values are the hyphenated forms already
# in the warehouse, which analytics_slp_decrease.sql filters on.
GHCNH_KEEP_REPORT_TYPES = {'FM12': 'FM-12', 'FM15': 'FM-15'}

# Variables consulted in order when collapsing GHCNh's per-variable provenance to
# one row-level report_type/source. Temperature leads because it is the most
# consistently reported; on a 2025 sample the non-null report types agreed on
# 13,420 of 13,440 rows, so the choice of leader is very nearly immaterial.
GHCNH_PROVENANCE_ORDER = [
    'temperature', 'sea_level_pressure', 'wind_speed', 'visibility',
]

# GHCNh grades every reading with the ISD quality-code scale, where 2 and 6 mean
# suspect and 3 and 7 mean erroneous, while 0, 1, 4, 5 and 9 have passed their
# checks. Readings carrying a reject code are stored as NULL rather than
# converted: without this a QC-7 temperature of 219 C and a QC-3 wind of
# 465 m/s land in the warehouse as 426 F and 1,041 mph. ISD offered no
# equivalent signal, which is part of why the pre-2025 rows contain sentinels.
GHCNH_REJECT_QUALITY_CODES = {'2', '3', '6', '7'}

# Precipitation columns in ascending accumulation period. ISD carried a single
# AA1 depth whose period the old loaders ignored, so the closest match to the
# historical series is the shortest period actually reported. Without this
# coalesce, synoptic-hour rows that report only a 3, 6 or 24 hour total would be
# written as 0.0 inches.
GHCNH_PRECIP_ORDER = [
    'precipitation', 'precipitation_3_hour', 'precipitation_6_hour',
    'precipitation_12_hour', 'precipitation_24_hour',
]


def _period_prefixes(periods):
    """ISO date prefixes for the (year, month) periods a load covers.

    month is None for a whole year. GHCNh writes DATE as an ISO string, so a
    period test is a cheap string prefix rather than a parse of all 5.9M rows.
    """
    prefixes = []
    for year_no, month_no in periods:
        if month_no is None:
            prefixes.append(f"{year_no}-")
        else:
            prefixes.append(f"{year_no}-{month_no:02d}-")
    return prefixes


def ghcnh_row(row, report_type_cols, source_cols):
    """Map one GHCNh row to an observations-shaped dict.

    Split out of ghcnh_transform so the streaming reader and any validation
    tooling share exactly one implementation of the mapping. Callers must have
    already established that the row is an FM-12/FM-15 in the wanted period.
    """
    from datetime import datetime

    def first_non_null(columns):
        for column in columns:
            value = row.get(column)
            if value is not None:
                return value
        return None

    def checked(column):
        """The reading, or None when GHCNh flagged it suspect or erroneous."""
        if row.get(f'{column}_Quality_Code') in GHCNH_REJECT_QUALITY_CODES:
            return None
        return row.get(column)

    def first_checked(columns):
        for column in columns:
            value = checked(column)
            if value is not None:
                return value
        return None

    report_type = first_non_null(report_type_cols)

    temp_dict = dict()
    temp_dict["station"] = row['isd_station']
    temp_dict["date"] = datetime.strptime(row['DATE'], '%Y-%m-%dT%H:%M:%S')
    temp_dict["source"] = first_non_null(source_cols)
    temp_dict["report_type"] = GHCNH_KEEP_REPORT_TYPES[report_type]
    temp_dict["wnd"] = ghcnh_wind_mph(checked('wind_speed'))
    temp_dict["cig"] = ghcnh_ceiling_miles(checked('ceiling_height'))
    temp_dict["vis"] = ghcnh_visibility_miles(checked('visibility'))
    temp_dict["tmp"] = ghcnh_temp_f(checked('temperature'))
    temp_dict["dew"] = ghcnh_temp_f(checked('dew_point_temperature'))
    temp_dict["slp"] = ghcnh_pressure_hg(checked('sea_level_pressure'))
    temp_dict["prp"] = ghcnh_precip_inches(first_checked(GHCNH_PRECIP_ORDER))

    return temp_dict


def ghcnh_transform(parquet_source, year_no=None, month_no=None, periods=None):
    """Turn a combined GHCNh parquet into observations-shaped dicts.

    Shared by ghcnh_process.py, the Lambda loader and the validation tooling so
    there is exactly one implementation of the GHCNh -> observations mapping.

    parquet_source is anything pyarrow can open - a path, or a file-like object,
    which is how the Lambda streams the object straight out of S3 without
    staging it on disk.

    Pass either (year_no, month_no) for a single period, or periods as a list of
    (year, month) tuples - which is what the nightly load uses to rebuild the
    current and previous months together. month None means the whole year.

    Reads one row group at a time and pushes the report-type and period filters
    down onto the Arrow table BEFORE converting anything to Python. The previous
    version called table.to_pydict() on the whole file and looped over every
    row: correct, but it materialised all 5.9M rows x 32 columns as Python
    objects to keep the ~50k that survive the filters, peaking at 4.4 GB and
    taking 130 seconds. Filtering in Arrow first keeps the row logic identical
    while holding one row group in memory at a time.

    Returns (rows_read, records).
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if periods is None:
        if year_no is None:
            raise ValueError("ghcnh_transform needs year_no or periods.")
        periods = [(year_no, month_no)]

    prefixes = _period_prefixes(periods)

    parquet_file = pq.ParquetFile(parquet_source)
    schema_names = set(parquet_file.schema_arrow.names)

    # Mirrors the dict.get() lookups the row logic uses: a provenance column the
    # generator does not download is skipped, not an error. visibility_Source_Code
    # is exactly that case - GHCNh publishes it, ghcnh_generate.py does not select
    # it, and the original code silently fell through to None.
    report_type_cols = [f"{name}_Report_Type" for name in GHCNH_PROVENANCE_ORDER
                        if f"{name}_Report_Type" in schema_names]
    source_cols = [f"{name}_Source_Code" for name in GHCNH_PROVENANCE_ORDER
                   if f"{name}_Source_Code" in schema_names]

    if not report_type_cols:
        raise RuntimeError(
            f"{parquet_source}: none of the expected *_Report_Type columns are "
            f"present, so no row can be classified as FM-12/FM-15."
        )

    keep_types = pa.array(list(GHCNH_KEEP_REPORT_TYPES))

    row_total = 0
    data_clean = list()

    for group_no in range(parquet_file.num_row_groups):
        table = parquet_file.read_row_group(group_no)
        row_total += table.num_rows

        report_type = table[report_type_cols[0]]
        for column in report_type_cols[1:]:
            report_type = pc.coalesce(report_type, table[column])
        mask = pc.is_in(report_type, value_set=keep_types)

        date_column = table['DATE']
        period_mask = pc.starts_with(date_column, pattern=prefixes[0])
        for prefix in prefixes[1:]:
            period_mask = pc.or_(
                period_mask, pc.starts_with(date_column, pattern=prefix)
            )

        # fill_null: a null DATE or report type is "not in this period", the
        # same outcome the row-at-a-time version reached by falling through.
        table = table.filter(pc.fill_null(pc.and_(mask, period_mask), False))
        if table.num_rows == 0:
            continue

        columns = table.to_pydict()
        for index in range(table.num_rows):
            row = {name: columns[name][index] for name in columns}
            data_clean.append(ghcnh_row(row, report_type_cols, source_cols))

    return row_total, data_clean


# Column order used by copy_observations. `id` is omitted so the sequence default
# applies. `timestamp` must be listed explicitly: it has no database-side default
# - the ISD loaders got it from the ORM's Python-side default, which COPY
# bypasses - and it is the only record of when each slice was loaded.
COPY_OBSERVATION_COLUMNS = [
    'station', 'date', 'source', 'report_type',
    'wnd', 'cig', 'vis', 'tmp', 'dew', 'slp', 'prp', 'timestamp',
]


def _copy_field(value):
    """Render one value for COPY ... FROM STDIN in text format."""
    if value is None:
        return r'\N'
    if isinstance(value, str):
        return (value.replace('\\', r'\\')
                     .replace('\t', r'\t')
                     .replace('\n', r'\n')
                     .replace('\r', r'\r'))
    return str(value)


def copy_observations(connection, records, min_rows=1, label='load',
                     loaded_at=None):
    """Bulk-load observations with COPY instead of row-wise INSERT.

    The ORM bulk insert costs one network round trip per handful of rows. Against
    localhost that is invisible, but the EC2 warehouse is reached through an SSH
    tunnel with ~44 ms of latency, where it collapses to ~151 rows/sec - a
    1.2M-row year takes over two hours. COPY streams the whole payload in one
    operation and measures ~77,000 rows/sec over the same tunnel, so the same
    year lands in about 42 seconds.

    Takes an open SQLAlchemy Connection and does NOT commit: the caller's
    transaction governs, so the DELETE of a slice and the COPY that rebuilds it
    commit together and readers never observe the slice empty.

    Returns the number of rows supplied.
    """
    import io
    from datetime import datetime

    require_rows(records, min_rows=min_rows, label=label)

    # One timestamp for the whole slice, matching what the ORM default produced.
    if loaded_at is None:
        loaded_at = datetime.now()

    buffer = io.StringIO()
    for record in records:
        row = dict(record)
        row.setdefault('timestamp', loaded_at)
        buffer.write('\t'.join(
            _copy_field(row.get(column)) for column in COPY_OBSERVATION_COLUMNS
        ))
        buffer.write('\n')
    buffer.seek(0)

    column_list = ', '.join(COPY_OBSERVATION_COLUMNS)
    statement = f"COPY observations ({column_list}) FROM STDIN"

    # The DBAPI connection underneath this Connection, so the COPY runs inside
    # the transaction the caller already opened.
    cursor = connection.connection.cursor()
    if connection.engine.dialect.driver == 'psycopg2':
        cursor.copy_expert(statement, buffer)
    else:
        # psycopg (v3) replaced copy_expert with a context-managed writer.
        with cursor.copy(statement) as copy:
            copy.write(buffer.read())
    copied = cursor.rowcount

    if copied is not None and copied >= 0 and copied != len(records):
        raise RuntimeError(
            f"{label}: COPY reported {copied} rows but {len(records):,} were "
            f"supplied."
        )

    return len(records)


def station_roster(path='metadata/stations.csv'):
    """Return [(isd_station, ghcnh_station, station_name, state), ...].

    The roster is read from metadata/stations.csv rather than from the ISD
    download folders, so the GHCNh pipeline keeps working once the retired ISD
    csv directories are deleted.
    """
    import csv

    with open(path, 'r', newline='') as read_file:
        reader = csv.reader(read_file)
        next(reader)
        return [(row[0], row[1], row[2], row[3]) for row in reader]


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


def require_rows(records, min_rows=1, label='load'):
    """Refuse a batch that is empty or implausibly short.

    Call this *before* a DELETE in the scripts that replace a whole table or a
    whole period, so a bad source can never wipe good data and then fail on the
    insert.  guarded_insert() applies the same check again just before writing.

    min_rows is a plausibility floor: pass the smallest count a healthy load
    would ever produce, so a truncated source is caught here rather than
    discovered months later.
    """
    if not records:
        raise RuntimeError(
            f"{label}: 0 rows to load - refusing, because an empty insert "
            f"writes one all-NULL row instead of nothing. The source files "
            f"most likely do not cover the requested period."
        )

    if len(records) < min_rows:
        raise RuntimeError(
            f"{label}: only {len(records):,} rows to load but at least "
            f"{min_rows:,} were expected - refusing, the source data looks "
            f"short or truncated. Re-download, or lower min_rows deliberately."
        )

    return len(records)


def guarded_insert(session, model, records, min_rows=1, label='load'):
    """Bulk-insert records, refusing the empty batch and verifying the row count.

    An empty list must never reach session.execute(insert(model), records):
    SQLAlchemy treats it as a single INSERT of column defaults and silently
    writes one all-NULL row.  That is how the junk rows timestamped 2024-09-04
    through 2025-10-02 got into the observations table, and it made runs against
    a stale source look successful.
    """
    from sqlalchemy import insert

    require_rows(records, min_rows=min_rows, label=label)

    result = session.execute(insert(model), records)

    # An ORM bulk insert returns an IteratorResult, which exposes no rowcount;
    # a Core executemany does, and reports one affected row per record. Only
    # assert when the driver actually gave us a usable count.
    rowcount = getattr(result, 'rowcount', None)
    if isinstance(rowcount, int) and rowcount >= 0 and rowcount != len(records):
        raise RuntimeError(
            f"{label}: inserted {rowcount} rows but supplied "
            f"{len(records):,} - aborting before commit."
        )

    return len(records)


def load_window(today=None, months_back=1):
    """The (year, month) periods a nightly load rebuilds.

    The daily job reloads the current month AND the month before it, rather than
    the current month alone. GHCNh keeps revising recent observations - late
    arrivals and corrected readings land days after the hour they describe - and
    a current-month-only job stops looking at those revisions the moment the
    month rolls over, so a correction published on the 2nd for the 30th would
    never be picked up. Reloading two months costs a few extra seconds of COPY.

    Returns oldest first, and crosses the year boundary correctly: on any day in
    January the window is [(prev_year, 12), (year, 1)], which is why the download
    step fetches two years' files in January.
    """
    from datetime import date

    if today is None:
        today = date.today()

    periods = []
    year_no, month_no = today.year, today.month
    for _step in range(months_back + 1):
        periods.append((year_no, month_no))
        month_no -= 1
        if month_no == 0:
            year_no, month_no = year_no - 1, 12

    return list(reversed(periods))


def window_bounds(periods):
    """(start, end) datetimes covering periods, end exclusive.

    Used for the DELETE that precedes the reload. A half-open range on `date`
    uses idx_observations_date, where the EXTRACT(year ...)/EXTRACT(month ...)
    predicates the monthly loader uses cannot.
    """
    from datetime import datetime

    first_year, first_month = periods[0]
    last_year, last_month = periods[-1]

    start = datetime(first_year, first_month, 1)
    if last_month == 12:
        end = datetime(last_year + 1, 1, 1)
    else:
        end = datetime(last_year, last_month + 1, 1)

    return start, end


def verify_window_freshness(engine, start, end, max_lag_days=3, now=None):
    """Check that a freshly loaded window actually reaches close to today.

    verify_month_coverage() answers "did this completed month reach its end",
    which is the right question for the monthly loader but the wrong one for a
    nightly run: the current month is incomplete by definition, and on the 1st
    the previous month is not published yet either. The question that holds every
    day is whether the newest observation is recent - which is exactly what a
    stalled upstream source stops being. NOAA publishes GHCNh with roughly a
    two-day lag, so three days is the smallest threshold that does not alarm on
    a healthy feed.

    Returns (row_count, min_date, max_date); raises if the window is empty or
    the newest row is older than max_lag_days.
    """
    from datetime import datetime
    from sqlalchemy.sql import text

    if now is None:
        now = datetime.now()

    select_query = (
        "SELECT count(*), min(date), max(date) FROM observations "
        "WHERE date >= :start AND date < :end"
    )

    with engine.connect() as connection:
        statement = text(select_query)
        row_count, min_date, max_date = connection.execute(
            statement, {"start": start, "end": end}
        ).one()

    if not row_count:
        raise RuntimeError(
            f"{start:%Y-%m-%d} to {end:%Y-%m-%d}: no rows present after load."
        )

    lag_days = (now - max_date).days
    if lag_days > max_lag_days:
        raise RuntimeError(
            f"{start:%Y-%m-%d} to {end:%Y-%m-%d}: loaded {row_count:,} rows but "
            f"the newest observation is {max_date} - {lag_days} days old, past "
            f"the {max_lag_days}-day threshold. GHCNh has most likely stopped "
            f"publishing; do not treat this load as current."
        )

    return row_count, min_date, max_date


def verify_month_coverage(engine, year_no, month_no, max_gap_days=2):
    """Check that a freshly loaded month actually reaches the end of that month.

    A row-count floor alone does not catch a source that froze part-way through
    a month: the August 2025 load looked reasonable at 87,566 rows but stopped
    dead on 2025-08-27 because NOAA had stopped publishing.  Comparing the
    loaded max(date) against the month end catches exactly that case.

    Returns (row_count, min_date, max_date); raises if coverage is short.
    """
    from calendar import monthrange
    from datetime import date
    from sqlalchemy.sql import text

    select_query = (
        "SELECT count(*), min(date), max(date) FROM observations "
        "WHERE EXTRACT(year from date) = :yr AND EXTRACT(month from date) = :mo"
    )

    with engine.connect() as connection:
        statement = text(select_query)
        row_count, min_date, max_date = connection.execute(
            statement, {"yr": year_no, "mo": month_no}
        ).one()

    if not row_count:
        raise RuntimeError(
            f"{year_no}-{month_no:02d}: no rows present after load."
        )

    month_end = date(year_no, month_no, monthrange(year_no, month_no)[1])
    gap_days = (month_end - max_date.date()).days

    if gap_days > max_gap_days:
        raise RuntimeError(
            f"{year_no}-{month_no:02d}: loaded {row_count:,} rows but coverage "
            f"stops at {max_date} - {gap_days} days short of {month_end}. The "
            f"source is probably stale or still publishing; do not treat this "
            f"month as complete."
        )

    return row_count, min_date, max_date


if __name__ == '__main__':
    from pprint import pp

    dir_struct = dir_replicate()
    db_struct = db_replicate()

    files_clean = list(filter(lambda x: x not in db_struct, dir_struct))

    print(len(files_clean))
    pp(files_clean)
