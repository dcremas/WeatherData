-- Rebuilds obs_baro_impact: per-station sea-level-pressure deltas over 3, 6 and
-- 24 hours, for the ten stations the scatter_baro_impact visualisation plots.
--
-- Wrapped in a transaction because DDL is transactional in Postgres: without it,
-- a failure between the DROP and the CREATE leaves no table at all and the bokeh
-- app errors. Committing together means readers keep seeing the previous table
-- until the new one is ready.
--
-- DO NOT add `AND obs.source IN ('6', '7')` here. An older copy of this script
-- in bokeh_server/scatter_baro_impact/obs_baro_impact_script.sql still carries
-- that line, which was correct for ISD's single-digit source codes but excludes
-- every GHCNh row: GHCNh uses 3-digit codes (223, 343, 413), so the filter keeps
-- 0 of 1.59M post-2024 observations and silently drops all current data.

BEGIN;

DROP TABLE IF EXISTS obs_baro_impact;

CREATE TABLE obs_baro_impact AS
SELECT
	obs.station,
	loc.station_name,
	reg.region,
    reg.sub_region,
    loc.state,
	obs.date,
    EXTRACT(YEAR from obs.date) AS rdg_year,
	EXTRACT(MONTH from obs.date) AS rdg_month,
	EXTRACT(DAY from obs.date) AS rdg_day,
	EXTRACT(HOUR from obs.date) AS rdg_hour,
	COALESCE(ROUND(obs.slp::numeric - LAG(obs.slp, 3) OVER(PARTITION BY obs.station ORDER BY obs.date)::numeric, 2), 0.0) AS slp_3hr_diff,
	COALESCE(ROUND(obs.slp::numeric - LAG(obs.slp, 6) OVER(PARTITION BY obs.station ORDER BY obs.date)::numeric, 2), 0.0) AS slp_6hr_diff,
	COALESCE(ROUND(obs.slp::numeric - LAG(obs.slp, 24) OVER(PARTITION BY obs.station ORDER BY obs.date)::numeric, 2), 0.0) AS slp_24hr_diff
FROM observations obs
JOIN locations loc
    ON obs.station = loc.station
JOIN regions reg
    ON loc.state = reg.state
WHERE obs.station IN ('70381025309', '72290023188', '72530094846', '72494023234', '72565003017', '91182022521', '72509014739', '72606014764', '72306013722', '74486094789')
	AND obs.report_type IN ('FM-15')
	AND obs.slp BETWEEN 20.00 AND 35.00
	AND obs.prp <= 10.00;

CREATE INDEX obs_baro_impact_station
ON obs_baro_impact(station);

COMMIT;
