#!/usr/bin/env bash
set -euo pipefail
: "${RETENTION_DAYS:?}" "${BATCH_SIZE:?}" "${BATCH_SLEEP_SECONDS:?}" "${MAX_BATCHES:?}"
for v in "$RETENTION_DAYS" "$BATCH_SIZE" "$MAX_BATCHES"; do [[ $v =~ ^[1-9][0-9]*$ ]]; done
[[ $BATCH_SLEEP_SECONDS =~ ^[0-9]+$ ]] && (( BATCH_SLEEP_SECONDS >= 1 && BATCH_SIZE <= 10000 ))
psql=(psql -X -qAt -v ON_ERROR_STOP=1)
# Fail closed on a missing/migrated schema (including timestamp rather than bigint).
"${psql[@]}" <<'SQL'
DO $$ BEGIN
  IF (SELECT count(*) FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name IN ('logs', 'audit_logs')
        AND column_name = 'created_at' AND data_type = 'bigint') <> 2 THEN
    RAISE EXCEPTION 'rc.44 logs/audit_logs.created_at bigint schema required';
  END IF;
END $$;
SQL
cutoff=$("${psql[@]}" -v days="$RETENTION_DAYS" <<'SQL'
SELECT floor(extract(epoch FROM current_timestamp))::bigint - (:days::bigint * 86400);
SQL
)
[[ $cutoff =~ ^[0-9]+$ ]]
for table in logs audit_logs; do
  total=0
  for ((batch=1; batch<=MAX_BATCHES; batch++)); do
    count=$("${psql[@]}" -v table="$table" -v cutoff="$cutoff" -v batch_size="$BATCH_SIZE" \
      -f "${MAINTENANCE_DIR:-/maintenance}/retention-batch.sql")
    [[ $count =~ ^[0-9]+$ ]] || { echo 'invalid batch count' >&2; exit 1; }
    total=$((total + count))
    echo "retention table=$table batch=$batch deleted=$count total=$total cutoff=$cutoff"
    (( count > 0 )) || break
    # Even a partial batch can have skipped locked rows; retry until zero.
    sleep "$BATCH_SLEEP_SECONDS"
  done
  (( batch <= MAX_BATCHES )) || { echo "retention batch limit reached: $table" >&2; exit 1; }
done
"${psql[@]}" -c 'SELECT pg_database_size(current_database())' |
  while IFS= read -r bytes; do echo "pg_database_size_bytes=$bytes"; done
