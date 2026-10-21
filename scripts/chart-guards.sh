#!/usr/bin/env bash
# The chart's fail() guards for the newapi-proxy component (templates/proxy.yaml)
# must keep refusing unsafe values and keep accepting the real ones.
# Run by CI (.gitea/workflows/ci.yml); locally: scripts/chart-guards.sh
#
# Note: `helm lint` only logs a template fail() as [INFO]; `helm template`
# is what exits non-zero, so the cases render.
set -euo pipefail
shopt -s lastpipe  # check() runs as the last pipeline element: keep its rc=1
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fake_digest="sha256:$(printf 'a%.0s' $(seq 64))"
zero_digest="sha256:$(printf '0%.0s' $(seq 64))"
rc=0

# case <name> <pass|fail> <message substring or -> ; values (proxy: ...) on stdin
check() {
  local name=$1 expect=$2 want=$3 got
  cat > "$tmp/$name.yaml"
  if helm template newapi ./chart -n guard-test -f "$tmp/$name.yaml" > /dev/null 2> "$tmp/$name.err"; then
    got=pass
  else
    got=fail
  fi
  if [ "$got" != "$expect" ]; then
    echo "FAIL $name: expected $expect, got $got"; sed 's/^/  /' "$tmp/$name.err" | head -5; rc=1; return
  fi
  if [ "$want" != "-" ] && ! grep -qF -- "$want" "$tmp/$name.err"; then
    echo "FAIL $name: refused, but not with \"$want\""; sed 's/^/  /' "$tmp/$name.err" | head -5; rc=1; return
  fi
  echo "ok   $name ($got)"
}

head_() { printf 'proxy:\n  enabled: true\n  image:\n    digest: %s\n' "${1:-$fake_digest}"; }
odoo_client() {
  cat <<'EOF'
  clients:
    - name: c
      namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}
      podSelector: {matchLabels: {app.kubernetes.io/component: odoo}}
EOF
}

{ head_ "$zero_digest"; odoo_client; } | check zero-digest fail "all-zero placeholder"
{ head_ "sha256:abc"; odoo_client; } | check short-digest fail "proxy.image.digest must be"
{ printf 'proxy:\n  enabled: true\n  image:\n    digest: %s\n    tag: latest\n' "$fake_digest"; odoo_client; } | check tag-latest fail "40-hex commit sha"
head_ | check no-clients fail "proxy.clients must list"
{ head_; cat <<'EOF'; } | check empty-selectors fail "namespaceSelector needs non-empty"
  clients:
    - name: everything
      namespaceSelector: {matchLabels: {}}
      podSelector: {matchLabels: {}}
EOF
{ head_; cat <<'EOF'; } | check no-namespace-selector fail "namespaceSelector needs non-empty"
  clients:
    - name: nons
      podSelector: {matchLabels: {app.kubernetes.io/component: odoo}}
EOF
{ head_; cat <<'EOF'; } | check empty-pod-selector fail "podSelector needs non-empty"
  clients:
    - name: nopod
      namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}
      podSelector: {matchLabels: {}}
EOF
{ head_; cat <<'EOF'; } | check pod-without-component fail "must select app.kubernetes.io/component"
  clients:
    - name: nocomp
      namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}
      podSelector: {matchLabels: {app.kubernetes.io/instance: paas-odoo-platform}}
EOF
{ head_; cat <<'EOF'; } | check component-not-in fail "must select app.kubernetes.io/component"
  clients:
    - name: notin
      namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}
      podSelector:
        matchExpressions:
          - {key: app.kubernetes.io/component, operator: NotIn, values: [nginx]}
EOF
{ head_; cat <<'EOF'; } | check prod-shape pass -
  clients:
    - name: paas-odoo
      namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: paas-system
      podSelector:
        matchLabels:
          app.kubernetes.io/instance: paas-odoo-platform
        matchExpressions:
          - key: app.kubernetes.io/component
            operator: In
            values: [server, cron]
EOF

n=0
for rx in '^.+$' '^wp\d{10}$|^root$' '^w.*$' '^(wp|ws)\d{10}$' '^wp\S{10}$' '^wp\d{10}' '^[a-z]{2}\d{10}$'; do
  n=$((n + 1))
  { head_; printf "  usernameRegex: '%s'\n" "$rx"; odoo_client; } | check "wide-regex-$n" fail "proxy.usernameRegex must be"
done
for rx in '^wp\d{10}$' '^ws[a-z]{2}\d{10}$'; do
  n=$((n + 1))
  { head_; printf "  usernameRegex: '%s'\n" "$rx"; odoo_client; } | check "real-regex-$n" pass -
done

# NA-07: dual-track migration/retirement guards, legacy compatibility,
# rendered maintenance/network contracts and runtime failure paths.
# Optional real age tests run when NA07_AGE_BIN is set.
python3 -m unittest discover -s tests/maintenance -p test_maintenance.py -v || rc=1

exit $rc
