#!/usr/bin/env python3
"""Sandbox integration test for scripts/apporo_sync.py (docs/APPORO-SYNC.md 〈已驗證〉).

Manual, not CI. Every write goes to two throwaway namespaces
dev-sandbox-nasync-{src,tgt}-<suffix>; WOOW production (new-api) is only read,
by the tool's own read-only export. Run the phases in order:

  export APPORO_SYNC_KUBECTL="kubectl --context woow-k3s"   # plus --server/... if needed
  export APPORO_SYNC_SANDBOX_NODE=<node that caches the new-api/postgres digests>
  python3 tests/apporo_sync/sandbox.py up            # namespaces, Postgres, New API, mock upstream
  python3 tests/apporo_sync/sandbox.py setup         # /api/setup root on both, sandbox Secrets
  python3 tests/apporo_sync/sandbox.py export-woow   # read-only export of WOOW production
  python3 tests/apporo_sync/sandbox.py seed-source   # replay it 1:1 (mirror profile) + fingerprints
  python3 tests/apporo_sync/sandbox.py replay-target # Apporo profile replay, verify, idempotency
  python3 tests/apporo_sync/sandbox.py quota         # WOOW charge / 30 through a mock upstream
  python3 tests/apporo_sync/sandbox.py daily         # classification, holds, notice, maturity, write order
  python3 tests/apporo_sync/sandbox.py rbac          # daily as the deploy/apporo-sync/rbac.yaml identity
  python3 tests/apporo_sync/sandbox.py mint-revoke   # a failed Secret write revokes the new PAT
  python3 tests/apporo_sync/sandbox.py platform <llm_pricing.py>
  python3 tests/apporo_sync/sandbox.py down          # delete both namespaces (confirm NotFound)

Output (export, state, journal; no secret) goes to $APPORO_SYNC_SANDBOX_OUT
(default ~/.local/state/apporo-newapi-sync-sandbox/<suffix>), never into the repo.
To test without reading WOOW production again, copy an earlier export-woow result
(woow-prod-export.json and woow-prod-fingerprints.json) into that directory and
skip export-woow. The throwaway sandbox credentials (0600 sandbox-secrets.json)
are deleted by `down`.
"""
import base64
import datetime
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apporo_sync as aps  # noqa: E402
import newapi_admin as na  # noqa: E402
import newapi_content as nc  # noqa: E402
sys.path.insert(0, HERE)
import manifests  # noqa: E402

KUBECTL = shlex.split(os.environ.get("APPORO_SYNC_KUBECTL", "kubectl"))
SUFFIX = os.environ.get("APPORO_SYNC_SANDBOX_SUFFIX", datetime.date.today().strftime("%m%d"))
SRC, TGT = f"dev-sandbox-nasync-src-{SUFFIX}", f"dev-sandbox-nasync-tgt-{SUFFIX}"
OUT = os.path.expanduser(os.environ.get("APPORO_SYNC_SANDBOX_OUT",
                                        f"~/.local/state/apporo-newapi-sync-sandbox/{SUFFIX}"))
SECRETS_FILE = os.path.join(OUT, "sandbox-secrets.json")
WOOW_EXPORT = os.path.join(OUT, "woow-prod-export.json")
STATE = os.path.join(OUT, "state")
LITELLM = "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000"
ENV = dict(os.environ, APPORO_SYNC_KUBECTL=" ".join(KUBECTL))
FP_SQL = nc.READ_ONLY_PREAMBLE + """SELECT json_build_object(
'options', (SELECT json_build_array(count(*), encode(sha256(convert_to(string_agg(key||'='||value, E'\\n' ORDER BY key),'UTF8')),'hex')) FROM options WHERE key NOT IN ('ServerAddress','passkey.origins','passkey.legacy_rp_ids','passkey.rp_id','LegacyAccessTokenRetireAt')),
'abilities', (SELECT json_build_array(count(*), encode(sha256(convert_to(string_agg(concat_ws('|',"group",model,channel_id,enabled,priority,weight,coalesce(tag,'')), E'\\n' ORDER BY "group",model,channel_id),'UTF8')),'hex')) FROM abilities),
'channels', (SELECT json_build_array(count(*), encode(sha256(convert_to(string_agg(concat_ws('|', id, type, name, status, coalesce(base_url,''), "group", coalesce(models,''), coalesce(model_mapping,''), coalesce(status_code_mapping,''), coalesce(priority,0), coalesce(weight,0), coalesce(auto_ban,0), coalesce(tag,''), coalesce(test_model,''), coalesce(open_ai_organization,''), coalesce(other,''), coalesce(other_info,''), coalesce(setting,''), coalesce(param_override,''), coalesce(header_override,''), coalesce(remark,''), coalesce(channel_info::text,''), coalesce(settings,'')), E'\\n' ORDER BY id),'UTF8')),'hex')) FROM channels)
)::text;"""
# The two fixed SQL texts of this harness are pinned (newapi_content.check_pinned_sql):
# reviewed once; any edit must update the pin. FP_SQL returns counts and sha256 only.
FP_SQL_SHA256 = "bd2d2298662f5df1d9fe2844f603f27272b183d73b7ce8da787d2729891411e6"


