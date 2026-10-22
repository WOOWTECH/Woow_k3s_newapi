-- rc.44 model/log.go and model/audit_log.go: created_at is Unix seconds bigint.
-- A separate psql invocation commits each batch; never wrap the full loop.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
WITH picked AS (
    SELECT id FROM :"table"
    WHERE created_at < :cutoff
    ORDER BY created_at, id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
), deleted AS (
    DELETE FROM :"table" WHERE id IN (SELECT id FROM picked)
    RETURNING 1
)
SELECT count(*) FROM deleted;
COMMIT;
