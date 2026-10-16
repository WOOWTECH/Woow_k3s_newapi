#!/usr/bin/env bash
# Create (or reuse) a remotely-managed Cloudflare tunnel for new-api, point
# its ingress at the in-cluster Service, and store the connector token in the
# Secret the chart's cloudflared Deployment reads. DNS is a separate step
# (dns) so the hostname only resolves after scripts/bootstrap-root.sh ran.
#
# Usage:
#   scripts/cf-tunnel.sh tunnel   # create tunnel + ingress + k8s Secret
#   scripts/cf-tunnel.sh dns      # create/refresh the proxied CNAME
#   scripts/cf-tunnel.sh show     # print tunnel id, status, connections
# Env:
#   CF_API_TOKEN_FILE  file holding the API token (never commit; mode 0600)
#   CF_ACCOUNT_ID      default: woowtech account
#   CF_ZONE_ID         default: woowtech.io
#   HOSTNAME_FQDN      default: newapi.woowtech.io
#   TUNNEL_NAME        default: woow-k3s-new-api
#   NAMESPACE          default: new-api
#   SERVICE_URL        default: http://newapi.new-api.svc.cluster.local:3000
#   SECRET_NAME        default: newapi-cloudflared
set -euo pipefail

cmd=${1:?usage: cf-tunnel.sh tunnel|dns|show}
: "${CF_API_TOKEN_FILE:?set CF_API_TOKEN_FILE}"
acct=${CF_ACCOUNT_ID:-9c27f623ee596e0b67be56263bcb1974}
zone=${CF_ZONE_ID:-4d12cea85ec6022e42b72015c43e5513}
host=${HOSTNAME_FQDN:-newapi.woowtech.io}
name=${TUNNEL_NAME:-woow-k3s-new-api}
ns=${NAMESPACE:-new-api}
svc=${SERVICE_URL:-http://newapi.new-api.svc.cluster.local:3000}
secret=${SECRET_NAME:-newapi-cloudflared}
api=https://api.cloudflare.com/client/v4

cf() {
  local method=$1 path=$2; shift 2
  curl -sS --fail-with-body -X "$method" -H "Authorization: Bearer $(cat "$CF_API_TOKEN_FILE")" \
    -H 'Content-Type: application/json' "$api$path" "$@"
}
ok() { python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("success") else (print(d.get("errors"), file=sys.stderr) or 1))'; }

tunnel_id() {
  cf GET "/accounts/$acct/cfd_tunnel?name=$name&is_deleted=false" |
    python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")'
}

case $cmd in
tunnel)
  tmp_manifest=$(mktemp)
  chmod 600 "$tmp_manifest"
  id=$(tunnel_id)
  if [ -z "$id" ]; then
    id=$(cf POST "/accounts/$acct/cfd_tunnel" --data "{\"name\":\"$name\",\"config_src\":\"cloudflare\"}" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])')
    echo "created tunnel $name ($id)"
  else
    echo "reusing tunnel $name ($id)"
  fi
  cf PUT "/accounts/$acct/cfd_tunnel/$id/configurations" --data "$(python3 -c '
import json,sys
host,svc=sys.argv[1:3]
print(json.dumps({"config":{"ingress":[
  {"hostname":host,"service":svc,"originRequest":{"connectTimeout":10,"keepAliveTimeout":90}},
  {"service":"http_status:404"}]}}))' "$host" "$svc")" | ok
  echo "ingress: $host -> $svc"
  cf GET "/accounts/$acct/cfd_tunnel/$id/token" |
    python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["result"])' |
    kubectl -n "$ns" create secret generic "$secret" --from-file=token=/dev/stdin \
      --dry-run=client -o yaml > "$tmp_manifest"
  # replace/create, never apply: apply would copy the token into the
  # last-applied-configuration annotation.
  if kubectl -n "$ns" get secret "$secret" > /dev/null 2>&1; then
    kubectl -n "$ns" replace -f "$tmp_manifest" > /dev/null
  else
    kubectl -n "$ns" create -f "$tmp_manifest" > /dev/null
  fi
  rm -f "$tmp_manifest"
  echo "secret $ns/$secret updated"
  ;;
dns)
  id=$(tunnel_id)
  [ -n "$id" ] || { echo "no tunnel $name; run 'tunnel' first" >&2; exit 1; }
  target="$id.cfargotunnel.com"
  rec=$(cf GET "/zones/$zone/dns_records?name=$host" |
    python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')
  body="{\"type\":\"CNAME\",\"name\":\"$host\",\"content\":\"$target\",\"proxied\":true,\"comment\":\"woow-k3s ns $ns new-api (Woow_k3s_newapi)\"}"
  if [ -z "$rec" ]; then
    cf POST "/zones/$zone/dns_records" --data "$body" | ok
    echo "created CNAME $host -> $target"
  else
    cf PUT "/zones/$zone/dns_records/$rec" --data "$body" | ok
    echo "updated CNAME $host -> $target"
  fi
  ;;
show)
  id=$(tunnel_id)
  [ -n "$id" ] || { echo "no tunnel $name" >&2; exit 1; }
  cf GET "/accounts/$acct/cfd_tunnel/$id" | python3 -c '
import json,sys
r=json.load(sys.stdin)["result"]
print(r["id"], r["name"], r["status"], "connections:", len(r.get("connections") or []))'
  ;;
*)
  echo "unknown command $cmd" >&2; exit 2 ;;
esac
