#!/usr/bin/env bash
# Compare this chart with what is running. Exit 0 = in sync, 1 = drift.
#   1. repo    vs release : helm template (this repo) <-> helm get manifest
#   2. cluster vs chart   : kubectl diff of the rendered chart against live objects
# Extra arguments are passed to `helm template`, e.g. -f my-values.yaml.
#   CONTEXT=woow-k3s RELEASE=newapi NAMESPACE=new-api scripts/check-drift.sh
set -euo pipefail

CONTEXT="${CONTEXT:-woow-k3s}"
RELEASE="${RELEASE:-newapi}"
NAMESPACE="${NAMESPACE:-new-api}"
cd "$(dirname "$0")/.."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

helm template "$RELEASE" . -n "$NAMESPACE" --skip-tests "$@" > "$tmp/repo.yaml"
helm --kube-context "$CONTEXT" get manifest "$RELEASE" -n "$NAMESPACE" > "$tmp/release.yaml"

rc=0
# -B: helm get manifest ends with an extra blank line that helm template does not.
if diff -u -B "$tmp/release.yaml" "$tmp/repo.yaml" > "$tmp/repo.diff"; then
  echo "1. repo == release ${RELEASE}"
else
  echo "1. DRIFT: this repo renders differently from release ${RELEASE}:"
  cat "$tmp/repo.diff"
  rc=1
fi

set +e
kubectl --context "$CONTEXT" diff -f "$tmp/repo.yaml" > "$tmp/live.diff" 2>&1
krc=$?
set -e
case "$krc" in
  0) echo "2. cluster == chart (context ${CONTEXT})" ;;
  1) echo "2. DRIFT: live objects differ from the chart:"; cat "$tmp/live.diff"; rc=1 ;;
  *) cat "$tmp/live.diff" >&2; exit "$krc" ;;
esac
exit "$rc"
