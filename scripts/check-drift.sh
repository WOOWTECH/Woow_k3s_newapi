#!/usr/bin/env bash
# Drift check for the manifests applied by hand, outside the Helm release
# (DESIGN §7.4): today the LiteLLM-side NetworkPolicies that let new-api reach
# svc203. If one of them drifts or disappears, every relay call fails — so this
# is the FIRST thing to run when the PaaS canary fails or type=5 errors rise
# (docs/OPERATIONS.md). Read-only: `kubectl diff` is a server-side dry run.
#
#   scripts/check-drift.sh [--context <ctx>] [file ...]
#
# Exit: 0 every file matches the cluster, 1 drift (diff printed), 2 error.
set -uo pipefail
cd "$(dirname "$0")/.."

ctx=()
if [ "${1:-}" = "--context" ]; then
  ctx=(--context "$2")
  shift 2
fi
files=("$@")
if [ ${#files[@]} -eq 0 ]; then
  files=(deploy/litellm-from-newapi-ingress.yaml deploy/litellm-from-newapi-stg-ingress.yaml)
fi

rc=0
for f in "${files[@]}"; do
  out=$(kubectl "${ctx[@]}" diff -f "$f" 2>&1)
  status=$?
  case $status in
    0) echo "ok     $f" ;;
    1)
      echo "DRIFT  $f (cluster differs from git; re-apply with: kubectl apply -f $f)"
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
