-- One-time cleanup of ISD "missing reading" sentinels in the 2005-2024 rows.
-- Safe to re-run: NULLIF on an already-NULL column is a no-op, and after the
-- first run the WHERE clause matches nothing.
--
-- WHY THESE VALUES
--
-- ISD encoded a missing reading as an all-9s code rather than as NULL, and the
-- original loaders converted whatever they found without checking. So a missing
-- reading became a real-looking number in the warehouse. Feeding each ISD missing
-- code through the converter that produced the stored value gives:
--
--   column  ISD code   stored value   shared_funcs converter
--   tmp      9999        1831.82       celsius_to_fahrenheit
--   dew      9999        1831.82       celsius_to_fahrenheit
--   slp     99999         295.30       millibar_to_hg
--   wnd      9999        2236.72       mps_to_mph
--   vis    999999         621.37       meters_to_miles
--   cig     99999          62.14       meters_to_miles
--   prp      9999          39.37       millimeters_to_inches
--
-- Confirmed against the data before running: each value appeared as a large
-- spike (cig 62.14 alone was 2.6M rows) while no other physically impossible
-- values existed. GHCNh, which supersedes ISD from 2025, publishes NULL for a
-- missing reading and a quality code for a bad one, so post-2024 rows never had
-- this problem - hence the date bound.
--
-- DELIBERATELY NOT TOUCHED
--
--   cig = 13.67  ISD code 22000 is "unlimited ceiling", a real observation, not
--                a missing one. GHCNh reports 22000 for the same condition.
--   prp > 20 and <> 39.37  Seven rows, each a distinct one-off value (37.6, 37.2,
--                34.69 ...) rather than a repeated spike, all at synoptic hours.
--                These look like multi-hour accumulations that the old loader
--                mislabelled by ignoring AA1's period field - wrong semantics,
--                but real readings, so not sentinels.
--
-- Setting prp sentinels to NULL rather than 0.0 is intentional: those rows were
-- already excluded from obs_baro_impact by `prp <= 10.00` (39.37 fails it, and so
-- does NULL), so the analytics output is unchanged. Verified by md5 checksum of
-- obs_baro_impact before and after on both warehouses - byte identical.

UPDATE observations SET
  tmp = NULLIF(tmp, 1831.82),
  dew = NULLIF(dew, 1831.82),
  slp = NULLIF(slp, 295.3),
  wnd = NULLIF(wnd, 2236.72),
  vis = NULLIF(vis, 621.37),
  cig = NULLIF(cig, 62.14),
  prp = NULLIF(prp, 39.37)
WHERE date < '2025-01-01'
  AND (tmp = 1831.82 OR dew = 1831.82 OR slp = 295.3 OR wnd = 2236.72
       OR vis = 621.37 OR cig = 62.14 OR prp = 39.37);

-- The update rewrites every affected row, so reclaim the dead tuples afterwards.
-- Not inside the statement above because VACUUM cannot run in a transaction.
VACUUM (ANALYZE) observations;
