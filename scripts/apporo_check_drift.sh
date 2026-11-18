#!/usr/bin/env bash
# Drift check for Apporo's two hand-applied manifests (docs/APPORO-K3S.md
# 〈手動套用的物件〉): WOOW's scripts/check-drift.sh run on Apporo's files.
# Read-only: `kubectl diff` is a server-side dry run.
#
#   scripts/apporo_check_drift.sh [--context <ctx>] [file ...]
#
# Without files: both Apporo files. Use it once Apporo is live (docs/APPORO-K3S.md
# 〈apun 那一側〉 done): before that its objects do not exist yet and show as
# DRIFT; mid-install, name the one file already applied.
#
# An Apporo DRIFT line never says to re-apply: an Apporo object is applied only
# in the order, and with the owner consent, that docs/APPORO-K3S.md gives.
# Apporo's own script, so scripts/check-drift.sh and its default run stay
# WOOW's alone (owner decision 2026-11-17: Apporo never turns a WOOW change
# red). scripts/tests/test_apporo_check_drift.py checks this wrapper in CI's
# "Apporo checks" step: if a WOOW change to check-drift.sh breaks it, that
# change only gets a warning and an Apporo PR follows.
#
# Exit: scripts/check-drift.sh's (0 every file matches the cluster, 1 drift, 2 error).
set -uo pipefail
cd "$(dirname "$0")/.."

ctx=()
if [ "${1:-}" = "--context" ]; then
  ctx=(--context "$2")
  shift 2
fi
files=("$@")
if [ ${#files[@]} -eq 0 ]; then
  files=(deploy/litellm-from-newapi-apporo-ingress.yaml deploy/apun-odoo-to-newapi-apporo-egress.yaml)
fi

hint='cluster differs from git, or not applied yet; Apporo object: apply only as docs/APPORO-K3S.md 〈手動套用的物件〉 says — order and owner consent'
out=$(scripts/check-drift.sh "${ctx[@]}" "${files[@]}")
rc=$?
# check-drift.sh ends each DRIFT line with "(...; re-apply with: kubectl apply -f <file>)".
printf '%s\n' "$out" | sed "s|^DRIFT  \([^ ]*\) (.*)\$|DRIFT  \1 ($hint)|"
exit $rc
