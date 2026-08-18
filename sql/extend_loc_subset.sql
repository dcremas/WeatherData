-- Extend loc_subset forward to cover every year present in observations.
--
-- loc_subset is a station x year scaffold: box_plots and geo_views drive their
-- final SELECT from it with a LEFT OUTER JOIN, so a station with no qualifying
-- days in a year still appears with a zero rather than vanishing from the plot.
-- Nothing in the repo built it, and it had stalled at 2019-2024 while
-- observations ran to 2026 - so those apps could not report on 2025 or later no
-- matter what year range their queries asked for.
--
-- Station attributes do not change from year to year, so new years are
-- replicated from the distinct station rows already present. Extends forward
-- only: the `year >= min(year)` guard stops this from retroactively inventing
-- 2005-2018 scaffold rows on the local warehouse, which holds that deeper
-- history but never had scaffold for it.
--
-- Idempotent: the NOT EXISTS clause makes a second run insert nothing.

INSERT INTO loc_subset (year, station, station_name, region, sub_region, state, lat, lon)
SELECT y.yr, s.station, s.station_name, s.region, s.sub_region, s.state, s.lat, s.lon
FROM (
    SELECT DISTINCT EXTRACT(YEAR from date)::numeric AS yr
    FROM observations
    WHERE date IS NOT NULL
) y
CROSS JOIN (
    SELECT DISTINCT station, station_name, region, sub_region, state, lat, lon
    FROM loc_subset
) s
WHERE y.yr >= (SELECT min(year) FROM loc_subset)
  AND NOT EXISTS (
      SELECT 1 FROM loc_subset ls
      WHERE ls.year = y.yr AND ls.station = s.station
  );
