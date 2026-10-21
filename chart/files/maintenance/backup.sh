#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${AGE_RECIPIENT:?public age recipient required}"
: "${KEEP_DAYS:?}" "${CAPACITY_WARNING_PERCENT:?}"
backup_dir=${BACKUP_DIR:-/backups}
[[ $KEEP_DAYS =~ ^[1-9][0-9]*$ ]]
[[ $CAPACITY_WARNING_PERCENT =~ ^[1-9][0-9]*$ ]] && (( CAPACITY_WARNING_PERCENT <= 100 ))
# Validate recipient/tooling before connecting or touching the backup PVC.
command -v age >/dev/null
command -v pg_dump >/dev/null
i=0
until pg_isready -q; do
  i=$((i+1))
  (( i < 60 )) || { echo 'postgres unreachable after 60 s' >&2; exit 1; }
  sleep 1
done
find "$backup_dir" -maxdepth 1 -type f -name 'newapi-*.part' -mmin +120 -print -delete
f="$backup_dir/newapi-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump.age"
trap 'rm -f "$f.part"' EXIT
trap 'exit 1' HUP INT TERM
# pipefail is essential: age can succeed even when pg_dump fails halfway.
# No plaintext archive is ever written to a PVC or an emptyDir.
pg_dump -Fc | age -r "$AGE_RECIPIENT" -o "$f.part"
[[ -s $f.part ]]
mv "$f.part" "$f"
echo "wrote $f ($(wc -c < "$f") encrypted bytes)"
# -mmin rounds down; +N-1 expires files at >= N whole minutes, not day 8.
# Only prune after a successful encrypted dump, including legacy plaintext.
find "$backup_dir" -maxdepth 1 -type f \( -name 'newapi-*.dump.age' -o -name 'newapi-*.dump' \) \
  -mmin "+$((KEEP_DAYS * 1440 - 1))" -print -delete
psql -X -v ON_ERROR_STOP=1 -Atc 'SELECT pg_database_size(current_database())' |
  while IFS= read -r bytes; do echo "pg_database_size_bytes=$bytes"; done
used=$(df -Pk "$backup_dir" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
[[ $used =~ ^[0-9]+$ ]] || { echo 'cannot read backup PVC utilization' >&2; exit 1; }
echo "backup_pvc_used_percent=$used threshold=$CAPACITY_WARNING_PERCENT"
if (( used > CAPACITY_WARNING_PERCENT )); then
  echo 'backup PVC capacity threshold exceeded' >&2
  exit 1
fi
