#!/usr/bin/env bash
# Check Access gating and new-api route exposure without printing response bodies.
set -euo pipefail

usage() {
  echo "usage: scripts/check-exposure.sh [--key-file FILE --exercise-qwen QWEN_MODEL] <host-or-https-url>" >&2
}

fail() { echo "check-exposure: $*" >&2; exit 1; }

key_file=
exercise_model=
while (($#)); do
  case $1 in
    --key-file)
      (($# >= 2)) || { usage; exit 2; }
      key_file=$2; shift 2 ;;
    --exercise-qwen)
      (($# >= 2)) || { usage; exit 2; }
      exercise_model=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) break ;;
  esac
done
(($# == 1)) || { usage; exit 2; }
input=$1

if [[ $input == *://* ]]; then
  scheme=${input%%://*}
  authority=${input#*://}
  [[ $scheme == https || $scheme == http ]] || fail 'only HTTPS URLs are supported (HTTP is localhost-only)'
else
  scheme=https
  authority=$input
fi
[[ -n $authority && $authority != */* && $authority != *\?* && $authority != *\#* &&
   $authority != *@* && $authority != *[[:space:]]* ]] ||
  fail 'base URL must contain only a valid host and optional port (no userinfo, path, query, or fragment)'

host=
port=
if [[ $authority == \[* ]]; then
  [[ $authority =~ ^\[([^][]+)\](:([0-9]+))?$ ]] || fail 'invalid bracketed IPv6 host or port'
  host=${BASH_REMATCH[1]}
  port=${BASH_REMATCH[3]:-}
  [[ $host == ::1 ]] || { [[ $scheme != http ]] || fail 'plain HTTP is allowed only for localhost, 127.0.0.1, or ::1'; }
else
  [[ $authority != *\[* && $authority != *\]* ]] || fail 'invalid host brackets'
  if [[ $authority == *:* ]]; then
    [[ $authority != *:*:* ]] || fail 'IPv6 hosts must use brackets'
    host=${authority%%:*}
    port=${authority#*:}
    [[ $port =~ ^[0-9]+$ ]] || fail 'invalid port'
  else
    host=$authority
  fi
  [[ $host =~ ^[A-Za-z0-9.-]+$ && $host != .* && $host != *. && $host != *..* ]] || fail 'invalid host name'
  IFS=. read -r -a labels <<< "$host"
  for label in "${labels[@]}"; do
    [[ ${#label} -le 63 && $label =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || fail 'invalid host name'
  done
  if [[ $scheme == http ]]; then
    [[ $host == localhost || $host == 127.0.0.1 ]] ||
      fail 'plain HTTP is allowed only for localhost, 127.0.0.1, or ::1'
  fi
fi
if [[ -n $port ]]; then
  ((10#$port >= 1 && 10#$port <= 65535)) || fail 'port must be between 1 and 65535'
fi
if [[ $scheme == http && $authority == \[* && $host != ::1 ]]; then
  fail 'plain HTTP is allowed only for localhost, 127.0.0.1, or ::1'
fi
base="$scheme://$authority"

if [[ -n $exercise_model && -z $key_file ]]; then
  fail '--exercise-qwen requires --key-file'
fi
if [[ -n $key_file && -z $exercise_model ]]; then
  fail '--key-file requires --exercise-qwen MODEL; key-mode runs the Qwen streaming check'
fi
if [[ -n $exercise_model ]]; then
  [[ $exercise_model =~ ^[A-Za-z0-9._:-]+$ && ${exercise_model,,} == *qwen* ]] ||
    fail '--exercise-qwen model must be a safe model name containing Qwen'
fi

tmpdir=$(mktemp -d)
cleanup() { rm -rf -- "$tmpdir"; }
trap cleanup EXIT
curl_config=
if [[ -n $key_file ]]; then
  [[ -f $key_file ]] || fail 'key file is not a regular file'
  mode=$(stat -c '%a' -- "$key_file") || fail 'cannot inspect key file permissions'
  # Require exactly 0600 or a stricter mode (owner read-only); reject group/other bits.
  (( (8#$mode & 077) == 0 )) || fail 'key file must have mode 0600 or stricter'
  [[ -r $key_file && -s $key_file ]] || fail 'key file must be readable and non-empty'
  key=$(<"$key_file")
  [[ $key =~ ^[A-Za-z0-9._~-]+$ ]] || fail 'key file must contain one safe token line'
  curl_config="$tmpdir/curl.conf"
  (umask 077; printf 'header = "Authorization: Bearer %s"\n' "$key" > "$curl_config")
  chmod 600 "$curl_config"
  unset key
fi

curl_status() {
  local auth_mode=$1 method=$2 path=$3 output=$4; shift 4
  local -a cfg=()
  case $auth_mode in
    anonymous) ;;
    key) [[ -n $curl_config ]] || fail 'key auth mode requires --key-file'; cfg=(--config "$curl_config") ;;
    *) fail 'internal error: invalid curl auth mode' ;;
  esac
  local url="$base$path"
  local -a target=()
  if [[ $path == //* ]]; then
    url="$base/"
    target=(--request-target "$path")
  fi
  curl -q -sS --connect-timeout 5 --max-time 20 --path-as-is -o "$output" \
    -w '%{http_code}' -X "$method" "${cfg[@]}" "${target[@]}" --url "$url" "$@"
}

anon_paths=(/ /console /api/status /api/pricing /api/ratio_config /api/perf-metrics/summary)
for path in "${anon_paths[@]}"; do
  status=$(curl_status anonymous GET "$path" "$tmpdir/body") || fail "anonymous GET $path: transport error"
  case $status in
    302|403) echo "PASS anonymous $path: $status" ;;
    *) fail "anonymous GET $path expected Cloudflare Access block (302/403), got $status" ;;
  esac
done

status=$(curl_status anonymous POST /pg/chat/completions "$tmpdir/body" --data-binary '') ||
  fail 'anonymous POST /pg/chat/completions: transport error'
case $status in
  302|403) echo "PASS anonymous POST /pg/chat/completions: $status" ;;
  *) fail "anonymous POST /pg/chat/completions expected Cloudflare Access block (302/403), got $status" ;;
esac

if [[ -z $key_file ]]; then
  echo '略過已驗證 relay 測項'
  exit 0
fi

status=$(curl_status key GET /v1/models "$tmpdir/body") || fail 'authenticated GET /v1/models: transport error'
[[ $status == 200 ]] || fail "authenticated GET /v1/models expected 200, got $status"
echo "PASS authenticated /v1/models: $status"

denied_paths=(/v1/dashboard/billing/subscription /v1/tasks/x /v1/mj/submit/imagine /v1/video/generations/x /v1/../api/status /v1/%2e%2e/api/status //api/status)
for path in "${denied_paths[@]}"; do
  status=$(curl_status key GET "$path" "$tmpdir/body") || fail "authenticated GET $path: transport error"
  case $status in
    302|401|403|404) echo "PASS blocked $path: $status" ;;
    *) fail "authenticated GET $path expected blocked (302/401/403/404), got $status" ;;
  esac
done

for path in /v1 /v1/ /v1x /V1/models; do
  status=$(curl_status key GET "$path" "$tmpdir/body") || fail "authenticated GET $path: transport error"
  echo "EVIDENCE $path: $status"
done

if [[ -n $exercise_model ]]; then
  # This opt-in sends one short streaming request and buffers at most 64 KiB.
  stream_file="$tmpdir/stream"
  payload=$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8,"stream":true}' "$exercise_model")
  cfg=(--config "$curl_config")
  status=$(curl -q -sS --connect-timeout 5 --max-time 45 --path-as-is --max-filesize 65536 \
    -o "$stream_file" -w '%{http_code}' -H 'Content-Type: application/json' \
    -X POST "${cfg[@]}" --data-binary "$payload" "$base/v1/chat/completions") ||
      fail 'Qwen stream: transport error'
  [[ $status == 200 ]] || fail "Qwen stream expected 200, got $status"
  if ! tr -d '\r' < "$stream_file" | LC_ALL=C grep -aFxq 'data: [DONE]'; then
    fail "Qwen stream returned $status but had no SSE [DONE] terminator"
  fi
  echo "PASS Qwen stream: $status with SSE terminator"
fi
