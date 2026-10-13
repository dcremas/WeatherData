"""Compare every station's yearly precipitation total with NOAA's official figure.

    python validate_precip.py                 # local warehouse, 2019-2025
    python validate_precip.py remote          # the serving warehouse
    python validate_precip.py local 2005 2025

Read-only. The reference is NOAA's Global Summary of the Year (GSOY), built from
GHCN-Daily -- the official annual totals -- fetched from the NCEI access service.
The warehouse side is SUM(prp) per station and calendar year, which since the
precipitation fix (GHCNH_PRECIP_COLUMN in shared_funcs.py and
sql/fix_precip_double_count.sql) is meant to be the station's total.

Expect a few percent of disagreement, not zero: the hourly METAR amounts are rounded
to 0.01 in per row, a heated ASOS gauge under-reads snow, and the warehouse's day
boundary is local standard time. Before the fix the typical station-year was 2-3x
the official total.
"""

import csv
import io
import os
import statistics
import sys
import urllib.request

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.sql import text

import shared_funcs

load_dotenv()

target = sys.argv[1] if len(sys.argv) > 1 else 'local'
first_year = int(sys.argv[2]) if len(sys.argv) > 2 else 2019
last_year = int(sys.argv[3]) if len(sys.argv) > 3 else 2025

GSOY_URL = ("https://www.ncei.noaa.gov/access/services/data/v1"
            "?dataset=global-summary-of-the-year&dataTypes=PRCP&units=standard"
            "&format=csv&startDate={start}-01-01&endDate={end}-12-31&stations={ids}")
BATCH = 40          # stations per request; the service caps the URL, not the rows


def official_totals(ghcn_ids):
    """{(ghcn_id, year): inches} from GSOY. Station-years GSOY omits are absent."""
    totals = {}
    for i in range(0, len(ghcn_ids), BATCH):
        url = GSOY_URL.format(start=first_year, end=last_year,
                              ids=",".join(ghcn_ids[i:i + BATCH]))
        with urllib.request.urlopen(url, timeout=120) as response:
            body = response.read().decode()
        for row in csv.DictReader(io.StringIO(body)):
            if row.get("PRCP", "").strip():
                totals[(row["STATION"], int(row["DATE"]))] = float(row["PRCP"])
    return totals


def warehouse_totals(engine):
    """{(isd_station, year): (inches, rows_with_prp)} from the observations table."""
    sql = text("""
        SELECT station, EXTRACT(year FROM date)::int AS yr,
               SUM(prp) AS total, COUNT(prp) AS reports
        FROM observations
        WHERE date >= make_date(:y0, 1, 1) AND date < make_date(:y1 + 1, 1, 1)
        GROUP BY 1, 2
    """)
    with engine.connect() as connection:
        rows = connection.execute(sql, {"y0": first_year, "y1": last_year})
        return {(r.station, r.yr): (float(r.total or 0), r.reports) for r in rows}


def main():
    with open("metadata/stations.csv") as f:
        stations = list(csv.DictReader(f))
    isd_to_ghcn = {s["isd_station"]: s["ghcnh_station"] for s in stations}
    names = {s["isd_station"]: s["station_name"] for s in stations}

    url = (os.getenv('url_ext_aws') if target == 'remote'
           else shared_funcs.database_path('weatherdata'))
    ours = warehouse_totals(create_engine(url))
    theirs = official_totals(sorted(isd_to_ghcn.values()))

    compared = []
    for (isd, year), (total, reports) in ours.items():
        official = theirs.get((isd_to_ghcn.get(isd), year))
        # Skip station-years too dry to give a meaningful ratio.
        if official is None or official < 2.0:
            continue
        compared.append((100 * (total - official) / official, isd, year, total, official))

    if not compared:
        sys.exit("Nothing to compare -- check the target and the year range.")

    print(f"{target}: {len(compared)} station-years compared, {first_year}-{last_year}\n")
    print("year  station-years  median error  within 5%  within 10%")
    for year in range(first_year, last_year + 1):
        errs = [e for e, _, y, _, _ in compared if y == year]
        if errs:
            print(f"{year}  {len(errs):13}  {statistics.median(errs):+11.1f}%"
                  f"  {sum(abs(e) <= 5 for e in errs) / len(errs):8.0%}"
                  f"  {sum(abs(e) <= 10 for e in errs) / len(errs):9.0%}")
    errs = [e for e, *_ in compared]
    print(f"\nall   {len(errs):13}  {statistics.median(errs):+11.1f}%"
          f"  {sum(abs(e) <= 5 for e in errs) / len(errs):8.0%}"
          f"  {sum(abs(e) <= 10 for e in errs) / len(errs):9.0%}")

    print("\nWorst ten station-years:")
    for err, isd, year, total, official in sorted(compared, key=lambda c: -abs(c[0]))[:10]:
        print(f"  {err:+7.1f}%  {year}  {names.get(isd, isd)[:38]:38}"
              f"  warehouse {total:7.2f}  official {official:7.2f}")


if __name__ == "__main__":
    main()
