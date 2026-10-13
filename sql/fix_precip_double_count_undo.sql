-- Undo sql/fix_precip_double_count.sql: restore the FM-12 prp values it cleared.
--
-- Only rows still carrying their original id are restored, so this cannot undo
-- anything a later reload replaced -- and it does not need to, since those
-- years were never touched by the fix (it stops at 2025-01-01).
-- The backup table is kept; drop it by hand once the fix is settled.

BEGIN;

UPDATE observations o
SET prp = b.prp
FROM prp_fm12_backup_20261012 b
WHERE o.id = b.id;

COMMENT ON COLUMN public.observations.prp IS 'Precipitation in INCHES.';

COMMIT;

VACUUM (ANALYZE) observations;
