#!/usr/bin/env bash
# Daily Apporo New API content sync: WOOW new-api -> new-api-apporo
# (scripts/apporo_sync.py daily; docs/APPORO-SYNC.md). Meant for cluster1, run by
# deploy/apporo-sync/apporo-newapi-sync.timer or the crontab example. Not installed
# by anything in this repo.
#
# Settings come from $APPORO_SYNC_ENV (default ~/.config/apporo-newapi-sync/env,
# 0600; template deploy/apporo-sync/env.example):
#   APPORO_SYNC_KUBECTL    kubectl command line for woow-k3s (required)
#   APPORO_SYNC_STATE_DIR  holds, notices, alerts, journal (default ~/.local/state/apporo-newapi-sync)
#   APPORO_SYNC_MODE       report (default: classify, hold and alert; write nothing) | apply
#                          (start with report for a few days, read the alerts, then switch)
#   APPORO_SYNC_PLATFORM_PRICING  optional path of the platform's llm_pricing.py for the pricing gate
set -euo pipefail
umask 077

repo=$(cd "$(dirname "$0")/.." && pwd)
env_file=${APPORO_SYNC_ENV:-$HOME/.config/apporo-newapi-sync/env}
if [ -f "$env_file" ]; then
  if [ "$(stat -c %a "$env_file")" != 600 ]; then
    echo "apporo-sync-daily: $env_file must be mode 0600" >&2
    exit 2
  fi
  # shellcheck disable=SC1090
  . "$env_file"
fi
: "${APPORO_SYNC_KUBECTL:?set APPORO_SYNC_KUBECTL in $env_file}"
export APPORO_SYNC_KUBECTL
export APPORO_SYNC_STATE_DIR=${APPORO_SYNC_STATE_DIR:-$HOME/.local/state/apporo-newapi-sync}
mkdir -p -m 700 "$APPORO_SYNC_STATE_DIR"

args=()
if [ -n "${APPORO_SYNC_PLATFORM_PRICING:-}" ]; then
  args+=(--platform-pricing-module "$APPORO_SYNC_PLATFORM_PRICING")
fi
args+=(daily --live-images)
case "${APPORO_SYNC_MODE:-report}" in
  apply) args+=(--apply) ;;
  report) ;;
  *) echo "apporo-sync-daily: APPORO_SYNC_MODE must be apply or report" >&2; exit 2 ;;
esac

echo "apporo-sync-daily: $(date -Is) repo $(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo '?') mode ${APPORO_SYNC_MODE:-report}"
exec python3 "$repo/scripts/apporo_sync.py" "${args[@]}"
