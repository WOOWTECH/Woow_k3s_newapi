#!/usr/bin/env bash
# Drift check for the manifests applied by hand, outside the Helm release
# (DESIGN §7.4): today the LiteLLM-side NetworkPolicies that let new-api reach
# svc203. If one of them drifts or disappears, every relay call fails — so this
# is the FIRST thing to run when the PaaS canary fails or type=5 errors rise
# (docs/OPERATIONS.md). Read-only: `kubectl diff` is a server-side dry run.
#
#   scripts/check-drift.sh [--context <ctx>] [--apporo] [file ...]
#
# Without files: the two WOOW files. --apporo checks the Apporo instance's two
# files instead (docs/APPORO-K3S.md 〈手動套用的物件〉). Use it only once Apporo
# is live: before that its objects do not exist yet and show as DRIFT. The
# default run never looks at Apporo, whatever state its install is in.
# Exit: 0 every file matches the cluster, 1 drift (diff printed), 2 error.
set -uo pipefail
cd "$(dirname "$0")/.."

# Applied in install/switch order with owner consent, never by a bare re-apply.
APPORO_FILES=(deploy/litellm-from-newapi-apporo-ingress.yaml deploy/apun-odoo-to-newapi-apporo-egress.yaml)

ctx=()
apporo=0
while [ $# -gt 0 ]; do
  case $1 in
    --context) ctx=(--context "$2"); shift 2 ;;
    --apporo) apporo=1; shift ;;
    *) break ;;
  esac
done
files=("$@")
if [ ${#files[@]} -eq 0 ]; then
  if [ $apporo -eq 1 ]; then
    files=("${APPORO_FILES[@]}")
  else
    files=(deploy/litellm-from-newapi-ingress.yaml deploy/litellm-from-newapi-stg-ingress.yaml)
  fi
fi

rc=0
for f in "${files[@]}"; do
  out=$(kubectl "${ctx[@]}" diff -f "$f" 2>&1)
  status=$?
  case $status in
    0) echo "ok     $f" ;;
    1)
      if printf '%s\n' "${APPORO_FILES[@]}" | grep -qxF -- "$f"; then
        echo "DRIFT  $f (cluster differs from git, or not applied yet; Apporo object: apply only as docs/APPORO-K3S.md 〈手動套用的物件〉 says — order and owner consent)"
      else
        echo "DRIFT  $f (cluster differs from git; re-apply with: kubectl apply -f $f)"
      fi
      printf '%s\n' "$out" | sed 's/^/  /'
      [ $rc -eq 0 ] && rc=1
      ;;
    *)
      echo "ERROR  $f (kubectl diff exit $status)"
      printf '%s\n' "$out" | head -20 | sed 's/^/  /'
      rc=2
      ;;
  esac
done
exit $rc
