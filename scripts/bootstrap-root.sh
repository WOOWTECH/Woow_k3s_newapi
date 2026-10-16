#!/usr/bin/env bash
# Finish new-api's first-run wizard (POST /api/setup) through a port-forward,
# before the public hostname has DNS. Whoever calls /api/setup first becomes
# root, so this must run first.
#
# Usage: scripts/bootstrap-root.sh <base-url> [credentials-file]
#   base-url          e.g. http://127.0.0.1:3300 (kubectl port-forward)
#   credentials-file  default ~/.config/newapi/root-credentials (mode 0600)
# Env: NEWAPI_ROOT_USER (default woowroot, max 12 chars)
set -euo pipefail

base=${1:?usage: bootstrap-root.sh <base-url> [credentials-file]}
creds=${2:-$HOME/.config/newapi/root-credentials}
user=${NEWAPI_ROOT_USER:-woowroot}

status=$(curl -fsS "$base/api/setup")
if printf '%s' "$status" | grep -q '"status":true'; then
  echo "already initialised; nothing to do" >&2
  exit 0
fi

umask 077
mkdir -p "$(dirname "$creds")"
if [ -e "$creds" ]; then
  echo "refusing to overwrite $creds" >&2
  exit 1
fi
pass=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
printf 'url=%s\nusername=%s\npassword=%s\n' "${NEWAPI_PUBLIC_URL:-}" "$user" "$pass" > "$creds"

body=$(printf '{"username":"%s","password":"%s","confirmPassword":"%s","SelfUseModeEnabled":false,"DemoSiteEnabled":false}' "$user" "$pass" "$pass")
resp=$(curl -fsS -X POST -H 'Content-Type: application/json' --data "$body" "$base/api/setup")
if ! printf '%s' "$resp" | grep -q '"success":true'; then
  echo "setup failed: $resp" >&2
  rm -f "$creds"
  exit 1
fi
echo "root user '$user' created; credentials in $creds"
