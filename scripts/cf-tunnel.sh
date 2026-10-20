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

tmp_dir=
cleanup() {
  if [[ -n "$tmp_dir" && -d "$tmp_dir" ]]; then
    rm -rf -- "$tmp_dir"
  fi
}
trap cleanup EXIT

url_encode() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
json_body() {
  python3 -c 'import json,sys
mode=sys.argv[1]
if mode == "tunnel":
 print(json.dumps({"name":sys.argv[2],"config_src":"cloudflare"}, separators=(",",":")))
elif mode == "ingress":
 host,svc=sys.argv[2:4]
 print(json.dumps({"config":{"ingress":[
  {"hostname":host,"service":svc,"originRequest":{"connectTimeout":10,"keepAliveTimeout":90}},
  {"service":"http_status:404"}]}}, separators=(",",":")))
elif mode == "dns":
 host,target,ns=sys.argv[2:5]
 print(json.dumps({"type":"CNAME","name":host,"content":target,"proxied":True,
                   "comment":f"woow-k3s ns {ns} new-api (Woow_k3s_newapi)"}, separators=(",",":")))
' "$@"
}

# Open the token once through a no-follow descriptor, validate that descriptor,
# and write the private curl config from that same in-memory value.
write_curl_config() {
  python3 - "$CF_API_TOKEN_FILE" "$1" <<'PY'
import os, re, stat, sys
src, dst = sys.argv[1:]

TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{20,512}\Z")
MAX_TOKEN_BYTES = 512
MAX_RAW_BYTES = MAX_TOKEN_BYTES + 2  # optional final LF plus overflow sentinel

def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)

if not hasattr(os, "O_NOFOLLOW"):
    fail("secure token-file open is unavailable on this platform")

read_flags = os.O_RDONLY | os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    read_flags |= os.O_CLOEXEC
if hasattr(os, "O_NONBLOCK"):
    read_flags |= os.O_NONBLOCK

try:
    fd = os.open(src, read_flags)
except OSError:
    fail("CF_API_TOKEN_FILE must be a readable regular file with mode 0600 or stricter")

try:
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or
            not st.st_mode & stat.S_IRUSR):
        fail("CF_API_TOKEN_FILE must be a readable regular file with mode 0600 or stricter")
    raw = os.read(fd, MAX_RAW_BYTES)
except OSError:
    fail("CF_API_TOKEN_FILE must be a readable regular file with mode 0600 or stricter")
finally:
    try:
        os.close(fd)
    except OSError:
        pass

if len(raw) > MAX_TOKEN_BYTES + 1:
    fail("CF_API_TOKEN_FILE contains an invalid token")
if raw.endswith(b"\n"):
    raw = raw[:-1]
try:
    token = raw.decode("ascii")
except UnicodeDecodeError:
    fail("CF_API_TOKEN_FILE contains an invalid token")
if not TOKEN_RE.fullmatch(token):
    fail("CF_API_TOKEN_FILE contains an invalid token")

# The validated alphabet cannot add curl config directives or quotes.
write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    write_flags |= os.O_CLOEXEC
out_fd = None
try:
    out_fd = os.open(dst, write_flags, 0o600)
    os.fchmod(out_fd, 0o600)
    with os.fdopen(out_fd, "w", encoding="ascii") as f:
        out_fd = None
        f.write('header = "Authorization: Bearer ' + token + '"\n')
        f.write('header = "Content-Type: application/json"\n')
except (OSError, UnicodeError, ValueError):
    try:
        os.unlink(dst)
    except OSError:
        pass
    fail("could not create private curl configuration")
finally:
    if out_fd is not None:
        try:
            os.close(out_fd)
        except OSError:
            pass
PY
}

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/cf-tunnel.XXXXXXXX")
chmod 700 "$tmp_dir"
curl_config=$tmp_dir/curl.conf
write_curl_config "$curl_config"

cf() {
  local method=$1 path=$2
  shift 2
  curl -q --config "$curl_config" --noproxy '*' -sS --fail-with-body -X "$method" "$api$path" "$@"
}
ok() { python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("success") else (print(d.get("errors"), file=sys.stderr) or 1))'; }

tunnel_id() {
  local qname
  qname=$(url_encode "$name")
  cf GET "/accounts/$acct/cfd_tunnel?name=$qname&is_deleted=false" |
    python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")'
}

case $cmd in
tunnel)
  id=$(tunnel_id)
  if [ -z "$id" ]; then
    body=$(json_body tunnel "$name")
    id=$(cf POST "/accounts/$acct/cfd_tunnel" --data "$body" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])')
    echo "created tunnel $name ($id)"
  else
    echo "reusing tunnel $name ($id)"
  fi
  body=$(json_body ingress "$host" "$svc")
  cf PUT "/accounts/$acct/cfd_tunnel/$id/configurations" --data "$body" | ok
  echo "ingress: $host -> $svc"
  cf GET "/accounts/$acct/cfd_tunnel/$id/token" |
    python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["result"])' |
    kubectl -n "$ns" create secret generic "$secret" --from-file=token=/dev/stdin \
      --dry-run=client -o yaml > "$tmp_dir/secret.yaml"
  # Replace/create, never apply: apply would copy the token into the
  # last-applied-configuration annotation.
  if kubectl -n "$ns" get secret "$secret" > /dev/null 2>&1; then
    kubectl -n "$ns" replace -f "$tmp_dir/secret.yaml" > /dev/null
  else
    kubectl -n "$ns" create -f "$tmp_dir/secret.yaml" > /dev/null
  fi
  echo "secret $ns/$secret updated"
  ;;
dns)
  id=$(tunnel_id)
  [ -n "$id" ] || { echo "no tunnel $name; run 'tunnel' first" >&2; exit 1; }
  target="$id.cfargotunnel.com"
  qhost=$(url_encode "$host")
  rec=$(cf GET "/zones/$zone/dns_records?name=$qhost" |
    python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')
  body=$(json_body dns "$host" "$target" "$ns")
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