def k(*args, data=None, check=True):
    r = subprocess.run(KUBECTL + list(args), input=data, capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args[:4])}...: {r.stderr.decode()[-400:]}")
    return r


def guard(ns):
    assert ns.startswith("dev-sandbox-"), ns


def load_secrets():
    with open(SECRETS_FILE) as f:
        return json.load(f)


def save_secrets(d):
    fd = os.open(SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def create_secret(ns, name, data):
    guard(ns)
    m = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": name, "namespace": ns},
         "data": {kk: base64.b64encode(v.encode()).decode() for kk, v in data.items()}}
    k("-n", ns, "delete", "secret", name, "--ignore-not-found")
    k("create", "-f", "-", data=json.dumps(m).encode())


def tool(*args, check=True, show=True):
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "apporo_sync.py"), "--state-dir", STATE, *args],
                       capture_output=True, text=True, env=ENV)
    text = r.stdout + r.stderr
    if show:
        print(text)
    if check and r.returncode != 0:
        raise SystemExit(f"tool {args[:3]} exited {r.returncode}")
    return r.returncode, text


def profiles():
    with open(os.path.join(REPO, "deploy", "apporo-sync", "profile.json")) as f:
        real = json.load(f)
    apporo = json.loads(json.dumps(real))
    apporo["source"]["namespace"], apporo["target"]["namespace"] = SRC, TGT
    mirror = json.loads(json.dumps(real))
    mirror["target"] = dict(real["target"], namespace=SRC, server_address="https://newapi-sandbox-source.invalid")
    mirror.update(price_divisor=1, divided_maps=[], target_options={})
    mirror.pop("group_labels")
    mirror.pop("platform")
    mirror["channels"] = {"1": {"key_slot": "api"}, "2": {"key_slot": "internal"}, "3": {"key_slot": "paid"},
                          "4": {"key_slot": "api"}}
    mirror["excluded_channels"] = {}
    mirror["key_secret"] = {"name": "newapi-upstream-keys", "slots": {"api": "api", "paid": "paid", "internal": "internal"}}
    paths = {}
    for name, p in (("apporo", apporo), ("mirror", mirror)):
        nc.validate_profile(p)
        paths[name] = os.path.join(OUT, f"profile-{name}.json")
        with open(paths[name], "w") as f:
            json.dump(p, f, indent=1)
    return paths


