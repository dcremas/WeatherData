-- One-time fix: stop the ISD-era synoptic rows double counting precipitation.
-- Safe to re-run: the backup is created only once, and after the first run the
-- UPDATE matches nothing. Undo with sql/fix_precip_double_count_undo.sql.
--
-- WHAT WAS WRONG
--
-- prp is meant to be the rain in the hour ending at each report, so that summing
-- a station's rows gives its total. But the FM-12 (synoptic) rows carry 3, 6, 12
-- and 24 hour accumulations, which overlap the FM-15 (hourly METAR) amounts. The
-- old ISD loaders stored those in prp anyway, ignoring AA1's period field, so
-- every total counted the same rain two or three times. Seattle-Tacoma
-- (USW00024233) against NOAA's official GHCN-Daily totals, measured 2026-10-12:
--
--   year   FM-15 rows   FM-12 rows   official
--   2019     34.21        +42.63       33.90
--   2022     40.92        +48.90       40.98
--   2024     34.42        +53.56       32.20
--
-- Miami, Denver and Chicago O'Hare show the same: the FM-15 rows alone are
-- within a few percent of official, and the FM-12 rows are pure double counting
-- (Miami 2022: 71.77 + 137.45 against 71.60 official).
--
-- WHAT THIS DOES
--
-- Sets prp to NULL on FM-12 rows before 2025, which is the rule the fixed loader
-- (GHCNH_PRECIP_COLUMN in shared_funcs.py) applies to new rows. NULL, not 0.0:
-- these reports carry no 1-hour amount, which is not the same as no rain.
-- analytics_slp_decrease.sql reads FM-15 rows only, so obs_baro_impact is
-- unaffected.
--
-- 2025 onward is NOT fixed here. Those rows came from GHCNh, where the old
-- coalesce also inflated the FM-15 rows (Miami 2025: 88.78 against 57.95), so
-- they need reloading with the fixed loader instead:
--     python ghcnh_generate.py <year> && python ghcnh_process.py <year> all <target>
--
-- DELIBERATELY NOT TOUCHED
--
-- ISD-era FM-15 rows. A few still carry a multi-hour AA1 amount (Seattle 2024
-- runs 6-7% high, Denver 2022 8%), but they cannot be told apart from 1-hour
-- amounts in the stored data. Reloading those years' prp from GHCNh would fix
-- them exactly; it is a separate job.

BEGIN;

-- Everything this UPDATE changes, keyed by id, so the undo script can put it
-- back. IF NOT EXISTS keeps the original snapshot on a re-run.
CREATE TABLE IF NOT EXISTS prp_fm12_backup_20261012 AS
SELECT id, prp
FROM observations
WHERE report_type = 'FM-12'
  AND date < '2025-01-01'
  AND prp IS NOT NULL;

UPDATE observations
SET prp = NULL
WHERE report_type = 'FM-12'
  AND date < '2025-01-01'
  AND prp IS NOT NULL;

-- The description the MCP server hands the model. Kept in step with
-- weather-sql-explorer/mcp_server/comment_tables.sql.
COMMENT ON COLUMN public.observations.prp IS
  'Precipitation in INCHES during the hour ending at this report (the METAR '
  '1-hour amount). Only FM-15 (hourly) rows carry it; FM-12 (synoptic) rows are '
  'NULL, because their 3-24 hour totals overlap the hourly amounts. SUM(prp) over '
  'a station''s rows is its precipitation total, within a few percent of NOAA''s '
  'official GHCN-Daily figures.';

COMMIT;

-- The update rewrites every affected row, so reclaim the dead tuples afterwards.
-- Not inside the transaction because VACUUM cannot run in one.
VACUUM (ANALYZE) observations;
