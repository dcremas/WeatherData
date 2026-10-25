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

-- Re-granted every rebuild, because DROP TABLE takes the privileges with it and
-- CREATE TABLE AS makes a fresh table owned by whoever ran the script, with no
-- grants at all. That was survivable while this was run by hand and someone
-- noticed the bokeh app had gone blank; run nightly by ghcnhPostgresqlUpdate it
-- would silently revoke prosite_ro's access the first time it fired.
--
-- dustincremascoli is granted explicitly rather than relying on ownership: the
-- table is owned by ghcnh_etl so that the Lambda can DROP it, which a non-owner
-- cannot do.
GRANT SELECT ON obs_baro_impact TO prosite_ro;
GRANT SELECT ON obs_baro_impact TO dustincremascoli;
-- mcp_ro reads this table through the MCP server's text-to-SQL tool. It is in
-- the same position prosite_ro was in: granted once by hand, then silently
-- revoked by the first rebuild that ran without this line.
GRANT SELECT ON obs_baro_impact TO mcp_ro;

-- Re-commented every rebuild, for the same reason as the grants: DROP TABLE takes
-- the COMMENTs with it. The MCP server's list_schema/describe_table hand these to
-- the text-to-SQL model, and its selftest fails "every table carries a
-- description" without them -- which is how the loss was found, 2026-10-25.
-- The text is kept in step with comment_tables.sql in weather-sql-explorer/mcp_server
-- (which applies it by hand); no row count in it, since the count changes every load.
COMMENT ON TABLE obs_baro_impact IS
    'Derived: per-station sea-level-pressure change over 3, 6 and 24 hours, '
    'for the subset of stations the barometric visualisation plots '
    '(NOT all 112). Rebuilt from observations on every load, so it is a '
    'convenience table -- the same numbers can be computed from observations with '
    'window functions. Deltas are in INCHES OF MERCURY; negative means falling '
    'pressure, which is the storm-approach signal.';
COMMENT ON COLUMN obs_baro_impact.slp_3hr_diff IS
    'Sea-level pressure change over the previous 3 hours, in inHg. Negative = '
    'falling.';
COMMENT ON COLUMN obs_baro_impact.slp_6hr_diff IS
    'Sea-level pressure change over the previous 6 hours, in inHg.';
COMMENT ON COLUMN obs_baro_impact.slp_24hr_diff IS
    'Sea-level pressure change over the previous 24 hours, in inHg.';

COMMIT;