def fingerprints(ns, pod="newapi-postgres-0"):
    """Research fingerprints (hashes only): a sandbox namespace, or WOOW production read-only."""
    if not ns.startswith("dev-sandbox-") and ns != "new-api":
        raise SystemExit(f"fingerprints: {ns} is neither a sandbox nor WOOW production (read-only)")
    nc.check_pinned_sql(FP_SQL, FP_SQL_SHA256)
    psql = nc.Psql(KUBECTL, ns, pod)
    r = subprocess.run(psql.argv(), input=FP_SQL.encode(), capture_output=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(r.stderr.decode()[-300:])
    return json.loads([ln for ln in r.stdout.decode().splitlines() if ln.strip()][-1])


def root_client(ns, base):
    s = load_secrets()[ns]
    return na.Client(base, creds={"username": s["user"], "password": s["password"]})


def phase_up():
    with open(os.path.join(HERE, "mock_llm.py")) as f:
        mock = f.read()
    sec = {}
    for ns, lang in ((SRC, "zh-TW"), (TGT, "en")):
        guard(ns)
        k("create", "namespace", ns)
        k("label", "namespace", ns, "app.kubernetes.io/part-of=apporo-sync-sandbox")
        create_secret(ns, "newapi-secrets", {"POSTGRES_PASSWORD": secrets.token_urlsafe(24),
                                             "SESSION_SECRET": secrets.token_urlsafe(32)})
        lst = {"apiVersion": "v1", "kind": "List", "items": manifests.objects(ns, lang, mock)}
        k("apply", "-f", "-", data=json.dumps(lst).encode())
        sec[ns] = {}
    save_secrets(sec)
    for ns in (SRC, TGT):
        for obj in ("statefulset/newapi-postgres", "deployment/newapi-master", "deployment/mock-llm"):
            k("-n", ns, "rollout", "status", obj, "--timeout=600s")
    print("up: both sandboxes ready")


def phase_setup():
    sec = load_secrets()
    for ns in (SRC, TGT):
        with aps.PortForward(KUBECTL, ns) as base:
            pw = secrets.token_urlsafe(24)
            body = json.dumps({"username": "sbxroot", "password": pw, "confirmPassword": pw,
                               "SelfUseModeEnabled": False, "DemoSiteEnabled": False}).encode()
            req = urllib.request.Request(base + "/api/setup", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": na.UA})
            with na._OPENER.open(req, timeout=30) as resp:
                ans = json.loads(resp.read())
            assert ans.get("success"), ans.get("message")
            sec[ns] = {"user": "sbxroot", "password": pw}
            save_secrets(sec)
            create_secret(ns, "newapi-root-credentials", {"url": base, "username": "sbxroot", "password": pw})
            keys = {"api": "sk-sbx-" + secrets.token_hex(12), "paid": "sk-sbx-" + secrets.token_hex(12),
                    "internal": "sk-sbx-" + secrets.token_hex(12)}
            create_secret(ns, "newapi-upstream-keys", keys)
            print(f"setup {ns}: root created by /api/setup, Secrets newapi-root-credentials, newapi-upstream-keys")


def phase_export_woow():
    os.makedirs(OUT, exist_ok=True)
    profiles()
    if os.path.exists(WOOW_EXPORT):
        os.unlink(WOOW_EXPORT)
    tool("--profile", os.path.join(REPO, "deploy", "apporo-sync", "profile.json"), "export", "--side", "source",
         "--out", WOOW_EXPORT)
    fp = fingerprints("new-api")
    with open(os.path.join(OUT, "woow-prod-fingerprints.json"), "w") as f:
        json.dump(fp, f, indent=1)
    print("WOOW prod research fingerprints (read-only):", json.dumps(fp))


def phase_seed_source():
    p = profiles()
    tool("--profile", p["mirror"], "--source-snapshot", WOOW_EXPORT, "--auth", "password", "--state-dir",
         os.path.join(OUT, "state-mirror"), "replay", "--yes-bootstrap")
    tool("--profile", p["mirror"], "--source-snapshot", WOOW_EXPORT, "verify", "--strict", "--check-keys")
    with open(os.path.join(OUT, "woow-prod-fingerprints.json")) as f:
        woow = json.load(f)
    sbx = fingerprints(SRC)
    print("sandbox source fingerprints:", json.dumps(sbx))
    for part in ("options", "abilities", "channels"):
        print(f"  {part}: WOOW {woow[part]} sandbox {sbx[part]} -> {'EQUAL' if woow[part] == sbx[part] else 'DIFFERENT'}")


def phase_replay_target():
    p = profiles()
    tool("--profile", p["apporo"], "--auth", "password", "replay", "--yes-bootstrap")
    tool("--profile", p["apporo"], "verify", "--strict", "--check-keys", "-v")
    rc, text = tool("--profile", p["apporo"], "plan", "--bootstrap")
    assert "plan: 0 auto" in text, "a finished replay leaves nothing to copy"
    rc, text = tool("--profile", p["apporo"], "--auth", "password", "replay", "--yes-bootstrap", check=False)
    assert rc == 2 and "not a fresh install" in text, "a second replay must be refused"
    rc, text = tool("--profile", p["apporo"], "plan")
    src, tgt = fingerprints(SRC), fingerprints(TGT)
    print("target research fingerprints:", json.dumps(tgt))
    src_rows = nc.read_snapshot(nc.Psql(KUBECTL, SRC), json.load(open(p["apporo"])))
    tgt_rows = nc.read_snapshot(nc.Psql(KUBECTL, TGT), json.load(open(p["apporo"])))
    mapped = [r for r in src_rows["abilities"] if r["channel_id"] in (1, 3, 4)]
    print("abilities source(1,3,4):", nc.abilities_fingerprint(mapped), "target:", nc.abilities_fingerprint(tgt_rows["abilities"]))
    print("ModelRatio target == source/30 (Go bytes):",
          tgt_rows["options"]["ModelRatio"] == nc.divide_map(src_rows["options"]["ModelRatio"], "ModelRatio", 30))
    print("ModelPrice target == source/30 (Go bytes):",
          tgt_rows["options"]["ModelPrice"] == nc.divide_map(src_rows["options"]["ModelPrice"], "ModelPrice", 30))


def api_call(client, method, path, body=None):
    ok, msg, ans = aps.api(client, method, path, body)
    if not ok:
        raise SystemExit(f"{method} {path}: {msg}")
    return ans


def make_token(client):
    api_call(client, "POST", "/api/token/", {"name": "sbx-quota", "remain_quota": 0, "unlimited_quota": True,
                                              "expired_time": -1, "group": "default"})
    ans = client.req("GET", "/api/token/?p=1&page_size=10")
    items = ans["data"]["items"] if isinstance(ans.get("data"), dict) else ans["data"]
    tid = [t for t in items if t["name"] == "sbx-quota"][-1]["id"]
    key = api_call(client, "POST", f"/api/token/{tid}/key")["data"]["key"]
    return key if key.startswith("sk-") else "sk-" + key


def chat(base, token, model):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hello"}], "stream": False}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                          "User-Agent": na.UA})
    with na._OPENER.open(req, timeout=60) as resp:
        return json.loads(resp.read())


LOG_SQL = nc.READ_ONLY_PREAMBLE + ("SELECT coalesce(json_agg(x), '[]')::text FROM (SELECT quota, prompt_tokens, completion_tokens, "
                                   "model_name, channel_id, other FROM logs WHERE type = 2 ORDER BY id DESC LIMIT 1) x;")
LOG_SQL_SHA256 = "0c4e9d69c4268b543392d6a0b03386af487d92f87e09116f6c8a940ed7c67a30"


def last_log(ns):
    guard(ns)                                   # the logs table: sandbox namespaces only
    nc.check_pinned_sql(LOG_SQL, LOG_SQL_SHA256)
    psql = nc.Psql(KUBECTL, ns)
    r = subprocess.run(psql.argv(), input=LOG_SQL.encode(), capture_output=True, timeout=120)
    return json.loads([ln for ln in r.stdout.decode().splitlines() if ln.strip()][-1])[0]


def set_source_base_url(url):
    with aps.PortForward(KUBECTL, SRC) as base:
        c = root_client(SRC, base)
        try:
            api_call(c, "PUT", "/api/channel/", {"id": 1, "base_url": url})
        finally:
            c.close()


def phase_quota():
    p = profiles()
    set_source_base_url("http://mock-llm:4000")
    tool("--profile", p["apporo"], "--auth", "password", "replay", "--yes-bootstrap", "--force", "--why",
         "sandbox quota test: point channel 1 at the mock upstream")
    time.sleep(65)  # rc.44 refreshes channel and option caches once a minute (SYNC_FREQUENCY)
    model = "anthropic/claude-sonnet-5.5"
    result = {}
    for ns in (SRC, TGT):
        with aps.PortForward(KUBECTL, ns) as base:
            c = root_client(ns, base)
            try:
                tok = make_token(c)
                ans = chat(base, tok, model)
                assert ans.get("choices"), ans
            finally:
                c.close()
        time.sleep(2)
        result[ns] = last_log(ns)
        print(ns, json.dumps(result[ns]))
    qs, qt = result[SRC]["quota"], result[TGT]["quota"]
    print(f"quota source {qs}, target {qt}, source/30 = {qs / 30:.4f}, |target - source/30| = {abs(qt - qs / 30):.4f}")
    assert abs(qt - qs / 30) <= 1, "target charge must equal WOOW's / 30 within rounding"
    set_source_base_url(LITELLM)
    tool("--profile", p["apporo"], "--auth", "password", "replay", "--yes-bootstrap", "--force", "--why",
         "sandbox quota test: channel 1 back to LiteLLM")
    tool("--profile", p["apporo"], "verify", "--strict", "--check-keys")


def put_map_entry(c, snap, key, entry, value):
    m = json.loads(snap["options"][key])
    if value is None:
        m.pop(entry, None)
    else:
        m[entry] = value
    api_call(c, "PUT", "/api/option/", {"key": key, "value": nc.go_json(m)})
    snap["options"][key] = nc.go_json(m)


def phase_daily():
    p = profiles()
    tool("--profile", p["apporo"], "mint-sync-pat")
    rc, text = tool("--profile", p["apporo"], "apply")
    assert "plan: 0 auto, 0 hold" in text, "first apply after replay must be a no-op"
    prof = json.load(open(p["mirror"]))
    snap = nc.read_snapshot(nc.Psql(KUBECTL, SRC), prof)
    with aps.PortForward(KUBECTL, SRC) as base:
        c = root_client(SRC, base)
        try:
            put_map_entry(c, snap, "ModelRatio", "anthropic/claude-sonnet-5.5", 0.8)       # decrease
            put_map_entry(c, snap, "ModelRatio", "openai/gpt-6-luna", 0.06)                # increase
            put_map_entry(c, snap, "CacheRatio", "deepseek/deepseek-v4-pro", 0.08)         # cache increase
            put_map_entry(c, snap, "ModelRatio", "test/sandbox-new-model", 0.3)            # new model
            put_map_entry(c, snap, "CompletionRatio", "test/sandbox-new-model", 4)
            put_map_entry(c, snap, "CompletionRatio", "anthropic/claude-opus-5.5", 0)      # pilot output 0
            put_map_entry(c, snap, "ModelRatio", "apporo/gpt-6-luna", None)                # never offered: removed last
            api_call(c, "PUT", "/api/option/", {"key": "RetryTimes", "value": "3"})        # non-price option
            api_call(c, "PUT", "/api/option/", {"key": "GroupRatio",
                                                 "value": '{"default": 1.3, "internal": 1, "free": 0}'})
            ch1 = snap["channels"][1]["models"].split(",")
            ch1 = [m for m in ch1 if m != "poolside/laguna-xs-2.1:free"] + ["test/sandbox-new-model"]
            api_call(c, "PUT", "/api/channel/", {"id": 1, "models": ",".join(ch1)})       # add + remove
            api_call(c, "PUT", "/api/channel/", {"id": 4, "models": snap["channels"][4]["models"] + ",qwen3-sbx-extra"})
            api_call(c, "POST", "/api/channel/", {"mode": "single", "channel": {
                "name": "sbx new route", "type": 62, "key": "sk-sbx-new-route-000000", "base_url": LITELLM,
                "group": "default", "models": "sbx/new-route", "test_model": "sbx/new-route"}})
        finally:
            c.close()
    rc, text = tool("--profile", p["apporo"], "plan")
    rc, text = tool("--profile", p["apporo"], "daily", "--apply")
    tgt = nc.read_snapshot(nc.Psql(KUBECTL, TGT), prof)
    mr = json.loads(tgt["options"]["ModelRatio"])
    checks = {
        "decrease applied": mr["anthropic/claude-sonnet-5.5"] == 0.8 / 30,
        "increase held": mr["openai/gpt-6-luna"] == 0.05 / 30,
        "cache increase held": json.loads(tgt["options"]["CacheRatio"])["deepseek/deepseek-v4-pro"] == 0.07371,
        "new model priced": mr.get("test/sandbox-new-model") == 0.3 / 30,
        "new model routed": "test/sandbox-new-model" in tgt["channels"][1]["models"],
        "removed model gone": "poolside/laguna-xs-2.1:free" not in tgt["channels"][1]["models"],
        "RetryTimes copied": tgt["options"]["RetryTimes"] == "3",
        "GroupRatio alert-only": tgt["options"]["GroupRatio"] == '{"default": 1, "internal": 1, "free": 0}',
        "free channel alert-only": "qwen3-sbx-extra" not in tgt["channels"][4]["models"],
        "new channel alert-only": 5 not in tgt["channels"],
        "pilot output 0 alert-only": json.loads(tgt["options"]["CompletionRatio"])["anthropic/claude-opus-5.5"] == 5,
        "never-offered price removed": "apporo/gpt-6-luna" not in mr,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    jdir = os.path.join(STATE, "journal")
    last = sorted(n for n in os.listdir(jdir) if "-apply-" in n)[-1]
    with open(os.path.join(jdir, last)) as f:
        steps = [json.loads(line) for line in f]
    order = [(r["step"], r.get("key") or r.get("channel")) for r in steps if r["step"] in ("option", "channel-update")]
    print("write order (option keys / channel ids):", order)
    mr_writes = [i for i, (step, key) in enumerate(order) if key == "ModelRatio"]
    ch_writes = [i for i, (step, key) in enumerate(order) if step == "channel-update"]
    print(f"{'PASS' if len(mr_writes) == 2 and mr_writes[0] < min(ch_writes) < mr_writes[1] else 'FAIL'} "
          "ModelRatio written before the channels (new/changed) and after them (removed entries)")
    rc, text2 = tool("--profile", p["apporo"], "daily", "--apply")
    assert "plan: 0 auto" in text2, "second daily must apply nothing"
    with open(os.path.join(STATE, "holds.json")) as f:
        st = json.load(f)
    pending = [h for h in st["holds"].values() if h["state"] == "pending-notice"]
    print("pending holds:", [(h["id"], h["subject"]) for h in pending])
    nid = pending[0]["notice_id"]
    with open(st["notices"][nid]["draft"]) as f:
        print(f.read())
    # simulate a notice sent 10 days ago, effective 2 days ago (only the test edits created_at)
    now = datetime.datetime.now(datetime.timezone.utc)
    st["notices"][nid]["created_at"] = (now - datetime.timedelta(days=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(STATE, "holds.json"), "w") as f:
        json.dump(st, f)
    sent = (now - datetime.timedelta(days=10)).isoformat()
    effective = (now - datetime.timedelta(days=2)).date().isoformat()
    tool("notice-sent", nid, "--effective", effective, "--at", sent, "--backdated")
    tool("--profile", p["apporo"], "apply")
    tgt = nc.read_snapshot(nc.Psql(KUBECTL, TGT), prof)
    print("increase applied after maturity:", json.loads(tgt["options"]["ModelRatio"])["openai/gpt-6-luna"] == 0.06 / 30)
    print("cache increase applied after maturity:",
          json.loads(tgt["options"]["CacheRatio"])["deepseek/deepseek-v4-pro"] == 0.08)
    rc, text3 = tool("--profile", p["apporo"], "apply")
    assert "plan: 0 auto" in text3 and "matured" in text3
    tool("--profile", p["apporo"], "verify")
    tool("--profile", p["apporo"], "verify", "--strict", check=False)
    tool("holds")


def as_sync_identity():
    """kubectl that acts as the rbac.yaml ServiceAccount (impersonation: no token is handled)."""
    return KUBECTL + ["--as", f"system:serviceaccount:{TGT}:apporo-newapi-sync"]


def phase_rbac():
    """deploy/apporo-sync/rbac.yaml, with new-api -> SRC and new-api-apporo -> TGT, is enough for the daily job."""
    with open(os.path.join(REPO, "deploy", "apporo-sync", "rbac.yaml")) as f:
        text = f.read()
    text = text.replace("namespace: new-api-apporo", "namespace: " + TGT).replace("namespace: new-api\n",
                                                                                  "namespace: " + SRC + "\n")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "new-api" not in body.replace(SRC, "").replace(TGT, ""), "rbac.yaml names another namespace"
    for ns in (SRC, TGT):
        guard(ns)
    k("apply", "-f", "-", data=text.encode())
    p = profiles()
    as_sa = " ".join(as_sync_identity())
    env = dict(ENV, APPORO_SYNC_KUBECTL=as_sa)
    for args in (["daily", "--apply", "--live-images"], ["verify"], ["export", "--side", "target"]):
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "apporo_sync.py"), "--state-dir", STATE,
                            "--profile", p["apporo"], *args], capture_output=True, text=True, env=env, timeout=600)
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        print(f"{'PASS' if r.returncode == 0 else 'FAIL'} as the sync ServiceAccount: {' '.join(args)} -> rc "
              f"{r.returncode}: {tail}")
    for verb, res, ns, want in (("get", "secret/newapi-root-credentials", TGT, "no"),
                                ("get", "secret/newapi-upstream-keys", TGT, "yes"),
                                ("create", "secret", TGT, "no"),
                                ("patch", "deployment/newapi-master", TGT, "no"),
                                ("get", "secret/newapi-upstream-keys", SRC, "no"),
                                ("create", "pods/exec", SRC, "yes")):
        args = ["auth", "can-i", verb, res, "-n", ns, "--as", f"system:serviceaccount:{TGT}:apporo-newapi-sync"]
        if res == "pods/exec":
            args = ["auth", "can-i", verb, "pods/newapi-postgres-0", "--subresource=exec", "-n", ns,
                    "--as", f"system:serviceaccount:{TGT}:apporo-newapi-sync"]
        got = k(*args, check=False).stdout.decode().strip()
        print(f"{'PASS' if got == want else 'FAIL'} can-i {verb} {res} in {ns}: {got} (want {want})")


def list_pat_names(ns):
    with aps.PortForward(KUBECTL, ns) as base:
        c = root_client(ns, base)
        try:
            ans = c.req("GET", "/api/user/access_tokens")
        finally:
            c.close()
    data = ans.get("data") or []
    items = data.get("items", data) if isinstance(data, dict) else data
    return sorted((it.get("id"), it.get("name")) for it in items)


def phase_mint_revoke():
    """mint-sync-pat whose Secret write fails must revoke the PAT it created (real rc.44 revoke API)."""
    p = profiles()
    before = list_pat_names(TGT)
    aps.SECRET_WRITER = lambda ns, name, manifest: (False, "sandbox: simulated kubectl failure")
    try:
        rc = aps.main(["--profile", p["apporo"], "--kubectl", " ".join(KUBECTL), "--state-dir", STATE,
                       "mint-sync-pat"])
    finally:
        aps.SECRET_WRITER = None
    after = list_pat_names(TGT)
    print(f"{'PASS' if rc == 2 and after == before else 'FAIL'} mint-sync-pat with a failed Secret write: rc {rc}, "
          f"PATs before {len(before)} after {len(after)}")


def phase_platform(module_path):
    p = profiles()
    prof = json.load(open(p["apporo"]))
    mod = aps.load_platform_module(module_path)
    with aps.PortForward(KUBECTL, TGT) as base:
        req = urllib.request.Request(base + "/api/pricing", headers={"User-Agent": na.UA})
        with na._OPENER.open(req, timeout=30) as resp:
            real = json.loads(resp.read())
    snap = nc.read_snapshot(nc.Psql(KUBECTL, TGT), prof)
    synth = aps.pricing_payload(snap, prof)

    def verdict(payload):
        try:
            mod.normalize_pricing(payload, profile=prof["platform"]["pricing_profile"])
            return None
        except mod.PricingInvalid as e:
            return str(e)
    print("real module on real /api/pricing:", verdict(real))
    print("real module on synthesized payload:", verdict(synth))
    print("built-in port on synthesized payload:", aps.platform_check(synth, prof["platform"]))
    real_rows = {r["model_name"]: r for r in real["data"]}
    syn_rows = {r["model_name"]: r for r in synth["data"]}
    print("rows: real", len(real_rows), "synthesized", len(syn_rows), "same names:", set(real_rows) == set(syn_rows))
    diffs = []
    for m in sorted(set(real_rows) & set(syn_rows)):
        a, b = real_rows[m], syn_rows[m]
        for f in ("quota_type", "model_ratio", "model_price", "completion_ratio", "cache_ratio", "create_cache_ratio"):
            if (a.get(f) or 0) != (b.get(f) or 0):
                diffs.append((m, f, a.get(f), b.get(f)))
        if set(a["enable_groups"]) != set(b["enable_groups"]):
            diffs.append((m, "enable_groups", a["enable_groups"], b["enable_groups"]))
    print("field differences:", diffs[:20], "count", len(diffs))
    print("group_ratio real", real["group_ratio"], "synth", synth["group_ratio"])
    # each pilot model at Apporo: input price points/1M (the platform's own model_price)
    for m in ("chatgpt/gpt-5.5", "anthropic/claude-sonnet-5.5", "deepseek/deepseek-v4-pro"):
        if m in real_rows:
            print(m, mod.model_price(real_rows[m], "default", 1))


def phase_down():
    for ns in (SRC, TGT):
        guard(ns)
        k("delete", "namespace", ns, "--wait=true", "--timeout=300s", check=False)
    for ns in (SRC, TGT):
        r = k("get", "namespace", ns, check=False)
        print(ns, (r.stderr or r.stdout).decode().strip())
    if os.path.exists(SECRETS_FILE):
        os.unlink(SECRETS_FILE)


if __name__ == "__main__":
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    phase = sys.argv[1]
    if phase == "platform":
        phase_platform(sys.argv[2])
    else:
        globals()["phase_" + phase.replace("-", "_")]()
