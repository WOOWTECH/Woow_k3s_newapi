"""Unit tests for scripts/apporo_sync.py and scripts/apporo_newapi_content.py.

Run: python3 -m unittest discover -s scripts/tests -v
No cluster: an in-process fake rc.44 (FakeNewAPI) answers the admin API the
tool writes through, and FakePsql answers the read-only SQL it reads with, from
the same fake database. The fake keeps the rc.44 rules the sync depends on:
pricing maps re-marshaled with Go JSON on every pricing write (missing rows
created from defaults), other options stored raw, ServerAddress deriving the
passkey rows, channel ids from a sequence (explicit ids honoured), abilities =
models x groups, PUT /api/channel/ ignoring empty plain strings.
"""
import base64
import contextlib
import copy
import datetime
import decimal
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apporo_sync as aps  # noqa: E402
import apporo_newapi_content as nc  # noqa: E402

# Owner decision 2026-11-17: Apporo never fails a WOOW pull request. Once the Apporo
# values PR (#44) is in, CI runs every scripts/tests/test_apporo_*.py in its own
# "Apporo checks" step (scripts/apporo_checks.py sets APPORO_CHECKS=1: strict for a
# change to an Apporo file, warn-only otherwise), and this module skips itself
# anywhere else, like #44's modules. Without scripts/apporo_checks.py in the tree it
# runs in "Admin tool unit tests" as before. Locally after #44:
#   APPORO_CHECKS=1 python3 -m unittest discover -s scripts/tests -p 'test_apporo_sync.py'
if os.environ.get("APPORO_CHECKS") != "1" and os.path.exists(os.path.join(REPO, "scripts", "apporo_checks.py")):
    raise unittest.SkipTest("Apporo checks run in their own CI step (scripts/apporo_checks.py, APPORO_CHECKS=1); "
                            "locally: APPORO_CHECKS=1 python3 -m unittest discover -s scripts/tests "
                            "-p 'test_apporo_sync.py'")

PROFILE = os.path.join(REPO, "deploy", "apporo-sync", "profile.json")
PAT = "nap_" + "S" * 43
SESSION = "session-token-" + "x" * 20
ROOT_PW = "root-password-" + "y" * 18
KEY_API = "sk-" + "A" * 30
KEY_PAID = "sk-" + "P" * 30
SMTP_SECRET = "sk-" + "Z" * 30
RC44 = "v1.0.0-rc.44"
RC44_IMAGE = "docker.io/calciumion/new-api:v1.0.0-rc.44@" + nc.SEED_IMAGES[RC44]
PG_IMAGE = "docker.io/library/postgres:16.15-alpine3.24@sha256:" + "7" * 64


def load_json(path):
    with open(path) as f:
        return json.load(f)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def zulu(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path):
    with open(path) as f:
        return f.read()


PILOT = load_json(PROFILE)["platform"]["paid_pilot_models"]
REAL_VALUES_ALERTS = aps.values_alerts

DEFAULTS = {
    "ModelRatio": {"gpt-4": 15, "tiny-model": 0.0000015, "openrouter/free": 0},
    "ModelPrice": {"dall-e-3": 0.04, "mj_imagine": 0.1},
    "CompletionRatio": {"gpt-4": 2},
    "CacheRatio": {"gpt-4": 0.5},
    "CreateCacheRatio": {"claude-3-5-haiku-20241022": 1.25},
    "ImageRatio": {"gpt-image-1": 2},
    "AudioRatio": {"gpt-4o-audio-preview": 16},
    "AudioCompletionRatio": {"tts-1": 0},
    "billing_setting.billing_expr": {}, "billing_setting.billing_mode": {},
    "billing_setting.plugin_billing_expr": {},
}


def woow_ratios():
    mr = dict(DEFAULTS["ModelRatio"])
    cr = dict(DEFAULTS["CompletionRatio"])
    ca = dict(DEFAULTS["CacheRatio"])
    for m in PILOT:
        mr[m], cr[m], ca[m] = 1, 5, 0.1
    mr.update({"deepseek/deepseek-v4-flash-0731": 0.008, "deepseek/deepseek-v4-pro": 0.47763,
               "anthropic/claude-sonnet-5.5": 1, "openai/gpt-6-luna": 0.05, "qwen3-4b-aicoder": 0.025,
               "qwen3-4b-cluster1": 0.025, "apporo/gpt-6-luna": 0.05})
    cr.update({"deepseek/deepseek-v4-pro": 2, "openai/gpt-6-luna": 5, "qwen3-4b-aicoder": 4,
               "qwen3-4b-cluster1": 4, "apporo/gpt-6-luna": 5})
    ca.update({"deepseek/deepseek-v4-pro": 0.07371, "openai/gpt-6-luna": 0.1, "anthropic/claude-sonnet-5.5": 0.05})
    return mr, cr, ca


CH1 = ["anthropic/claude-fable-5.1", "anthropic/claude-haiku-5.5", "anthropic/claude-opus-5.5",
       "anthropic/claude-sonnet-5.5", "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4.1-flash",
       "openai/gpt-6-luna", "openrouter/free", "qwen3-4b-aicoder", "qwen3-4b-cluster1"]
CH3 = [m for m in PILOT if m.startswith("chatgpt/")] + ["deepseek/deepseek-v4-pro"]
LITELLM = "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000"


def channel(cid, name, group, models, test_model, remark, **extra):
    ch = {"id": cid, "type": 62, "name": name, "status": 1, "base_url": LITELLM, "group": group,
          "models": ",".join(models), "model_mapping": None, "status_code_mapping": "", "priority": 0,
          "weight": 0, "auto_ban": 1, "tag": None, "test_model": test_model, "openai_organization": None,
          "other": "", "other_info": "", "setting": None, "param_override": None, "header_override": None,
          "remark": remark, "channel_info": '{"is_multi_key":false,"multi_key_size":0,"multi_key_status_list":null,'
                                            '"multi_key_polling_index":0,"multi_key_mode":""}', "settings": ""}
    ch.update(extra)
    return ch


class FakeDB:
    """One rc.44 instance's tables, as the sync sees them."""

    def __init__(self, server_address=None):
        self.version = RC44            # setups.version: written once by /api/setup, never updated
        self.running = RC44            # what /api/status answers (the binary that runs)
        self.image = RC44_IMAGE        # what the Deployments pin (running_version reads it)
        self.options = {"theme.frontend": "default", "LegacyAccessTokenRetireAt": "1797000000",
                        "SelfUseModeEnabled": "false", "DemoSiteEnabled": "false"}
        if server_address:
            self.set_server_address(server_address)
        self.channels, self.keys = {}, {}
        self.seq = {"last_value": 1, "is_called": False}
        self.secret_options = {}

    def set_server_address(self, sa):
        self.options["ServerAddress"] = sa
        self.options["passkey.origins"] = "http://localhost:3000," + sa
        self.options["passkey.legacy_rp_ids"] = "localhost"
        self.options["passkey.rp_id"] = ""

    def put_pricing(self, key, value):
        for k in nc.PRICING_KEYS:
            if k not in self.options:
                self.options[k] = nc.go_json(DEFAULTS[k])
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        for v in parsed.values():
            if key not in nc.BILLING_MAPS and (not isinstance(v, (int, float)) or v < 0):
                raise ValueError("bad number")
        maps = {k: json.loads(self.options[k]) for k in nc.PRICING_KEYS}
        maps[key] = parsed
        for k in nc.PRICING_KEYS:
            self.options[k] = nc.go_json(maps[k])

    def nextval(self):
        v = self.seq["last_value"] + 1 if self.seq["is_called"] else self.seq["last_value"]
        self.seq.update(last_value=v, is_called=True)
        return v

    def abilities(self):
        return nc.expected_abilities(self.channels, {c: ch["status"] for c, ch in self.channels.items()})

    def load_woow(self):
        """WOOW-like production content (the shape of the real 11-17 export)."""
        mr, cr, ca = woow_ratios()
        self.options.update({
            "AutomaticDisableChannelEnabled": "true", "AutomaticEnableChannelEnabled": "true",
            "RetryTimes": "2", "GroupRatio": '{"default": 1, "internal": 1, "free": 0}',
            "UserUsableGroups": '{"default": "預設", "free": "免費試用"}', "HeaderNavModules": '{"rankings": false}',
            "ModelRequestRateLimitGroup": '{"free": [10, 10]}', "ModelRequestRateLimitEnabled": "true",
            "ModelRequestRateLimitSuccessCount": "0", "general_setting.quota_display_type": "CUSTOM",
            "general_setting.custom_currency_exchange_rate": "32", "TopupGroupRatio": '{"default": 1}',
            "GroupGroupRatio": "{}", "group_ratio_setting.group_special_usable_group": '{"free_blocked": {"-:free": ""}}',
            "quota_setting.enable_free_model_pre_consume": "false", "PasswordRegisterEnabled": "false",
            "RegisterEnabled": "false", "TaskPluginEnabled": "false",
        })
        self.set_server_address("https://newapi.woowtech.io")
        for k in nc.PRICING_KEYS:
            self.options[k] = nc.go_json(DEFAULTS[k])
        self.options["ModelRatio"] = nc.go_json(mr)
        self.options["CompletionRatio"] = nc.go_json(cr)
        self.options["CacheRatio"] = nc.go_json(ca)
        self.secret_options["SMTPToken"] = SMTP_SECRET
        self.channels = {
            1: channel(1, "LiteLLM svc203 · API（可販售）", "default", CH1, "openai/gpt-6-luna",
                       "LiteLLM key alias newapi-litellm (US$5 budget)"),
            2: channel(2, "LiteLLM svc203 · ChatGPT 訂閱（內部測試）", "internal", ["apporo/gpt-6-luna"],
                       "apporo/gpt-6-luna", "LiteLLM key alias newapi-litellm (US$5 budget)"),
            3: channel(3, "LiteLLM svc203 · 付費試行（ChatGPT / DeepSeek v4 Pro）", "default", CH3, "chatgpt/gpt-5.5",
                       "paid-pilot rc.44; fixed model route", param_override=""),
            4: channel(4, "LiteLLM svc203 · 免費試用（自架 Qwen）", "free", ["qwen3-4b-aicoder", "qwen3-4b-cluster1"],
                       "qwen3-4b-aicoder", "free-pilot rc.44; fixed Qwen route", param_override='{"max_tokens": 2048}'),
        }
        self.keys = {1: "sk-woow-not-copied-1", 2: "sk-woow-not-copied-2", 3: "sk-woow-not-copied-3",
                     4: "sk-woow-not-copied-4"}
        self.seq = {"last_value": 4, "is_called": True}
        return self

    def set_map_entry(self, key, entry, value):
        m = json.loads(self.options[key])
        if value is None:
            m.pop(entry, None)
        else:
            m[entry] = value
        self.options[key] = nc.go_json(m)


class FakePsql:
    """Answers the two read-only queries of apporo_newapi_content from a FakeDB."""

    def __init__(self, db):
        self.db, self.queries = db, []

    def query_json(self, sql):
        nc.check_read_only_sql(sql)
        self.queries.append(sql)
        db = self.db
        if "'keys'" in sql:
            return {"version": db.version, "keys": sorted(list(db.options) + list(db.secret_options), key=str.encode)}
        seg = sql.split("WHERE key IN (", 1)[1].split(")), 'channels'", 1)[0]
        wanted = [m.replace("''", "'") for m in re.findall(r"'((?:[^']|'')*)'", seg)]
        for k in wanted:
            if k in db.secret_options:
                raise AssertionError(f"the export asked for the value of {k}")
        rows = [[k, db.options[k], hashlib.sha256(db.options[k].encode()).hexdigest()]
                for k in sorted(wanted, key=str.encode) if k in db.options]
        chans = []
        for cid in sorted(db.channels):
            ch = {f: v for f, v in db.channels[cid].items()}
            if "key_sha256_16" in sql:
                ch["key_sha256_16"] = hashlib.sha256(db.keys[cid].encode()).hexdigest()[:16]
                ch["key_length"] = len(db.keys[cid])
            chans.append(ch)
        return {"version": db.version, "options": rows, "channels": chans, "abilities": db.abilities(),
                "seq": dict(db.seq)}


NONEMPTY_STRING_FIELDS = {"name", "models", "group", "other", "other_info", "settings"}


class FakeNewAPI:
    """The admin API the sync writes through (rc.44 semantics it relies on)."""

    def __init__(self, db):
        self.db, self.requests, self.sessions, self.pats = db, [], set(), {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null")

            def _authed(self):
                auth = self.headers.get("Authorization", "")
                return auth in ("Bearer " + PAT, "Bearer " + SESSION) and \
                    (auth != "Bearer " + SESSION or SESSION in fake.sessions)

            def do_GET(self):
                if self.path == "/api/status":
                    sa = fake.db.options.get("ServerAddress", "http://localhost:3000")
                    return self._send({"success": True, "data": {"version": fake.db.running, "server_address": sa}})
                self._send({"success": False}, 404)

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, body))
                if self.path == "/api/user/login":
                    if body == {"username": "root", "password": ROOT_PW}:
                        fake.sessions.add(SESSION)
                        return self._send({"success": True, "data": {"access_token": SESSION,
                                                                     "session": {"sid": "sid-1"}}})
                    return self._send({"success": False, "message": "bad login"})
                if not self._authed():
                    return self._send({"success": False, "message": "unauthorized"}, 401)
                if self.path == "/api/verify":
                    ok = body.get("password") == ROOT_PW
                    return self._send({"success": ok, "data": {"proof_token": "proof-1"} if ok else None})
                if self.path == "/api/user/access_tokens":
                    ok = self.headers.get("X-Security-Proof") == "proof-1"
                    if ok:
                        fake.pats[7] = PAT
                    return self._send({"success": ok, "data": {"token": PAT, "item": {"id": 7,
                                                                                       "token_hint": "nap_...SSSS"}}})
                if self.path == "/api/channel/":
                    return self._send(fake.add_channel(body))
                self._send({"success": False}, 404)

            def do_PUT(self):
                body = self._body()
                fake.requests.append(("PUT", self.path, body))
                if not self._authed():
                    return self._send({"success": False, "message": "unauthorized"}, 401)
                if self.path == "/api/option/":
                    return self._send(fake.put_option(body))
                if self.path == "/api/channel/":
                    return self._send(fake.update_channel(body))
                self._send({"success": False}, 404)

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path, None))
                if self.path.startswith("/api/user/sessions/"):
                    fake.sessions.discard(SESSION)
                    return self._send({"success": True})
                if not self._authed():
                    return self._send({"success": False, "message": "unauthorized"}, 401)
                m = re.fullmatch(r"/api/user/access_tokens/(\d+)", self.path)
                if m:
                    ok = self.headers.get("X-Security-Proof") == "proof-1" and int(m.group(1)) in fake.pats
                    if ok:
                        del fake.pats[int(m.group(1))]
                    return self._send({"success": ok, "message": "" if ok else "not found"})
                m = re.fullmatch(r"/api/channel/(\d+)", self.path)
                if m and int(m.group(1)) in fake.db.channels:
                    del fake.db.channels[int(m.group(1))]
                    fake.db.keys.pop(int(m.group(1)), None)
                    return self._send({"success": True})
                self._send({"success": False, "message": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def put_option(self, body):
        key, value = body.get("key"), body.get("value")
        if not isinstance(value, str):
            value = json.dumps(value)
        if key == "LegacyAccessTokenRetireAt":
            return {"success": False, "message": "LegacyAccessTokenRetireAt is managed by the server"}
        if key in nc.PRICING_KEYS:
            try:
                self.db.put_pricing(key, value)
            except ValueError as e:
                return {"success": False, "message": str(e)}
        elif key == nc.TOOL_PRICE_KEY:
            # rc.44 ValidateToolPricesJSON: one object of finite numbers >= 0; then stored as sent
            prices, ignored = nc.tool_price_overrides(value)
            if ignored or not value.strip().startswith("{"):
                return {"success": False, "message": "Tool prices must be a JSON object of non-negative numbers"}
            self.db.options[key] = value
        elif key == "ServerAddress":
            self.db.set_server_address(value)
        else:
            self.db.options[key] = value
        return {"success": True}

    def add_channel(self, body):
        ch = dict(body["channel"])
        key = ch.pop("key", "")
        if not key:
            return {"success": True}
        cid = ch.pop("id", None) or self.db.nextval()
        if cid in self.db.channels:
            return {"success": False, "message": "duplicate key value violates unique constraint"}
        row = channel(cid, ch.get("name", ""), ch.get("group", "default"), [], None, None)
        row.update(test_model=None, remark=None, base_url="", status_code_mapping="")
        row.update({f: v for f, v in ch.items() if f != "status"})
        row["status"] = ch.get("status", 1)
        self.db.channels[cid] = row
        self.db.keys[cid] = key
        return {"success": True}

    def update_channel(self, body):
        if "status" in body:
            return {"success": False, "message": "Invalid parameters"}
        cid = body.get("id")
        if cid not in self.db.channels:
            return {"success": False, "message": "record not found"}
        for f, v in body.items():
            if f in ("id",):
                continue
            if f == "key":
                if v:
                    self.db.keys[cid] = v
                continue
            if v is None or (v == "" and f in NONEMPTY_STRING_FIELDS):
                continue  # GORM Updates() skips zero values of plain fields and nil pointers
            self.db.channels[cid][f] = v
        return {"success": True}


SECRETS = {
    ("new-api-apporo", "newapi-upstream-keys", "api"): KEY_API,
    ("new-api-apporo", "newapi-upstream-keys", "paid"): KEY_PAID,
    ("new-api-apporo", "newapi-sync-pat", "token"): PAT,
    ("new-api-apporo", "newapi-root-credentials", "username"): "root",
    ("new-api-apporo", "newapi-root-credentials", "password"): ROOT_PW,
}


class Harness(unittest.TestCase):
    """A WOOW-like source, a fresh Apporo target (after /api/setup) and a state dir."""

    def setUp(self):
        self.src = FakeDB().load_woow()
        self.tgt = FakeDB()
        self.api = FakeNewAPI(self.tgt)
        self.addCleanup(self.api.close)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.sql_reads = []
        aps.PSQL_FACTORY = lambda side, profile: (self.sql_reads.append(side),
                                                  FakePsql(self.src if side == "source" else self.tgt))[1]
        aps.WORKLOAD_READER = self.workloads
        aps.SECRET_READER = lambda ns, name, key: SECRETS.get((ns, name, key))
        aps.ANNOTATION_READER = lambda ns, name, annotation: None
        # the Helm values check (scripts/apporo_parity.py + helm) has its own tests: keep daily runs hermetic
        aps.values_alerts = lambda repo, run=None: []
        self.addCleanup(setattr, aps, "values_alerts", REAL_VALUES_ALERTS)
        self.addCleanup(setattr, aps, "PSQL_FACTORY", None)
        self.addCleanup(setattr, aps, "WORKLOAD_READER", None)
        self.addCleanup(setattr, aps, "SECRET_READER", None)
        self.addCleanup(setattr, aps, "ANNOTATION_READER", None)

    def workloads(self, namespace):
        """`kubectl get deploy,statefulset,cronjob` of a side: its New API image and Postgres."""
        db = {"new-api": self.src, "new-api-apporo": self.tgt}.get(namespace)
        if db is None:
            return None
        return {"Deployment/newapi-master/new-api": db.image, "StatefulSet/newapi-postgres/postgres": PG_IMAGE}

    def run_cli(self, *args, auth="pat"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = aps.main(["--state-dir", self.state, "--target-base", self.api.base, "--auth", auth, *args])
        text = out.getvalue() + err.getvalue()
        for secret in (KEY_API, KEY_PAID, PAT, ROOT_PW, SMTP_SECRET, SESSION):
            self.assertNotIn(secret, text, "a credential reached the output")
        return rc, text

    def replay(self):
        rc, out = self.run_cli("replay", "--yes-bootstrap", auth="password")
        self.assertEqual(rc, 0, out)
        return out

    def state_data(self):
        with open(os.path.join(self.state, "holds.json")) as f:
            return json.load(f)

    def save_state(self, st):
        with open(os.path.join(self.state, "holds.json"), "w") as f:
            json.dump(st, f)

    def alerts(self):
        out = []
        d = os.path.join(self.state, "alerts")
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                with open(os.path.join(d, name)) as f:
                    out += [json.loads(line) for line in f]
        return out

    def mr(self, model):
        return json.loads(self.tgt.options["ModelRatio"]).get(model)

    def send_notice(self, nid, sent_days_ago=10, effective_days_ago=2):
        """Record a notice as sent long enough ago that its effective date has begun everywhere."""
        st = self.state_data()
        st["notices"][nid]["created_at"] = zulu(utcnow() - datetime.timedelta(days=sent_days_ago + 1))
        self.save_state(st)
        sent = (utcnow() - datetime.timedelta(days=sent_days_ago)).isoformat()
        effective = (utcnow() - datetime.timedelta(days=effective_days_ago)).date().isoformat()
        return self.run_cli("notice-sent", nid, "--effective", effective, "--at", sent, "--backdated")


# ------------------------------------------------------------------ tests

class GoJSONTest(unittest.TestCase):
    def test_float_formatting_matches_go(self):
        cases = {1.0: "1", 0.5: "0.5", 1 / 30: "0.03333333333333333", 0.008 / 30: "0.0002666666666666667",
                 5e-7: "5e-7", 1e-7: "1e-7", 2.5e-10: "2.5e-10", 1e21: "1e+21", 123456789012345678.0: "123456789012345680",
                 0.0: "0", 0.47763 / 30: "0.015921", 15 / 30: "0.5", 0.273972602739726 / 30: "0.0091324200913242"}
        for f, want in cases.items():
            self.assertEqual(nc.go_float(f), want, f)

    def test_go_json_sorts_keys_bytewise_and_escapes_like_go(self):
        self.assertEqual(nc.go_json({"b": 1, "B": 2.5, "a<&>": True, "預設": "x"}),
                         '{"B":2.5,"a\\u003c\\u0026\\u003e":true,"b":1,"預設":"x"}')

    def test_canonical_round_trip_and_refusal(self):
        text = nc.go_json({"m": 0.1, "n": 5})
        self.assertEqual(nc.canonical_map(text, "ModelRatio"), {"m": 0.1, "n": 5})
        with self.assertRaises(nc.ContentError):
            nc.canonical_map('{"n": 5, "m": 0.1}', "ModelRatio")   # not Go output: refuse to transform
        with self.assertRaises(nc.ContentError):
            nc.canonical_map('{"m":1,"m":2}', "ModelRatio")

    def test_divide_map_is_float64_and_byte_exact(self):
        self.assertEqual(nc.divide_map('{"a":1,"b":0.008,"c":0}', "ModelRatio", 30),
                         '{"a":0.03333333333333333,"b":0.0002666666666666667,"c":0}')


class SQLGuardTest(unittest.TestCase):
    def test_generated_sql_is_read_only_and_never_reads_channel_keys_or_customer_tables(self):
        for sql in (nc.sql_option_keys(), nc.sql_content(["ModelRatio", "tool_price_setting.prices"], False),
                    nc.sql_content(["ModelRatio"], True), nc.sql_content([], False)):
            self.assertTrue(sql.startswith("SET default_transaction_read_only = on;\n"))
            nc.check_read_only_sql(sql)
            for table in nc.FORBIDDEN_TABLES:
                self.assertNotRegex(sql.lower(), r"\b(from|join)\s+" + table + r"\b")
        self.assertNotIn("'key', key", nc.sql_content(["x"], True))
        self.assertNotIn("key", [api for api, _ in nc.CHANNEL_COLUMNS])

    def test_the_templates_are_the_reviewed_ones(self):
        templates = nc.sql_templates()
        self.assertEqual(sorted(templates), sorted(nc.SQL_TEMPLATE_SHA256))
        for name, (head, tail) in templates.items():
            self.assertEqual(nc.template_sha256(head, tail), nc.SQL_TEMPLATE_SHA256[name], name)
            text = head + ("" if tail is None else "'K'" + tail)
            self.assertTrue(text.startswith(nc.READ_ONLY_PREAMBLE), name)
            body = text[len(nc.READ_ONLY_PREAMBLE):]
            self.assertEqual(body.count(";"), 1, name)
            self.assertTrue(body.endswith(")::text;"), name)
            tables = re.findall(r"\bFROM\s+(\w+)", body)
            self.assertLessEqual(set(tables), {"options", "channels", "abilities", "setups", "channels_id_seq"}, name)
            # `key` of channels: never as a value; only inside sha256(...) / length(...) on the target query
            chan = body.split("'channels', (SELECT", 1)[1].split("FROM channels)", 1)[0] if "FROM channels" in body else ""
            self.assertEqual("FROM channels" in body, name != "option-keys")
            outside = chan.replace("sha256(convert_to(key, 'UTF8'))", "").replace("length(key)", "")
            self.assertNotRegex(outside, r"\bkey\b", name)
            if name == "content":
                self.assertNotRegex(chan, r"\bkey\b", "the source query never touches channels.key")

    def test_a_changed_template_refuses_every_read(self):
        orig = nc.CHANNEL_COLUMNS
        self.addCleanup(setattr, nc, "CHANNEL_COLUMNS", orig)
        nc.CHANNEL_COLUMNS = orig + (("key", "key"),)
        with self.assertRaises(nc.ContentError) as cm:
            nc.sql_content(["ModelRatio"], False)
        self.assertIn("template changed", str(cm.exception))
        with self.assertRaises(nc.ContentError):
            nc.check_read_only_sql(nc._option_keys_text())

    def test_key_names_that_need_quoting_are_never_sent(self):
        self.assertEqual(nc.sql_literal("it's"), "'it''s'")
        for keys in (["it's"], ["a b"], [""], ["x" * 129], ["a\\b"], ["ModelRatio", "x'y"]):
            with self.assertRaises(nc.ContentError, msg=keys):
                nc.sql_content(keys, False)

    def test_guard_refuses_writes_key_reads_and_customer_tables(self):
        pre = nc.READ_ONLY_PREAMBLE
        for bad in ("SELECT 1;", pre + "UPDATE options SET value='x';", pre + "SELECT 1; SELECT 2;",
                    pre + "SELECT key FROM channels;", pre + "SELECT jsonb_agg(c) FROM tokens c;",
                    pre + "SELECT setval('channels_id_seq', 9);", pre + "SELECT id FROM users;"):
            with self.assertRaises(nc.ContentError, msg=bad):
                nc.check_read_only_sql(bad)


class ExportTest(unittest.TestCase):
    def test_secret_looking_keys_are_never_read(self):
        db = FakeDB().load_woow()
        psql = FakePsql(db)
        snap = nc.read_snapshot(psql, load_json(PROFILE))
        self.assertNotIn("SMTPToken", snap["options"])
        self.assertEqual(snap["skipped_keys"].get("SMTPToken"), "secret")
        self.assertIn("PasswordRegisterEnabled", snap["options"])  # whitelisted by the profile
        for ch in snap["channels"].values():
            self.assertNotIn("key", ch)
            self.assertNotIn("key_sha256_16", ch)  # source: no key fingerprint either

    def test_a_credential_in_a_value_is_dropped(self):
        db = FakeDB().load_woow()
        db.options["Footer"] = "see https://user:hunter2@example.com"
        db.channels[3]["header_override"] = '{"Authorization": "Bearer sk-' + "Q" * 30 + '"}'
        snap = nc.read_snapshot(FakePsql(db), load_json(PROFILE))
        self.assertEqual(snap["skipped_keys"]["Footer"], "secret-value")
        self.assertEqual(snap["unsafe_channels"], {3: ["header_override"]})
        snap["running_version"] = RC44
        desired, notes = nc.transform(snap, nc.load_profile(PROFILE))
        self.assertNotIn(3, desired["channels"])
        self.assertIn("source-channel-unsafe", [n[0] for n in notes])


class TransformTest(unittest.TestCase):
    def setUp(self):
        self.profile = nc.load_profile(PROFILE)
        self.src = nc.read_snapshot(FakePsql(FakeDB().load_woow()), self.profile)
        self.src["running_version"] = RC44            # apporo_sync.read_side adds it (from the Deployment)
        self.desired, self.notes = nc.transform(self.src, self.profile)

    def test_prices_divided_by_30_exactly(self):
        mr = json.loads(self.desired["options"]["ModelRatio"])
        self.assertEqual(mr["chatgpt/gpt-5.5"], 1 / 30)
        self.assertEqual(self.desired["options"]["ModelPrice"], nc.go_json({"dall-e-3": 0.04 / 30, "mj_imagine": 0.1 / 30}))
        for k in ("CompletionRatio", "CacheRatio", "CreateCacheRatio", "AudioRatio", "ImageRatio"):
            self.assertEqual(self.desired["options"][k], self.src["options"][k], k)
        self.assertEqual(self.desired["options"]["GroupRatio"], self.src["options"]["GroupRatio"])
        self.assertEqual(self.desired["options"]["general_setting.custom_currency_exchange_rate"], "32")

    def test_apporo_values(self):
        o = self.desired["options"]
        self.assertEqual(o["ServerAddress"], "https://newapi.apporo.ai")
        self.assertEqual(json.loads(o["UserUsableGroups"]), {"default": "Default", "free": "Free trial"})
        self.assertEqual(o["general_setting.custom_currency_symbol"], "$")
        for k in nc.DERIVED_KEYS + nc.INSTANCE_KEYS:
            self.assertNotIn(k, o)

    def test_channels_1_3_4_keep_ids_and_channel_2_is_left_out(self):
        self.assertEqual(sorted(self.desired["channels"]), [1, 3, 4])
        self.assertEqual(self.desired["channel_slots"], {1: "api", 3: "paid", 4: "api"})
        self.assertEqual(self.desired["channels"][1]["remark"], "LiteLLM key alias apporo-newapi-litellm (monthly budget)")
        self.assertEqual(self.desired["channels"][3]["remark"], "paid-pilot rc.44; fixed model route")
        self.assertEqual(self.desired["channels"][3]["param_override"], "")
        self.assertIsNone(self.desired["channels"][1]["param_override"])
        self.assertNotIn("source-channel-new", [n[0] for n in self.notes])

    def test_no_running_version_no_transform(self):
        src = dict(self.src)
        del src["running_version"]               # never priced with a guessed (or setups) version
        with self.assertRaises(nc.ContentError):
            nc.transform(src, self.profile)
        self.assertEqual(self.src["setup_version"], RC44)
        self.assertNotIn("version", self.src)

    def test_profile_refuses_woow_targets(self):
        p = load_json(PROFILE)
        p["target"]["namespace"] = "new-api"
        with self.assertRaises(nc.ContentError):
            nc.validate_profile(p)


class ReplayTest(Harness):
    def test_replay_then_verify_strict_and_idempotent(self):
        out = self.replay()
        self.assertIn("placeholder created and deleted", out)
        self.assertEqual(sorted(self.tgt.channels), [1, 3, 4])
        self.assertEqual(self.tgt.seq, {"last_value": 4, "is_called": True})
        self.assertEqual(self.tgt.keys, {1: KEY_API, 3: KEY_PAID, 4: KEY_API})
        # abilities for 1/3/4 byte-identical to the source's rows
        src_rows = [r for r in self.src.abilities() if r["channel_id"] in (1, 3, 4)]
        self.assertEqual(nc.abilities_fingerprint(self.tgt.abilities()), nc.abilities_fingerprint(src_rows))
        # options: transform, byte for byte
        self.assertEqual(self.tgt.options["ModelRatio"], nc.divide_map(self.src.options["ModelRatio"], "ModelRatio", 30))
        self.assertEqual(self.tgt.options["passkey.origins"], "http://localhost:3000,https://newapi.apporo.ai")
        rc, out = self.run_cli("verify", "--strict", "--check-keys")
        self.assertEqual(rc, 0, out)
        self.assertIn(", 0 failed", out)
        before = len(self.api.requests)
        rc, out = self.run_cli("apply")
        self.assertEqual(rc, 0, out)
        writes = [r for r in self.api.requests[before:] if r[0] in ("PUT", "POST", "DELETE") and "sessions" not in r[1]]
        self.assertEqual(writes, [], "second run must be a no-op")
        self.assertIn("plan: 0 auto, 0 hold", out)

    def test_password_login_session_is_revoked(self):
        self.replay()
        self.assertIn(("DELETE", "/api/user/sessions/sid-1", None), self.api.requests)
        self.assertEqual(self.api.sessions, set())

    def test_replay_refused_after_the_daily_sync_started(self):
        self.replay()
        self.run_cli("apply")
        rc, out = self.run_cli("replay", "--yes-bootstrap", auth="password")
        self.assertEqual(rc, 2)
        self.assertIn("daily sync already ran", out)

    def test_refuses_a_target_that_is_a_woow_site(self):
        self.tgt.set_server_address("https://newapi.woowtech.io")
        rc, out = self.run_cli("replay", "--yes-bootstrap", auth="password")
        self.assertEqual(rc, 2)
        self.assertIn("WOOW site", out)

    def test_refuses_a_target_running_an_unknown_image(self):
        self.tgt.image = "docker.io/calciumion/new-api:v1.0.0-rc.45@sha256:" + "4" * 64
        rc, out = self.run_cli("replay", "--yes-bootstrap", auth="password")
        self.assertEqual(rc, 2)
        self.assertIn("new-api-apporo runs New API docker.io/calciumion/new-api:v1.0.0-rc.45@sha256:4444", out)
        self.assertIn("not an image whose rules and built-in prices this tool knows", out)
        self.assertEqual([r for r in self.api.requests if r[0] != "GET"], [])

    def test_setups_version_is_not_the_version_that_runs(self):
        # rc.44 writes setups.version once, at /api/setup: an instance installed with another
        # version and upgraded to rc.44 still says so. It is shown, never used.
        self.tgt.version = self.src.version = "v1.0.0-rc.40"
        self.replay()
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("export", "--side", "target")
        self.assertIn(f"runs New API {RC44} ({RC44_IMAGE}; setups.version v1.0.0-rc.40)", out)


class DailyTest(Harness):
    def setUp(self):
        super().setUp()
        self.replay()

    def mutate_source(self):
        s = self.src
        s.set_map_entry("ModelRatio", "anthropic/claude-sonnet-5.5", 0.8)       # decrease -> auto
        s.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)                # increase -> hold
        s.set_map_entry("ModelRatio", "test/new-model", 0.3)                    # new model -> auto
        s.set_map_entry("CompletionRatio", "test/new-model", 4)
        s.channels[1]["models"] += ",test/new-model"
        s.channels[1]["models"] = s.channels[1]["models"].replace(",openrouter/free", "")  # removed -> auto+alert
        s.options["RetryTimes"] = "3"                                            # non-price option -> auto
        s.options["GroupRatio"] = '{"default": 1.3, "internal": 1, "free": 0}'   # platform-coupled -> alert
        s.channels[4]["models"] += ",qwen3-new"                                  # free channel -> alert
        s.channels[5] = channel(5, "new route", "default", ["x/y"], "x/y", "")   # new channel -> alert
        s.keys[5] = "sk-woow-5"
        s.set_map_entry("ModelRatio", "anthropic/claude-haiku-5.5", 0)           # price 0 -> alert
        s.set_map_entry("CompletionRatio", "anthropic/claude-opus-5.5", 0)       # pilot output 0 -> platform gate

    def test_classification_holds_alerts_and_idempotency(self):
        self.mutate_source()
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        mr = json.loads(self.tgt.options["ModelRatio"])
        self.assertEqual(mr["anthropic/claude-sonnet-5.5"], 0.8 / 30)            # decrease applied
        self.assertEqual(mr["openai/gpt-6-luna"], 0.05 / 30)                     # increase held
        self.assertEqual(mr["test/new-model"], 0.3 / 30)                         # new model applied
        self.assertEqual(mr["anthropic/claude-haiku-5.5"], 1 / 30)               # zero price not applied
        self.assertEqual(json.loads(self.tgt.options["CompletionRatio"])["anthropic/claude-opus-5.5"], 5)
        self.assertIn("test/new-model", self.tgt.channels[1]["models"])
        self.assertNotIn("openrouter/free", self.tgt.channels[1]["models"])
        self.assertEqual(self.tgt.options["RetryTimes"], "3")
        self.assertEqual(self.tgt.options["GroupRatio"], '{"default": 1, "internal": 1, "free": 0}')
        self.assertNotIn("qwen3-new", self.tgt.channels[4]["models"])
        self.assertNotIn(5, self.tgt.channels)
        self.assertIn("ALERT [action] GroupRatio[default]", out)
        self.assertIn("channel 4 model qwen3-new", out)
        self.assertIn("source-channel-new: channel 5", out)
        self.assertIn("model openrouter/free", out)
        self.assertIn("pricing_zero", out)
        st = self.state_data()
        holds = [h for h in st["holds"].values() if h["state"] == "pending-notice"]
        self.assertEqual([h["subject"] for h in holds], [["map", "ModelRatio", "openai/gpt-6-luna"]])
        nid = holds[0]["notice_id"]
        notice = read_text(st["notices"][nid]["draft"])
        self.assertIn("| `openai/gpt-6-luna` | input | 0.106667 | 0.128 | +20.0% |", notice)
        self.assertIn("{EFFECTIVE_DATE}", notice)
        self.assertIn("APPORO UNION INC.", notice)
        # second run: no write, the same hold, no new notice
        before = len(self.api.requests)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual([r for r in self.api.requests[before:] if r[0] != "DELETE"], [])
        self.assertEqual(len(self.state_data()["notices"]), 1)
        # the owner sends the notice; once its effective date has begun everywhere, the increase applies
        rc, out = self.send_notice(nid)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.tgt.options["ModelRatio"])["openai/gpt-6-luna"], 0.06 / 30)
        self.assertEqual(self.state_data()["holds"][holds[0]["id"]]["state"], "applied")
        before = len(self.api.requests)
        rc, out = self.run_cli("apply")
        self.assertEqual([r for r in self.api.requests[before:] if r[0] != "DELETE"], [])

    def test_hold_waits_for_seven_days_after_the_notice(self):
        self.src.set_map_entry("CacheRatio", "openai/gpt-6-luna", 0.2)            # cache price increase
        self.run_cli("daily", "--apply")
        st = self.state_data()
        (h,) = [h for h in st["holds"].values()]
        earliest = aps.earliest_effective(utcnow(), 7)
        rc, out = self.run_cli("notice-sent", h["notice_id"], "--effective", earliest.isoformat())   # sent now
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state_data()["holds"][h["id"]]["eligible_at"], zulu(aps.effective_start(earliest)))
        self.run_cli("apply")
        self.assertEqual(json.loads(self.tgt.options["CacheRatio"])["openai/gpt-6-luna"], 0.1)
        self.assertEqual(self.state_data()["holds"][h["id"]]["state"], "notified")

    def test_a_changed_increase_supersedes_and_a_revert_cancels(self):
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.07)
        self.run_cli("daily", "--apply")
        states = sorted(h["state"] for h in self.state_data()["holds"].values())
        self.assertEqual(states, ["pending-notice", "superseded"])
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.05)
        self.run_cli("daily", "--apply")
        states = sorted(h["state"] for h in self.state_data()["holds"].values())
        self.assertEqual(states, ["cancelled", "superseded"])

    def test_a_taken_off_models_price_is_kept_for_the_window_then_removed(self):
        self.src.channels[1]["models"] = self.src.channels[1]["models"].replace(",openai/gpt-6-luna", "")
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", None)
        self.src.channels[1]["test_model"] = "anthropic/claude-haiku-5.5"
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("openai/gpt-6-luna", self.tgt.channels[1]["models"])       # route removed at once
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)                     # price kept (re-listing)
        self.assertIn("ALERT [action] channel 1 model openai/gpt-6-luna", out)
        self.assertIn("ALERT [info] KEPT ModelRatio[openai/gpt-6-luna]", out)
        rc, out = self.run_cli("daily", "--apply")                                     # still kept, no write
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)
        st = self.state_data()                                                          # 31 days later
        st["served"]["openai/gpt-6-luna"]["last_seen"] = zulu(utcnow() - datetime.timedelta(days=31))
        self.save_state(st)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.mr("openai/gpt-6-luna"))
        self.assertIn("AUTO  ModelRatio[openai/gpt-6-luna]: not offered on the target", out)

    def test_removing_only_the_price_of_a_served_model_is_alert_only(self):
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", None)
        rc, out = self.run_cli("daily", "--apply")
        self.assertIn("openai/gpt-6-luna", json.loads(self.tgt.options["ModelRatio"]))
        self.assertIn("loses its price", out)

    def test_plan_is_read_only(self):
        self.mutate_source()
        before = (copy.deepcopy(self.tgt.options), copy.deepcopy(self.tgt.channels), len(self.api.requests))
        rc, out = self.run_cli("plan")
        self.assertEqual(rc, 0, out)
        self.assertEqual(before, (self.tgt.options, self.tgt.channels, len(self.api.requests)))
        self.assertFalse(os.path.exists(os.path.join(self.state, "notices")))
        self.assertIn("HOLD  ModelRatio[openai/gpt-6-luna]", out)

    def test_verify_allows_only_held_and_alert_differences(self):
        self.mutate_source()
        self.run_cli("daily", "--apply")
        rc, out = self.run_cli("verify")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 1, out)

    def test_key_rotation_is_applied(self):
        SECRETS[("new-api-apporo", "newapi-upstream-keys", "paid")] = "sk-" + "R" * 30
        self.addCleanup(SECRETS.__setitem__, ("new-api-apporo", "newapi-upstream-keys", "paid"), KEY_PAID)
        rc, out = self.run_cli("apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tgt.keys[3], "sk-" + "R" * 30)
        self.assertNotIn("sk-" + "R" * 30, out)


class AlertTest(Harness):
    def alerts(self):
        out = []
        d = os.path.join(self.state, "alerts")
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name)) as f:
                out += [json.loads(line) for line in f]
        return out

    def test_a_hold_alert_is_new_only_once(self):
        self.replay()
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")
        self.run_cli("daily", "--apply")
        holds = [al for al in self.alerts() if al["title"].startswith("HOLD ")]
        self.assertEqual([al["new"] for al in holds], [True, False])
        self.assertEqual(holds[0]["fingerprint"], holds[1]["fingerprint"])

    def test_a_failed_daily_run_is_an_alert(self):
        def broken(side, profile):
            raise nc.ContentError("psql exit 2: connection refused")
        aps.PSQL_FACTORY = broken
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 2)
        (al,) = self.alerts()
        self.assertEqual(al["title"], "daily sync failed")
        self.assertIn("connection refused", al["details"][0])


class PlatformCheckTest(unittest.TestCase):
    def setUp(self):
        self.profile = nc.load_profile(PROFILE)
        self.db = FakeDB().load_woow()

    def state(self):
        return {"options": self.db.options, "abilities": self.db.abilities()}

    def verdict(self):
        return aps.platform_check(aps.pricing_payload(self.state(), self.profile), self.profile["platform"])

    def test_woow_like_catalogue_passes_the_pilot_profile(self):
        self.assertIsNone(self.verdict())

    def test_failures_match_the_platform_labels(self):
        self.db.options["GroupRatio"] = '{"default": 1.3, "internal": 1, "free": 0}'
        self.assertEqual(self.verdict(), "pricing_ratio")
        self.db.load_woow()
        self.db.set_map_entry("ModelRatio", "chatgpt/gpt-5.5", 0)
        self.assertEqual(self.verdict(), "pricing_zero")
        self.db.load_woow()
        self.db.channels[4]["models"] += ",chatgpt/gpt-5.5"
        self.assertEqual(self.verdict(), "pricing_groups")
        self.db.load_woow()
        self.db.channels[2]["group"] = "vip"
        self.assertIsNone(self.verdict())          # not a usable group: not in the payload
        self.db.options["UserUsableGroups"] = '{"default": "a", "free": "b", "vip": "c"}'
        self.assertEqual(self.verdict(), "pricing_groups")


# Synthetic values files (WOOW's own files are never an input of these tests: a
# WOOW change must not fail them).
SYNTHETIC_WOOW_VALUES = """publicUrl: https://newapi.example.invalid
proxy:
  enabled: true
  replicas: 2
  image:
    tag: 3947d2b08d0813a2fae9f120e8fa67990f86c4cb  # the commit
    digest: sha256:""" + "a" * 64 + """
  patsSecret: newapi-proxy-pats
"""
SYNTHETIC_CHART_VALUES = f"""newapi:
  image:
    repository: docker.io/calciumion/new-api
    tag: {nc.NEWAPI_VERSION}
    # a comment
    digest: {nc.SEED_IMAGES[nc.NEWAPI_VERSION]}
postgres:
  image:
    repository: docker.io/library/postgres
    tag: 16.15-alpine3.24
    digest: sha256:{"7" * 64}
"""


class ImagesTest(unittest.TestCase):
    def test_yaml_image_pins_and_difference(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "deploy"))
        with open(os.path.join(tmp, "deploy", "woow-k3s.yaml"), "w") as f:
            f.write(SYNTHETIC_WOOW_VALUES)
        pins = aps.yaml_images(os.path.join(tmp, "deploy", "woow-k3s.yaml"))
        self.assertEqual(pins, {"proxy.image.tag": "3947d2b08d0813a2fae9f120e8fa67990f86c4cb",
                                "proxy.image.digest": "sha256:" + "a" * 64})
        self.assertEqual(aps.image_alerts(tmp)[0]["severity"], "info")    # no apporo file yet
        text = SYNTHETIC_WOOW_VALUES
        with open(os.path.join(tmp, "deploy", "apporo-k3s.yaml"), "w") as f:
            f.write(text)
        self.assertEqual(aps.image_alerts(tmp), [])
        with open(os.path.join(tmp, "deploy", "apporo-k3s.yaml"), "w") as f:
            f.write(text.replace(pins["proxy.image.digest"], "sha256:" + "1" * 64))
        (al,) = aps.image_alerts(tmp)
        self.assertEqual(al["severity"], "action")
        self.assertIn("proxy.image.digest", al["title"])


class NoticeSentTest(Harness):
    def test_rejects_unknown_and_future_times(self):
        rc, out = self.run_cli("notice-sent", "N-19990101-1", "--effective", "2026-12-01")
        self.assertEqual(rc, 2)
        self.replay()
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")
        nid = next(iter(self.state_data()["notices"]))
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        rc, out = self.run_cli("notice-sent", nid, "--effective", "2099-01-01", "--at", future)
        self.assertEqual(rc, 2)
        self.assertIn("future", out)



# ------------------------------------------------- review round 2 (PR #45)

class OfferedModelsTest(Harness):
    """A price matters while a model is offered: any channel status, and for served_history_days after it left."""

    def setUp(self):
        super().setUp()
        self.replay()

    def test_a_disabled_channel_still_offers_its_models(self):
        self.tgt.channels[1]["status"] = 3                                   # auto-disabled by upstream errors
        self.src.set_map_entry("ModelRatio", "anthropic/claude-sonnet-5.5", 1.5)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mr("anthropic/claude-sonnet-5.5"), 1 / 30)      # held, not applied
        self.assertIn("HOLD  ModelRatio[anthropic/claude-sonnet-5.5]: price increase", out)
        self.tgt.channels[1]["status"] = 1                                   # rc.44 re-enables it
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.mr("anthropic/claude-sonnet-5.5"), 1 / 30)
        self.assertEqual([h["state"] for h in self.state_data()["holds"].values()], ["pending-notice"])

    def test_relisting_a_recent_model_at_a_higher_price_is_held(self):
        ch1 = self.src.channels[1]
        ch1["models"] = ch1["models"].replace(",openai/gpt-6-luna", "")
        ch1["test_model"] = "anthropic/claude-haiku-5.5"
        self.run_cli("daily", "--apply")                                       # day 1: taken off
        self.assertNotIn("openai/gpt-6-luna", self.tgt.channels[1]["models"])
        ch1["models"] += ",openai/gpt-6-luna"                                   # day 2: back at +20 %
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("openai/gpt-6-luna", self.tgt.channels[1]["models"])      # route back at once
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)               # at the old price
        (h,) = self.state_data()["holds"].values()
        self.assertEqual(h["subject"], ["map", "ModelRatio", "openai/gpt-6-luna"])
        notice = read_text(self.state_data()["notices"][h["notice_id"]]["draft"])
        self.assertIn("| `openai/gpt-6-luna` | input | 0.106667 | 0.128 | +20.0% |", notice)

    def test_taking_off_and_raising_then_relisting_is_held(self):
        ch1 = self.src.channels[1]
        ch1["models"] = ch1["models"].replace(",openai/gpt-6-luna", "")
        ch1["test_model"] = "anthropic/claude-haiku-5.5"
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.1)          # same day: off and +100 %
        rc, out = self.run_cli("daily", "--apply")
        self.assertIn("KEPT  ModelRatio[openai/gpt-6-luna]", out)
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)
        ch1["models"] += ",openai/gpt-6-luna"                                   # next day: back
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)
        self.assertIn("HOLD  ModelRatio[openai/gpt-6-luna]: price increase", out)

    def test_a_notified_hold_survives_a_short_delisting(self):
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")
        (h,) = self.state_data()["holds"].values()
        ch1 = self.src.channels[1]
        ch1["models"] = ch1["models"].replace(",openai/gpt-6-luna", "")
        ch1["test_model"] = "anthropic/claude-haiku-5.5"
        self.run_cli("daily", "--apply")                                       # taken off: hold kept
        rc, out = self.send_notice(h["notice_id"])
        self.assertEqual(rc, 0, out)
        self.run_cli("daily", "--apply")
        self.assertEqual(self.state_data()["holds"][h["id"]]["state"], "notified")
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)
        ch1["models"] += ",openai/gpt-6-luna"                                   # back: the matured hold applies
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.06 / 30)
        self.assertEqual(self.state_data()["holds"][h["id"]]["state"], "applied")

    def test_the_first_listing_of_a_model_takes_the_new_price(self):
        self.src.set_map_entry("ModelRatio", "gpt-4", 20)                         # priced, never offered
        self.src.channels[1]["models"] += ",gpt-4"
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mr("gpt-4"), 20 / 30)
        self.assertIn("gpt-4", self.tgt.channels[1]["models"])
        self.assertEqual(self.state_data()["holds"], {})

    def test_the_notice_shows_prices_after_this_runs_decreases(self):
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)         # held
        self.src.set_map_entry("CacheRatio", "openai/gpt-6-luna", 0.05)         # decrease, written today
        self.run_cli("daily", "--apply")
        self.assertEqual(json.loads(self.tgt.options["CacheRatio"])["openai/gpt-6-luna"], 0.05)
        notice = read_text(next(iter(self.state_data()["notices"].values()))["draft"])
        self.assertIn("| `openai/gpt-6-luna` | cache read | 0.005333 | 0.0064 | +20.0% |", notice)


class BillingExpressionTest(Harness):
    """billing_setting.* and rc.44's built-in expressions are real US$ prices: never copied by the sync."""

    def test_replay_refuses_billing_expressions(self):
        self.src.set_map_entry("billing_setting.billing_mode", "anthropic/claude-sonnet-5.5", "tiered_expr")
        self.src.set_map_entry("billing_setting.billing_expr", "anthropic/claude-sonnet-5.5", "p * 3 + c * 15")
        rc, out = self.run_cli("replay", "--yes-bootstrap", auth="password")
        self.assertEqual(rc, 2, out)
        self.assertIn("replay refused: billing-expression: billing_setting.billing_expr", out)
        self.assertEqual(self.tgt.channels, {})
        rc, out = self.run_cli("plan", "--bootstrap")
        self.assertIn("ALERT billing-expression: billing_setting.billing_mode", out)

    def test_a_new_expression_model_is_alert_only(self):
        self.replay()
        s = self.src
        s.channels[1]["models"] += ",x/expr-model"
        s.set_map_entry("billing_setting.billing_mode", "x/expr-model", "tiered_expr")
        s.set_map_entry("billing_setting.billing_expr", "x/expr-model", "p * 3 + c * 15")
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.tgt.options["billing_setting.billing_expr"]), {})
        self.assertNotIn("x/expr-model", self.tgt.channels[1]["models"])
        self.assertIn("ALERT billing_setting.billing_expr[x/expr-model]: billing expressions are real US$", out)
        self.assertIn("ALERT channel 1 model x/expr-model: x/expr-model is billed by a billing expression", out)

    def test_an_offered_models_expression_change_is_alert_only(self):
        self.replay()
        self.src.set_map_entry("billing_setting.billing_mode", "anthropic/claude-sonnet-5.5", "tiered_expr")
        self.src.set_map_entry("billing_setting.billing_expr", "anthropic/claude-sonnet-5.5", "p * 3 + c * 15")
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(json.loads(self.tgt.options["billing_setting.billing_mode"]), {})
        self.assertEqual(self.state_data()["holds"], {})
        self.assertIn("ALERT billing_setting.billing_mode[anthropic/claude-sonnet-5.5]", out)

    def test_a_builtin_expression_model_without_a_ratio_is_not_routed(self):
        self.replay()
        self.src.channels[1]["models"] += ",gpt-6-astra"                          # rc.44 built-in US$ expression
        rc, out = self.run_cli("daily", "--apply")
        self.assertNotIn("gpt-6-astra", self.tgt.channels[1]["models"].split(","))
        self.assertIn("ALERT channel 1 model gpt-6-astra: gpt-6-astra is billed by a billing expression", out)
        self.src.set_map_entry("ModelRatio", "gpt-6-astra", 3)                    # priced: routed normally
        rc, out = self.run_cli("daily", "--apply")
        self.assertIn("gpt-6-astra", self.tgt.channels[1]["models"].split(","))
        self.assertEqual(self.mr("gpt-6-astra"), 3 / 30)

    def test_the_notice_marks_expression_models_by_hand(self):
        self.replay()
        st = {"options": dict(self.tgt.options), "channels": {}, "abilities": []}
        st["options"]["billing_setting.billing_mode"] = nc.go_json({"openai/gpt-6-luna": "tiered_expr"})
        it = aps.item("map", "hold", key="ModelRatio", entry="openai/gpt-6-luna", old=0.05 / 30, new=0.06 / 30,
                      hold_id="H-1")
        text = aps.draft_notice("N-1", [it], st, nc.load_profile(PROFILE), utcnow(), 7,
                                {"openai/gpt-6-luna": {"default"}})
        self.assertIn("| `openai/gpt-6-luna` | billing expression | (by hand) | (by hand) | |", text)
        self.assertIn("BY HAND: openai/gpt-6-luna", text)
        text = aps.draft_notice("N-2", [it], st, nc.load_profile(PROFILE), utcnow(), 7, {})
        self.assertIn("(BY HAND: the held change(s) ModelRatio[openai/gpt-6-luna]", text)


class NoticeEffectiveDateTest(Harness):
    def setUp(self):
        super().setUp()
        self.replay()
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")
        self.nid = next(iter(self.state_data()["notices"]))

    def test_the_effective_date_must_be_seven_days_after_sending(self):
        early = (utcnow() + datetime.timedelta(days=6)).date().isoformat()
        rc, out = self.run_cli("notice-sent", self.nid, "--effective", early)
        self.assertEqual(rc, 2, out)
        self.assertIn("earliest effective date is " + aps.earliest_effective(utcnow(), 7).isoformat(), out)
        self.assertIsNone(self.state_data()["notices"][self.nid]["sent_at"])

    def test_a_later_effective_date_is_honoured(self):
        later = (utcnow() + datetime.timedelta(days=14)).date()
        rc, out = self.run_cli("notice-sent", self.nid, "--effective", later.isoformat())
        self.assertEqual(rc, 0, out)
        (h,) = self.state_data()["holds"].values()
        self.assertEqual(h["eligible_at"], zulu(aps.effective_start(later)))
        self.assertEqual(h["effective_date"], later.isoformat())
        self.run_cli("apply")
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)                 # not before the date

    def test_backdating_needs_a_flag_and_a_notice_is_recorded_once(self):
        st = self.state_data()
        st["notices"][self.nid]["created_at"] = zulu(utcnow() - datetime.timedelta(days=5))
        self.save_state(st)
        sent = (utcnow() - datetime.timedelta(days=3)).isoformat()
        effective = (utcnow() + datetime.timedelta(days=5)).date().isoformat()
        rc, out = self.run_cli("notice-sent", self.nid, "--effective", effective, "--at", sent)
        self.assertEqual(rc, 2, out)
        self.assertIn("--backdated", out)
        rc, out = self.run_cli("notice-sent", self.nid, "--effective", effective, "--at", sent, "--backdated")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("notice-sent", self.nid, "--effective", effective)
        self.assertEqual(rc, 2, out)
        self.assertIn("already recorded", out)

    def test_aoe_start_and_earliest_date(self):
        sent = datetime.datetime(2026, 11, 17, 2, 0, tzinfo=datetime.timezone.utc)     # 10:00 Taipei
        self.assertEqual(aps.earliest_effective(sent, 7), datetime.date(2026, 11, 24))
        self.assertEqual(aps.effective_start(datetime.date(2026, 11, 24)),
                         datetime.datetime(2026, 11, 24, 12, 0, tzinfo=datetime.timezone.utc))
        late = datetime.datetime(2026, 11, 17, 13, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(aps.earliest_effective(late, 7), datetime.date(2026, 11, 25))

    def test_the_profile_cannot_shorten_the_notice(self):
        p = load_json(PROFILE)
        for bad in (0, 3, 6, "7", 7.5):
            p["price_increase_notice_days"] = bad
            with self.assertRaises(nc.ContentError):
                nc.validate_profile(p)


class ReplayGuardTest(Harness):
    def test_replay_is_refused_on_a_synced_target_even_with_a_fresh_state_dir(self):
        self.replay()
        self.run_cli("apply")
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        self.run_cli("daily", "--apply")                                       # hold opened
        other = os.path.join(self.tmp, "other-state")
        out_io = io.StringIO()
        with contextlib.redirect_stdout(out_io), contextlib.redirect_stderr(out_io):
            rc = aps.main(["--state-dir", other, "--target-base", self.api.base, "--auth", "password",
                           "replay", "--yes-bootstrap"])
        self.assertEqual(rc, 2, out_io.getvalue())
        self.assertIn("not a fresh install", out_io.getvalue())
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.05 / 30)

    def test_force_needs_why_and_shows_what_it_overrides(self):
        self.replay()
        self.src.set_map_entry("ModelRatio", "openai/gpt-6-luna", 0.06)
        rc, out = self.run_cli("replay", "--yes-bootstrap", "--force", auth="password")
        self.assertEqual(rc, 2, out)
        self.assertIn("--force needs --why", out)
        rc, out = self.run_cli("replay", "--yes-bootstrap", "--force", "--why", "test: resume", auth="password")
        self.assertEqual(rc, 0, out)
        self.assertIn("OVERRIDE hold ModelRatio[openai/gpt-6-luna]", out)
        self.assertEqual(self.mr("openai/gpt-6-luna"), 0.06 / 30)

    def test_plan_bootstrap_after_a_replay_is_empty(self):
        self.replay()
        rc, out = self.run_cli("plan", "--bootstrap")
        self.assertEqual(rc, 0, out)
        self.assertIn("plan: 0 auto, 0 hold", out)


class FailureTest(Harness):
    def setUp(self):
        super().setUp()
        self.replay()

    def test_an_uncleared_field_is_alert_only(self):
        self.src.channels[1]["test_model"] = None
        rc1, out1 = self.run_cli("daily", "--apply")
        rc2, out2 = self.run_cli("daily", "--apply")
        self.assertEqual((rc1, rc2), (0, 0), out1 + out2)
        self.assertIn("ALERT channel 1 test_model: rc.44's PUT /api/channel/ cannot clear this field", out2)
        self.assertNotIn("LEFT", out2)

    def test_a_failed_write_is_an_alert_and_prices_go_last(self):
        self.src.set_map_entry("ModelRatio", "gpt-4", None)                       # never offered: removed (phase C)
        self.src.set_map_entry("ModelRatio", "anthropic/claude-sonnet-5.5", 0.8)  # decrease (phase A)
        self.src.channels[1]["name"] = "renamed"                                  # channel update (phase B)
        orig = self.api.update_channel
        self.api.update_channel = lambda body: {"success": False, "message": "database is locked"}
        try:
            rc, out = self.run_cli("daily", "--apply")
        finally:
            self.api.update_channel = orig
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.mr("anthropic/claude-sonnet-5.5"), 0.8 / 30)       # phase A written
        self.assertEqual(self.mr("gpt-4"), 15 / 30)                               # phase C not reached
        self.assertIn("LEFT channel 1 name", out)
        self.assertIn("LEFT ModelRatio[gpt-4]", out)
        (al,) = [al for al in self.alerts() if al["title"].startswith("apply failed")]
        self.assertEqual(al["severity"], "action")
        self.assertIn("database is locked", al["title"])
        self.assertIn("option ModelRatio", al["details"][1])
        self.assertIn("ModelRatio[gpt-4]", al["details"][2])
        rc, out = self.run_cli("daily", "--apply")                                # the next run finishes it
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.mr("gpt-4"))
        self.assertEqual(self.tgt.channels[1]["name"], "renamed")

    def test_a_broken_connection_is_an_unknown_outcome_not_a_crash(self):
        class Broken:
            def open(self, req, timeout=None):
                raise http.client.IncompleteRead(b"")
        self.addCleanup(setattr, aps.na, "_OPENER", aps.na._OPENER)
        client = aps.na.Client(self.api.base, token=PAT)
        aps.na._OPENER = Broken()
        ok, msg, ans = aps.api(client, "PUT", "/api/option/", {"key": "RetryTimes", "value": "3"})
        self.assertEqual((ok, ans), (False, {}))
        self.assertIn("IncompleteRead (outcome unknown)", msg)

    def test_an_unexpected_exception_is_an_alert_and_the_journal_says_so(self):
        self.src.options["RetryTimes"] = "3"
        self.src.set_map_entry("ModelRatio", "anthropic/claude-sonnet-5.5", 0.8)
        real = aps.api
        calls = {"n": 0}

        def flaky(client, method, path, body=None, headers=None):
            if method == "PUT" and path == "/api/option/":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise ValueError("unexpected")
            return real(client, method, path, body, headers)
        aps.api = flaky
        self.addCleanup(setattr, aps, "api", real)
        with self.assertRaises(ValueError):
            self.run_cli("daily", "--apply")
        titles = [al["title"] for al in self.alerts()]
        self.assertIn("daily sync failed", titles)
        jdir = os.path.join(self.state, "journal")
        last = sorted(n for n in os.listdir(jdir) if "-apply-" in n)[-1]
        with open(os.path.join(jdir, last)) as f:
            end = [json.loads(line) for line in f][-1]
        self.assertEqual((end["step"], end["ok"], end["error"]), ("end", False, "ValueError"))
        self.assertEqual(self.state_data()["runs"][-1]["error"], "ValueError")

    def test_sigterm_is_reported(self):
        real = aps.plan_all

        def killed(*args, **kw):
            os.kill(os.getpid(), signal.SIGTERM)
            return real(*args, **kw)
        aps.plan_all = killed
        self.addCleanup(setattr, aps, "plan_all", real)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn("SIGTERM", out)
        self.assertIn("daily sync failed", [al["title"] for al in self.alerts()])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_failure_alert_command(self):
        rc, out = self.run_cli("failure-alert", "--reason", "apporo-newapi-sync.service: timeout")
        self.assertEqual(rc, 0, out)
        (al,) = [al for al in self.alerts() if al["title"] == "daily sync unit failed"]
        self.assertIn("timeout", al["details"][0])


class CredentialTest(Harness):
    CF_SECRET = "0123456789abcdef" * 4
    BEARER = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    def test_a_channel_credential_is_never_read_out_copied_or_printed(self):
        self.replay()
        self.src.channels[1]["header_override"] = json.dumps({"CF-Access-Client-Secret": self.CF_SECRET,
                                                              "Authorization": "Bearer " + self.BEARER})
        texts = []
        for args in (("plan",), ("plan", "--json"), ("daily", "--apply"), ("verify",)):
            rc, out = self.run_cli(*args)
            texts.append(out)
        texts.append(json.dumps(self.alerts()))
        for text in texts:
            self.assertNotIn(self.CF_SECRET[:24], text)
            self.assertNotIn(self.BEARER[:20], text)
        self.assertIn("source-channel-unsafe: channel 1", texts[0])
        self.assertIsNone(self.tgt.channels[1]["header_override"])
        snap = nc.read_snapshot(FakePsql(self.src), nc.load_profile(PROFILE))
        self.assertEqual(snap["unsafe_channels"], {1: ["header_override"]})
        self.assertNotIn(self.CF_SECRET, json.dumps(snap))

    def test_non_printable_channel_fields_are_masked(self):
        self.replay()
        self.src.channels[4]["param_override"] = '{"max_tokens": 2048, "stop": ["zz-unique-marker"]}'
        for args in (("plan",), ("plan", "--json"), ("daily", "--apply"), ("verify", "--strict")):
            rc, out = self.run_cli(*args)
            self.assertNotIn("zz-unique-marker", out, args)
        self.assertNotIn("zz-unique-marker", json.dumps(self.alerts()))
        rc, out = self.run_cli("plan")
        self.assertIn("ALERT channel 4 param_override", out)
        self.assertIn("sha256:", out)

    def test_the_real_shapes_are_not_flagged(self):
        db = FakeDB().load_woow()
        self.assertEqual(nc.read_snapshot(FakePsql(db), nc.load_profile(PROFILE))["unsafe_channels"], {})
        for field, value in (("param_override", '{"max_tokens": 2048, "budget_tokens": 1000}'),
                             ("header_override", '{"anthropic-beta": "prompt-caching-2024-07-31"}'),
                             ("model_mapping", '{"gpt-4": "Qwen3-Coder-480B-A35B-Instruct-FP8"}'),
                             ("param_override", '{"system": "basic instructions; token budget is small"}'),
                             ("remark", "LiteLLM key alias newapi-litellm (US$5 budget)")):
            self.assertFalse(nc.channel_field_unsafe(field, value), (field, value))
        for field, value in (("header_override", '{"x-api-key": "abc"}'), ("header_override", '{"X": "Bearer abcdefgh1"}'),
                             ("setting", '{"cookie": "a=b"}'), ("base_url", "https://x.example/v1?api_key=abc"),
                             ("other", "Authorization: Basic Zm9vOmJhcg=="), ("remark", "sk-" + "Q" * 17)):
            self.assertTrue(nc.channel_field_unsafe(field, value), (field, value))

    def test_short_keys_are_redacted(self):
        key = "sk-" + "Q" * 16
        self.assertTrue(nc.UPSTREAM_KEY_RE.fullmatch(key))
        self.assertEqual(nc.redact("echo " + key), "echo [redacted]")


class SQLGuardBypassTest(unittest.TestCase):
    BYPASSES = (
        "SELECT * FROM channels;", "SELECT jsonb_agg(to_jsonb(c)) FROM channels c;",
        "SELECT row_to_json(channels) FROM channels;", "SELECT jsonb_agg(c.*) FROM channels c;",
        "SELECT id FROM public.users;", 'SELECT id FROM "users" u;', "SELECT 1 FROM options, tokens t;",
        "SELECT 1 FROM setups, two_fas x;", "SELECT pg_read_file('/etc/hostname');", "SELECT 1 \\! id;",
        "SELECT 1 \\o /tmp/x;", "SELECT 1 \\gexec;", "SELECT jsonb_agg(channels) FROM channels;",
        "SELECT 1 FROM channels JOIN tokens ON true;", "SELECT E'x' FROM options;", "SELECT $$x$$ FROM options;",
        "SELECT 1 FROM options -- c\n;", "SELECT :foo FROM options;", "SELECT set_config('a', 'b', false);",
        "SELECT id FROM channels AS c;", "SELECT count(id) FROM channels;", 'SELECT "key" FROM channels;',
        "SELECT 1 FROM options UNION SELECT key FROM channels;", "SELECT lo_import('/x');",
        "SELECT id FROM channels WHERE id IN (SELECT id FROM tokens);",
        # round 2: options-only `key` fragments reused on channels (each returns or leaks channel keys)
        'SELECT jsonb_agg(key ORDER BY key COLLATE "C") FROM channels;',
        "SELECT jsonb_build_object('k', (SELECT jsonb_agg(key ORDER BY key COLLATE \"C\") FROM channels))::text;",
        'SELECT jsonb_agg(name ORDER BY key COLLATE "C") FROM channels;',
        "SELECT jsonb_agg(name) FROM channels WHERE key IN ('x');",
        "SELECT jsonb_agg(length(key)) FROM channels;",
        "SELECT jsonb_agg(encode(sha256(convert_to(key, 'UTF8')), 'hex')) FROM channels;",
        "SELECT jsonb_agg(jsonb_build_array(key, value, 'x')) FROM options UNION SELECT jsonb_agg(key) FROM channels;",
        "SELECT jsonb_agg(name) FROM channels UNION SELECT jsonb_agg(id) FROM users;",
        "SELECT jsonb_agg(name) FROM channels WHERE id IN (SELECT id FROM users);",
        "SELECT jsonb_build_object('version', (SELECT version FROM setups ORDER BY id LIMIT 1), "
        "'keys', (SELECT coalesce(jsonb_agg(key ORDER BY key COLLATE \"C\"), '[]'::jsonb) FROM channels))::text;",
    )

    def test_whole_rows_aliases_joins_meta_commands_and_functions_are_refused(self):
        for bad in self.BYPASSES:
            with self.assertRaises(nc.ContentError, msg=bad):
                nc.check_read_only_sql(nc.READ_ONLY_PREAMBLE + bad)
            with self.assertRaises(nc.ContentError, msg=bad):
                nc.check_read_only_sql(bad)

    def test_the_real_queries_cannot_be_bent(self):
        good = nc.sql_content(["ModelRatio", "RetryTimes"], True)
        head, tail = nc.sql_templates()["content+key-hashes"]
        for bad_keys in ("'ModelRatio') UNION SELECT jsonb_agg(key) FROM channels --", "'a'||(SELECT key FROM channels)",
                         "'a' ,'b'", "'a',", "", "NULL,'a'", "'it''s'", "(SELECT key FROM channels)", "'a';'b'",
                         "'a'\n", "'x'::text", "'a' OR true"):
            with self.assertRaises(nc.ContentError, msg=bad_keys):
                nc.check_read_only_sql(head + bad_keys + tail)
        for bad in (good + " ", " " + good, good.replace("length(key)", "left(key, 64)"),
                    good.replace("FROM abilities", "FROM tokens"), good + "\\! id", good[:-1] + " UNION SELECT 1;",
                    nc.sql_option_keys().replace("FROM options", "FROM channels"),
                    nc.sql_option_keys() + "SELECT 1;", good.replace(nc.READ_ONLY_PREAMBLE, "")):
            with self.assertRaises(nc.ContentError, msg=bad[-80:]):
                nc.check_read_only_sql(bad)
        self.assertEqual(nc.check_read_only_sql(good), good)

    def test_literals_cannot_smuggle_anything(self):
        for keys in (["it's"], ["a;b"], ["$$"], ["\\\\! id"], ["*"], ["users x"], ["a') UNION SELECT key"]):
            with self.assertRaises(nc.ContentError, msg=keys):
                nc.sql_content(keys, True)
        for keys in (["ModelRatio"], ["tool_price_setting.prices", "grok.violation_deduction_amount"],
                     ["passkey.rp_id", "A-b:c_d.e"], ["--x"], ["users"]):    # quoted: data, never SQL
            nc.check_read_only_sql(nc.sql_content(keys, True))

    def test_pinned_sql(self):
        sql = nc.READ_ONLY_PREAMBLE + "SELECT 1;"
        nc.check_pinned_sql(sql, nc.sha256_text(sql))
        with self.assertRaises(nc.ContentError):
            nc.check_pinned_sql(sql + " ", nc.sha256_text(sql))


class ExportTargetTest(Harness):
    def test_export_refuses_the_repo_through_a_symlink(self):
        link = os.path.join(self.tmp, "repo-link")
        os.symlink(REPO, link)
        target = os.path.join(link, "snap-test.json")
        rc, out = self.run_cli("export", "--out", target)
        self.assertEqual(rc, 2, out)
        self.assertIn("outside the repository", out)
        self.assertFalse(os.path.exists(os.path.join(REPO, "snap-test.json")))


class MintPatTest(Harness):
    def setUp(self):
        super().setUp()
        self.replay()
        self.written = []
        self.addCleanup(setattr, aps, "SECRET_WRITER", None)

    def test_the_pat_goes_into_the_secret(self):
        def writer(ns, name, manifest):
            self.written.append((ns, name, manifest))
            return True, "created"
        aps.SECRET_WRITER = writer
        rc, out = self.run_cli("mint-sync-pat")
        self.assertEqual(rc, 0, out)
        ((ns, name, manifest),) = self.written
        self.assertEqual((ns, name), ("new-api-apporo", "newapi-sync-pat"))
        self.assertEqual(base64.b64decode(manifest["data"]["token"]).decode(), PAT)
        self.assertIn(7, self.api.pats)

    def test_a_secret_write_failure_revokes_the_new_pat(self):
        aps.SECRET_WRITER = lambda ns, name, manifest: (False, "kubectl create exit 1")
        rc, out = self.run_cli("mint-sync-pat")
        self.assertEqual(rc, 2, out)
        self.assertIn("was revoked", out)
        self.assertEqual(self.api.pats, {})
        self.assertIn(("DELETE", "/api/user/access_tokens/7", None), self.api.requests)


# ------------------------------------------------- review round 3 (PR #45)

APPORO_TOOL_TEXT = nc.go_json({k: v / 30 for k, v in nc.RC44_TOOL_PRICE_SEED.items()})


def rc44_surcharge_quota(price, count=1, group_ratio=1, quota_per_unit=500000):
    """rc.44 service/text_quota.go calculateTextToolCallSurcharge, in decimal."""
    d = decimal.Decimal
    return d(repr(float(price))) * d(count) / d(1000) * d(repr(float(group_ratio))) * d(quota_per_unit)


class ToolPriceTableTest(unittest.TestCase):
    """rc.44 tool price rules (setting/operation_setting/tools.go), and Apporo's map = WOOW's / 30."""

    def test_the_seed_is_the_pinned_rc44_table(self):
        self.assertEqual(nc.sha256_text(nc.go_json(nc.RC44_TOOL_PRICE_SEED)), nc.RC44_TOOL_PRICE_SEED_SHA256)
        self.assertEqual(len(nc.RC44_TOOL_PRICE_SEED), 27)
        self.assertEqual(nc.TOOL_PRICE_SEEDS[nc.NEWAPI_VERSION], nc.RC44_TOOL_PRICE_SEED)
        self.assertRegex(nc.SEED_IMAGES[nc.NEWAPI_VERSION], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(set(nc.SEED_IMAGES), set(nc.TOOL_PRICE_SEEDS))
        self.assertEqual(set(nc.SEED_IMAGES), set(nc.VIOLATION_FEE_DEFAULTS))
        self.assertEqual(nc.VIOLATION_FEE_DEFAULTS[nc.NEWAPI_VERSION], (True, 0.05))
        self.assertEqual(nc.version_of_digest(nc.SEED_IMAGES[RC44]), RC44)
        self.assertIsNone(nc.version_of_digest("sha256:" + "0" * 64))

    @unittest.skipUnless(os.environ.get("NEWAPI_RC44_SRC"), "set NEWAPI_RC44_SRC to a v1.0.0-rc.44 checkout")
    def test_the_tables_match_the_rc44_source(self):
        sys.path.insert(0, os.path.join(REPO, "tests", "apporo_sync"))
        import rc44_price_tables
        self.assertEqual(rc44_price_tables.check(os.environ["NEWAPI_RC44_SRC"])[3], [])

    def test_operator_prices_load_like_rc44(self):
        self.assertEqual(nc.tool_price_overrides(None), ({}, []))
        self.assertEqual(nc.tool_price_overrides("[1]")[0], {})
        self.assertEqual(nc.tool_price_overrides("not json")[0], {})
        self.assertEqual(nc.tool_price_overrides('{"a": NaN}')[0], {})       # rc.44: the whole value is invalid
        prices, ignored = nc.tool_price_overrides(
            '{"web_search": 12, "neg": -1, "s": "3", "b": true, "o": {}, "big": 1e400, "z": 0, "d": 1, "d": 2}')
        self.assertEqual(prices, {"web_search": 12.0, "z": 0.0, "d": 2.0})   # duplicate member: the last wins
        self.assertEqual(ignored, ["b", "big", "neg", "o", "s"])

    def test_lookup_like_rc44(self):
        idx, clashes = nc.tool_price_index(dict(nc.RC44_TOOL_PRICE_SEED, **{"web_search:openai/gpt*": 7.0}))
        self.assertEqual(clashes, [])
        cases = {("web_search_preview", "gpt-4o-mini-search-preview"): (25.0, "web_search_preview:gpt-4o-mini*"),
                 ("web_search_preview", "gpt-4o"): (25.0, "web_search_preview:gpt-4o*"),
                 ("web_search_preview", "openai/gpt-4o"): (10.0, "web_search_preview"),
                 ("web_search_preview", ""): (10.0, "web_search_preview"),
                 ("google_search", "gemini-2.5-pro"): (0.0, "google_search:gemini-2.5*"),    # a matched 0 is final
                 ("google_search", "gemini-3"): (14.0, "google_search"),
                 ("google_search_grounded_prompt", "gemini-3"): (0.0, None),                 # prefixes only
                 ("web_search", "openai/gpt-6-luna"): (7.0, "web_search:openai/gpt*"),
                 ("web_search", "grok-4"): (5.0, "web_search:grok*"),
                 ("no_such_tool", "x"): (0.0, None)}
        for (tool, model), want in cases.items():
            self.assertEqual(nc.tool_price(idx, tool, model), want, (tool, model))
        _, clashes = nc.tool_price_index({"t:abc*": 1.0, "t:abc": 2.0})
        self.assertEqual(clashes, [["t:abc", "t:abc*"]])

    def test_apporo_map_is_woow_effective_prices_divided(self):
        text, ignored, clashes = nc.divided_tool_prices(nc.NEWAPI_VERSION, None, 30)
        self.assertEqual(text, APPORO_TOOL_TEXT)
        self.assertEqual((ignored, clashes), ([], []))
        self.assertIn('"web_search":0.3333333333333333', text)
        self.assertIn('"image_generation":5', text)
        self.assertIn('"google_search:gemini-2.5*":0', text)
        over = '{"web_search": 12, "my_function": 3, "bad": -1}'
        text, ignored, _ = nc.divided_tool_prices(nc.NEWAPI_VERSION, over, 30)
        self.assertEqual(json.loads(text)["web_search"], 12 / 30)
        self.assertEqual(json.loads(text)["my_function"], 3 / 30)
        self.assertNotIn("bad", json.loads(text))
        self.assertEqual(ignored, ["bad"])
        with self.assertRaises(nc.ContentError):
            nc.divided_tool_prices("v1.0.0-rc.45", None, 30)

    def test_every_tool_call_costs_woow_over_30(self):
        for woow_text in (None, '{"web_search": 12, "web_search:openai/*": 20, "my_function": 3}'):
            woow, _ = nc.effective_tool_prices(nc.NEWAPI_VERSION, woow_text)
            text, _, _ = nc.divided_tool_prices(nc.NEWAPI_VERSION, woow_text, 30)
            app, _ = nc.effective_tool_prices(nc.NEWAPI_VERSION, text)      # Apporo's row loaded by rc.44
            w_idx, a_idx = nc.tool_price_index(woow)[0], nc.tool_price_index(app)[0]
            models = nc.tool_probes(w_idx, a_idx) | {"openai/gpt-6-luna", "gpt-4o-search-preview", "grok-4-fast",
                                                    "gemini-2.0-flash", "x/y-search-preview", "anthropic/claude"}
            for tool in set(w_idx) | set(a_idx):
                for m in models:
                    pw, pa = nc.tool_price(w_idx, tool, m)[0], nc.tool_price(a_idx, tool, m)[0]
                    self.assertEqual(pa, pw / 30, (tool, m))
                    qw, qa = rc44_surcharge_quota(pw, 3), rc44_surcharge_quota(pa, 3)
                    self.assertLess(abs(qa - qw / 30), decimal.Decimal("0.000001"), (tool, m))
        self.assertEqual(rc44_surcharge_quota(10), 5000)                   # web_search at WOOW: 5000 quota a call
        self.assertEqual(round(rc44_surcharge_quota(10 / 30)), 167)        # Apporo: 5000 / 30

    def test_formats_like_rc44(self):
        cases = {0.05 / 30: "0.0016666666666666668", 1e-7: "0.0000001", 1e21: "1000000000000000000000",
                 0.0: "0", 150.0: "150", 2.5: "2.5", -0.5: "-0.5", 123456789012345678.0: "123456789012345680"}
        for v, want in cases.items():
            self.assertEqual(nc.go_format_f(v), want, v)
        self.assertEqual(nc.go_parse_float("0.05"), (0.05, None))
        self.assertEqual(nc.go_parse_float("+.5e1"), (5.0, None))
        self.assertEqual(nc.go_parse_float("1_000"), (None, None))          # rc.44 skips it: the default stays
        self.assertEqual(nc.go_parse_float("1e400"), (None, None))
        self.assertEqual(nc.go_parse_float("NaN")[0], None)
        self.assertTrue(nc.go_parse_float("Inf")[1])
        self.assertEqual([nc.go_parse_bool(x) for x in ("true", "T", "0", "no")], [True, True, False, None])
        self.assertEqual(nc.violation_fee({}, RC44), (0.05, None))
        self.assertEqual(nc.violation_fee({nc.VIOLATION_SWITCH_KEY: "false"}, RC44), (0.0, None))
        self.assertEqual(nc.violation_fee({nc.VIOLATION_SWITCH_KEY: "maybe", nc.VIOLATION_FEE_KEY: "0.1"}, RC44),
                         (0.1, None))
        self.assertIsNone(nc.violation_fee({nc.VIOLATION_FEE_KEY: "Inf"}, RC44)[0])
        with self.assertRaises(nc.ContentError):
            nc.violation_fee({}, None)                                     # no version: never a guessed default


class ProfileBoundsTest(unittest.TestCase):
    def test_served_history_days_is_at_least_30(self):
        p = load_json(PROFILE)
        for bad in (0, 1, 7, 29, "30", 30.5, 3651):
            p["served_history_days"] = bad
            with self.assertRaises(nc.ContentError, msg=bad):
                nc.validate_profile(p)
        for good in (30, 45, 3650):
            p["served_history_days"] = good
            nc.validate_profile(p)
        self.assertEqual(aps.history_days({"served_history_days": 7}), 30)   # defence in depth

    def test_price_flags(self):
        p = load_json(PROFILE)
        self.assertIs(p["divide_tool_prices"], True)
        self.assertEqual(p["divided_scalar_prices"], [nc.VIOLATION_FEE_KEY])
        for key, bad in (("divide_tool_prices", "yes"), ("divided_scalar_prices", ["Price"]),
                         ("divided_scalar_prices", "grok.violation_deduction_amount")):
            q = load_json(PROFILE)
            q[key] = bad
            with self.assertRaises(nc.ContentError, msg=(key, bad)):
                nc.validate_profile(q)


class ToolPriceSyncTest(Harness):
    """tool_price_setting.prices and the violation fee in replay, apply, holds, notices and verify."""

    def setUp(self):
        super().setUp()
        self.src.set_map_entry("ModelRatio", "x/old-model", 0.2)
        self.src.channels[1]["models"] += ",x/old-model"
        self.replay()

    def tool_map(self):
        return json.loads(self.tgt.options.get(nc.TOOL_PRICE_KEY) or "{}")

    def set_woow_tools(self, prices):
        self.src.options[nc.TOOL_PRICE_KEY] = json.dumps(prices)

    def test_replay_writes_the_divided_tables_byte_exactly(self):
        self.assertEqual(self.tgt.options[nc.TOOL_PRICE_KEY], APPORO_TOOL_TEXT)
        self.assertEqual(self.tgt.options[nc.VIOLATION_FEE_KEY], "0.0016666666666666668")
        self.assertNotIn(nc.TOOL_PRICE_KEY, self.src.options)              # WOOW has no row: the seed applies
        rc, out = self.run_cli("verify", "--strict", "--check-keys", "-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("ok   prices every tool call price = WOOW's / 30", out)
        self.assertIn("ok   prices violation fee = WOOW's / 30", out)
        put = [r for r in self.api.requests if r[0] == "PUT" and r[2].get("key") == nc.TOOL_PRICE_KEY]
        self.assertEqual([r[2]["value"] for r in put], [APPORO_TOOL_TEXT])

    def test_a_target_replayed_without_the_row_gets_it_as_decreases(self):
        del self.tgt.options[nc.TOOL_PRICE_KEY]
        del self.tgt.options[nc.VIOLATION_FEE_KEY]
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tgt.options[nc.TOOL_PRICE_KEY], APPORO_TOOL_TEXT)
        self.assertEqual(self.tgt.options[nc.VIOLATION_FEE_KEY], "0.0016666666666666668")
        self.assertIn("AUTO  tool_price_setting.prices[web_search]: tool price (web_search, per 1,000 calls) decrease",
                      out)
        self.assertEqual(self.state_data()["holds"], {})
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 0, out)

    def test_an_increase_is_held_until_the_notice_and_a_decrease_applies_at_once(self):
        self.set_woow_tools({"web_search": 12, "web_search_preview:gpt-4o*": 20})
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search"], 10 / 30)                    # held
        self.assertEqual(self.tool_map()["web_search_preview:gpt-4o*"], 20 / 30)    # decrease (no model hit): written
        self.assertIn("HOLD  tool_price_setting.prices[web_search]: tool price (web_search, per 1,000 calls) "
                      "increase", out)
        (h,) = self.state_data()["holds"].values()
        self.assertEqual(h["subject"], ["map", nc.TOOL_PRICE_KEY, "web_search"])
        notice = read_text(self.state_data()["notices"][h["notice_id"]]["draft"])
        self.assertIn("| tool `web_search` | per 1,000 calls | 10.666667 | 12.8 | +20.0% |", notice)
        self.assertIn("per 1,000 tool calls", notice)
        rc, out = self.run_cli("verify")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 1, out)
        rc, out = self.send_notice(h["notice_id"])
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search"], 12 / 30)
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 0, out)
        before = len(self.api.requests)
        self.run_cli("apply")
        self.assertEqual([r for r in self.api.requests[before:] if r[0] != "DELETE"], [])

    def test_a_raise_only_for_unoffered_names_is_automatic_and_one_for_an_offered_model_is_held(self):
        self.set_woow_tools({"web_search_preview:gpt-4o*": 40, "web_search:x/old*": 30})
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search_preview:gpt-4o*"], 40 / 30)    # no offered model starts so
        self.assertIn("no offered model's tool price (web_search_preview, per 1,000 calls) changes", out)
        self.assertNotIn("web_search:x/old*", self.tool_map())                     # x/old-model pays it: held
        self.assertIn("HOLD  tool_price_setting.prices[web_search:x/old*]", out)

    def test_a_raise_that_reaches_only_a_new_model_is_its_first_price(self):
        self.src.set_map_entry("ModelRatio", "y/new-model", 0.3)
        self.src.channels[1]["models"] += ",y/new-model"
        self.set_woow_tools({"web_search:y/*": 40})
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search:y/*"], 40 / 30)
        self.assertIn("first listing of the models it raises", out)

    def test_a_raise_for_a_model_taken_off_is_kept(self):
        ch1 = self.src.channels[1]
        ch1["models"] = ch1["models"].replace(",x/old-model", "")
        self.set_woow_tools({"web_search:x/old*": 30})
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("web_search:x/old*", self.tool_map())
        self.assertIn("ALERT [info] KEPT tool_price_setting.prices[web_search:x/old*]", out)

    def test_changes_that_raise_a_paid_price_together_are_alert_only(self):
        # Synced state: x/old-model is governed by "web_search:x/old*" 25, "web_search:x/*" 5 covers x/ else.
        before = {"web_search:x/old*": 25, "web_search:x/*": 5}
        self.set_woow_tools(before)
        self.tgt.options[nc.TOOL_PRICE_KEY] = nc.divided_tool_prices(nc.NEWAPI_VERSION, json.dumps(before), 30)[0]
        rc, out = self.run_cli("daily", "--apply")
        self.assertNotIn("tool_price_setting.prices[", out)                         # in sync
        # One run: x/new-model (governed by "x/*") is listed and "x/*" rises (alone: its first price), and
        # "x/old*" is removed (alone: x/old-model falls back to 5, a decrease). Together x/old-model would
        # go 25 -> 40 with no notice.
        self.src.set_map_entry("ModelRatio", "x/new-model", 0.3)
        self.src.channels[1]["models"] += ",x/new-model"
        self.set_woow_tools({"web_search:x/*": 40})
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search:x/old*"], 25 / 30)            # nothing of it written
        self.assertEqual(self.tool_map()["web_search:x/*"], 5 / 30)
        self.assertIn("together this run's tool price changes would raise a price without a notice "
                      "(web_search for x/old-model", out)
        self.assertIn("x/new-model", self.tgt.channels[1]["models"])                # the route itself is fine

    def test_the_violation_fee_follows_woow_over_30(self):
        self.src.options[nc.VIOLATION_FEE_KEY] = "0.02"                              # decrease
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.tgt.options[nc.VIOLATION_FEE_KEY], nc.go_format_f(0.02 / 30))
        self.src.options[nc.VIOLATION_FEE_KEY] = "0.1"                               # increase: held
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.tgt.options[nc.VIOLATION_FEE_KEY], nc.go_format_f(0.02 / 30))
        self.assertIn("HOLD  option grok.violation_deduction_amount: content-safety violation fee increase", out)
        (h,) = [h for h in self.state_data()["holds"].values() if h["state"] == "pending-notice"]
        notice = read_text(self.state_data()["notices"][h["notice_id"]]["draft"])
        self.assertIn("| content-safety violation fee | per flagged request | 0.021333 | 0.106667 | +400.0% |", notice)
        self.src.options[nc.VIOLATION_FEE_KEY] = "0.02"                              # WOOW reverts: cancelled
        self.src.options[nc.VIOLATION_SWITCH_KEY] = "false"                          # switched off: a decrease
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.tgt.options[nc.VIOLATION_SWITCH_KEY], "false")
        self.src.options[nc.VIOLATION_SWITCH_KEY] = "true"                           # back on: an increase, held
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(self.tgt.options[nc.VIOLATION_SWITCH_KEY], "false")
        self.assertIn("HOLD  option grok.violation_deduction_enabled", out)

    def test_a_fixed_audio_price_model_is_an_alert(self):
        self.src.set_map_entry("ModelRatio", "gemini-2.5-flash-lite", 0.05)
        self.src.channels[1]["models"] += ",gemini-2.5-flash-lite"
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("ALERT fixed-audio-price: model gemini-2.5-flash-lite", out)


class PriceTableImageTest(unittest.TestCase):
    def tmp_repo(self, chart_text):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "chart"))
        with open(os.path.join(tmp, "chart", "values.yaml"), "w") as f:
            f.write(chart_text)
        return tmp

    def test_the_pinned_new_api_image_is_the_known_one(self):
        chart = SYNTHETIC_CHART_VALUES
        self.assertEqual(aps.seed_image_alerts(self.tmp_repo(chart)), [])
        tmp = self.tmp_repo(chart.replace("tag: v1.0.0-rc.44", "tag: v1.0.0-rc.45"))
        (al,) = aps.seed_image_alerts(tmp)
        self.assertEqual(al["severity"], "action")
        self.assertIn("v1.0.0-rc.45", al["title"])
        self.assertIn("rc44_price_tables.py", " ".join(al["details"]))
        tmp = self.tmp_repo(chart.replace(nc.SEED_IMAGES[nc.NEWAPI_VERSION], "sha256:" + "2" * 64))
        self.assertEqual(len(aps.seed_image_alerts(tmp)), 1)

    def test_a_running_image_without_known_tables_is_an_alert(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake = os.path.join(tmp, "kubectl.py")
        known = "docker.io/calciumion/new-api:v1.0.0-rc.44@" + nc.SEED_IMAGES[nc.NEWAPI_VERSION]
        newer = "docker.io/calciumion/new-api:v1.0.0-rc.45@sha256:" + "3" * 64
        with open(fake, "w") as f:
            f.write("import json, sys\nns = sys.argv[sys.argv.index('-n') + 1]\n"
                    f"img = {newer!r} if ns == 'new-api' else {known!r}\n"
                    "print(json.dumps({'items': [{'kind': 'Deployment', 'metadata': {'name': 'newapi-master'}, "
                    "'spec': {'template': {'spec': {'containers': [{'name': 'new-api', 'image': img}]}}}}]}))\n")
        profile = nc.load_profile(PROFILE)
        alerts = aps.image_alerts(tmp, [sys.executable, fake], True, profile)
        titles = [al["title"] for al in alerts]
        self.assertIn("images (live): Deployment/newapi-master/new-api differs", titles)
        self.assertTrue(any(t.startswith("images (live): WOOW Deployment/newapi-master/new-api runs " + newer)
                            for t in titles), titles)
        self.assertFalse(any(t.startswith("images (live): Apporo") for t in titles), titles)

    def test_a_changed_table_is_announced_once(self):
        state = types_ns(data={})
        self.assertEqual(aps.price_table_alerts(state, nc.NEWAPI_VERSION), [])
        self.assertEqual(aps.price_table_alerts(state, nc.NEWAPI_VERSION), [])
        self.addCleanup(nc.TOOL_PRICE_SEEDS.pop, "v1.0.0-rc.99", None)
        self.addCleanup(nc.VIOLATION_FEE_DEFAULTS.pop, "v1.0.0-rc.99", None)
        nc.TOOL_PRICE_SEEDS["v1.0.0-rc.99"] = dict(nc.RC44_TOOL_PRICE_SEED, web_search=12.0)
        nc.VIOLATION_FEE_DEFAULTS["v1.0.0-rc.99"] = (True, 0.05)
        (al,) = aps.price_table_alerts(state, "v1.0.0-rc.99")
        self.assertIn("v1.0.0-rc.44 -> v1.0.0-rc.99", al["title"])
        self.assertEqual(aps.price_table_alerts(state, "v1.0.0-rc.99"), [])


RC45 = "v1.0.0-rc.45"
RC45_IMAGE = "docker.io/calciumion/new-api:v1.0.0-rc.45@sha256:" + "5" * 64


class RunningVersionTest(Harness):
    """The version a site runs is the image digest its Deployments pin, never setups.version (rc.44
    writes that row once, at /api/setup): the built-in prices follow it; an unknown image stops the sync."""

    def setUp(self):
        super().setUp()
        self.replay()

    def support_rc45(self, seed_changes):
        """The tool learns rc.45 as docs/APPORO-SYNC.md 〈映像〉 says: its digest and its built-in tables."""
        for table, value in ((nc.SEED_IMAGES, RC45_IMAGE.rpartition("@")[2]),
                             (nc.TOOL_PRICE_SEEDS, dict(nc.RC44_TOOL_PRICE_SEED, **seed_changes)),
                             (nc.VIOLATION_FEE_DEFAULTS, (True, 0.05))):
            table[RC45] = value
            self.addCleanup(table.pop, RC45, None)

    @staticmethod
    def upgrade(db):
        db.image, db.running = RC45_IMAGE, RC45           # setups.version keeps saying rc.44

    def tool_map(self):
        return json.loads(self.tgt.options[nc.TOOL_PRICE_KEY])

    def writes(self, since):
        return [r for r in self.api.requests[since:] if r[0] in ("PUT", "POST") and "sessions" not in r[1]]

    def test_woow_running_an_image_the_tool_does_not_know_stops_the_sync(self):
        self.src.image = RC45_IMAGE                       # WOOW upgraded first; the tool does not know rc.45
        self.src.set_map_entry("ModelRatio", "gpt-4", 1)  # a change the sync would otherwise write
        before, reads = len(self.api.requests), len(self.sql_reads)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"new-api runs New API {RC45_IMAGE}: not an image whose rules and built-in prices this tool "
                      "knows", out)
        (al,) = [al for al in self.alerts() if al["title"] == "daily sync failed"]
        self.assertTrue(al["new"])
        self.assertEqual(self.writes(before), [])
        self.assertEqual(self.sql_reads[reads:], [], "no SQL is sent before the versions are known")
        for cmd in (["plan"], ["verify"], ["apply"], ["daily"]):
            rc, out = self.run_cli(*cmd)
            self.assertEqual(rc, 2, (cmd, out))
        self.assertEqual(self.writes(before), [])

    def test_a_supported_upgrade_prices_woow_with_the_version_it_runs(self):
        self.run_cli("daily", "--apply")                  # the record of the built-in tables starts here
        self.support_rc45({"web_search": 8.0})            # rc.45 lowers web_search 10 -> 8
        self.upgrade(self.src)
        self.assertEqual(self.src.version, RC44)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search"], 8 / 30)                       # a decrease: at once
        self.assertIn("AUTO  tool_price_setting.prices[web_search]: tool price (web_search, per 1,000 calls) "
                      "decrease", out)
        self.assertIn(f"ALERT [action] built-in prices changed: New API {RC44} -> {RC45}", out)
        rc, out = self.run_cli("verify", "--strict", "-v")
        self.assertEqual(rc, 0, out)                      # WOOW on rc.45, Apporo still on rc.44: WOOW / 30
        self.assertIn("ok   prices every tool call price = WOOW's / 30", out)
        self.upgrade(self.tgt)                            # Apporo follows: nothing left to write
        before = len(self.api.requests)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(before), [])
        self.assertNotIn("built-in prices changed", out)
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 0, out)

    def test_a_higher_builtin_price_is_held_for_a_notice(self):
        self.run_cli("daily", "--apply")
        self.support_rc45({"web_search": 12.0})
        self.upgrade(self.src)
        rc, out = self.run_cli("daily", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.tool_map()["web_search"], 10 / 30)                      # held
        self.assertIn("HOLD  tool_price_setting.prices[web_search]", out)
        (h,) = self.state_data()["holds"].values()
        notice = read_text(self.state_data()["notices"][h["notice_id"]]["draft"])
        self.assertIn("| tool `web_search` | per 1,000 calls | 10.666667 | 12.8 | +20.0% |", notice)

    def test_apporo_upgraded_before_woow_is_an_alert(self):
        self.support_rc45({"new_tool": 20.0})             # rc.45 bills a tool WOOW's rc.44 does not have
        self.upgrade(self.tgt)
        rc, out = self.run_cli("daily")
        self.assertEqual(rc, 0, out)
        (al,) = [al for al in self.alerts() if al["title"].startswith("prices: Apporo runs New API")]
        self.assertEqual(al["severity"], "action")
        self.assertIn(f"Apporo runs New API {RC45} (WOOW {RC44}): 1 built-in tool price(s) not divided by 30",
                      al["title"])
        self.assertIn("new_tool", al["details"][0])
        rc, out = self.run_cli("verify", "--strict")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL prices every tool call price = WOOW's / 30", out)

    def test_writes_need_the_api_and_the_deployments_to_agree(self):
        self.support_rc45({})
        self.src.set_map_entry("ModelRatio", "gpt-4", 1)  # something to write
        self.tgt.running = RC45                           # the API answers rc.45, the Deployments pin rc.44
        before = len(self.api.requests)
        rc, out = self.run_cli("apply")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"the target's /api/status says New API '{RC45}', its Deployments run '{RC44}'", out)
        self.assertEqual(self.writes(before), [])

    def test_mixed_undigested_missing_or_unlistable_images_stop_the_sync(self):
        cases = (
            ({"Deployment/newapi-master/new-api": RC44_IMAGE, "Deployment/newapi-slave/new-api": RC45_IMAGE},
             "pin 2 different New API images"),
            ({"Deployment/newapi-master/new-api": "docker.io/calciumion/new-api:v1.0.0-rc.44"},
             "without an image digest"),
            ({"StatefulSet/newapi-postgres/postgres": PG_IMAGE}, "no Deployment runs a New API image"),
            (None, "kubectl cannot list its Deployments"),
        )
        for images, why in cases:
            aps.WORKLOAD_READER = lambda ns, images=images: images if ns == "new-api" else self.workloads(ns)
            rc, out = self.run_cli("plan")
            self.assertEqual(rc, 2, (why, out))
            self.assertIn(why, out)
        aps.WORKLOAD_READER = lambda ns: {"Deployment/newapi-master/new-api": RC44_IMAGE,
                                          "Deployment/newapi-slave/new-api": "calciumion/new-api@" +
                                          nc.SEED_IMAGES[RC44]}
        rc, out = self.run_cli("plan")                    # the same digest written differently is one image
        self.assertEqual(rc, 0, out)

    def test_snapshots_record_the_version_their_source_ran(self):
        path = os.path.join(self.tmp, "woow.json")
        rc, out = self.run_cli("export", "--side", "source", "--out", path)
        self.assertEqual(rc, 0, out)
        snap = load_json(path)
        self.assertEqual((snap["running_version"], snap["running_image"], snap["setup_version"]),
                         (RC44, RC44_IMAGE, RC44))
        rc, out = self.run_cli("--source-snapshot", path, "verify", "--strict")
        self.assertEqual(rc, 0, out)
        # an export taken before the tool recorded the running version (setups.version as "version")
        legacy = {k: v for k, v in snap.items() if k not in ("running_version", "running_image", "setup_version")}
        legacy["version"] = RC44
        old = os.path.join(self.tmp, "old.json")
        with open(old, "w") as f:
            json.dump(legacy, f)
        rc, out = self.run_cli("--source-snapshot", old, "verify", "--strict")
        self.assertEqual(rc, 2, out)
        self.assertIn("does not record the New API version its source ran", out)
        rc, out = self.run_cli("--source-snapshot", old, "--source-version", RC44, "verify", "--strict")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("--source-snapshot", old, "--source-version", RC45, "verify")
        self.assertEqual(rc, 2, out)
        self.assertIn("not a version this tool supports", out)
        rc, out = self.run_cli("--source-snapshot", path, "--source-version", "v1.0.0-rc.43", "verify")
        self.assertEqual(rc, 2, out)
        self.assertIn("contradicts", out)
        rc, out = self.run_cli("--source-version", RC44, "verify")
        self.assertEqual(rc, 2, out)
        self.assertIn("only goes with --source-snapshot", out)


def types_ns(**kw):
    class NS:
        pass
    ns = NS()
    ns.__dict__.update(kw)
    return ns


CHECKS_STUB = """import os, sys
code = int(os.environ.get("CHECKS_STUB_EXIT", "0"))
assert sys.argv[1:] == ["--strict"], sys.argv
print("Apporo checks (strict; docs/APPORO-K3S.md)")
print("-- values: Apporo's own values, the AI terms' promises")
if code == 1:
    print("   FAIL 1 of 21 tests failed: test_values_promised_in_the_ai_terms")
    print("-- parity: everything else equals WOOW prod (scripts/apporo_parity.py --strict)")
    print("   FAIL scripts/apporo_parity.py --strict: differs from WOOW prod + the documented differences (exit 1)")
    print("   | DIFFERS values, proxy as committed (enabled: false): the renders differ in 4 line(s)")
    print("   |         values that differ (WOOW prod + documented differences | Apporo):")
    print("   |           retention.days: 180 | 90")
    print("   |         render diff (proxy as committed (enabled: false)), first 60 lines:")
    print("   |           -  days: 180")
    print("FAILED  Apporo checks: values, parity. strict. Do not install or helm upgrade Apporo until they pass")
    print("::error file=deploy/apporo-k3s.yaml,title=Apporo values::1 of 21 tests failed")
sys.exit(code)
"""


PARITY_STUB = """import os, sys
code = int(os.environ.get("PARITY_STUB_EXIT", "0"))
if os.environ.get("PARITY_STUB_SLEEP"):
    import time; time.sleep(float(os.environ["PARITY_STUB_SLEEP"]))
assert sys.argv[1:] == ["--strict"], sys.argv
print("Apporo follows WOOW prod: deploy/apporo-k3s.yaml vs deploy/woow-k3s.yaml + documented differences (strict)")
print("ok      values, proxy switched on")
if code == 1:
    print("DIFFERS values, proxy as committed (enabled: false): the renders differ in 4 line(s)")
    print("        values that differ (WOOW prod + documented differences | Apporo):")
    print("          newapi.resources.limits.memory: \\"2Gi\\" | \\"1Gi\\"")
    print("        render diff (proxy as committed (enabled: false)), first 60 lines:")
    print("          --- WOOW prod + documented differences")
    print("          -          memory: 2Gi")
    print("FAILED  Apporo differs from WOOW prod + the documented differences (newapi.resources.limits.memory)")
if code == 2:
    print("ERROR   deploy/apporo-k3s.yaml does not render in new-api-apporo with proxy as committed: boom")
sys.exit(code)
"""


class ValuesDriftTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.env = dict(os.environ)
        self.addCleanup(os.environ.update, self.env)    # cleanups run last-first: clear, then restore
        self.addCleanup(os.environ.clear)

    def stub(self):
        with open(os.path.join(self.repo, "scripts", "apporo_parity.py"), "w") as f:
            f.write(PARITY_STUB)

    def test_no_values_tool_yet_is_info(self):
        (al,) = REAL_VALUES_ALERTS(self.repo)
        self.assertEqual(al["severity"], "info")
        self.assertIn("apporo_checks.py is not in this checkout", al["title"])

    def test_apporo_checks_is_preferred_and_its_failures_are_quoted(self):
        self.stub()
        with open(os.path.join(self.repo, "scripts", "apporo_checks.py"), "w") as f:
            f.write(CHECKS_STUB)
        os.environ["CHECKS_STUB_EXIT"], os.environ["PARITY_STUB_EXIT"] = "0", "1"   # parity alone would fail
        self.assertEqual(REAL_VALUES_ALERTS(self.repo), [])
        os.environ["CHECKS_STUB_EXIT"] = "1"
        (al,) = REAL_VALUES_ALERTS(self.repo)
        self.assertEqual(al["severity"], "action")
        self.assertIn("fail the Apporo checks or are behind WOOW prod (scripts/apporo_checks.py --strict exit 1)",
                      al["title"])
        self.assertTrue(al["details"][0].startswith("FAILED  Apporo checks: values, parity"))
        self.assertIn("FAIL 1 of 21 tests failed: test_values_promised_in_the_ai_terms", al["details"])
        self.assertIn("retention.days: 180 | 90", al["details"])
        self.assertFalse(any("days: 180" == d.strip("- ") or d.startswith("::") for d in al["details"]))

    def test_in_sync_is_quiet_and_drift_or_errors_are_action_alerts(self):
        self.stub()
        os.environ["PARITY_STUB_EXIT"] = "0"
        self.assertEqual(REAL_VALUES_ALERTS(self.repo), [])
        os.environ["PARITY_STUB_EXIT"] = "1"
        (al,) = REAL_VALUES_ALERTS(self.repo)
        self.assertEqual(al["severity"], "action")
        self.assertIn("behind WOOW prod (scripts/apporo_parity.py --strict exit 1)", al["title"])
        self.assertTrue(al["details"][0].startswith("FAILED  Apporo differs"))
        self.assertIn('newapi.resources.limits.memory: "2Gi" | "1Gi"', al["details"])
        self.assertFalse(any("memory: 2Gi" == d.strip("- ") for d in al["details"]), "no render diff lines")
        os.environ["PARITY_STUB_EXIT"] = "2"
        (al,) = REAL_VALUES_ALERTS(self.repo)
        self.assertIn("do not render", al["title"])
        self.assertIn("boom", al["details"][0])

    def test_a_hanging_check_is_an_alert(self):
        self.stub()
        os.environ["PARITY_STUB_SLEEP"] = "5"
        self.addCleanup(setattr, aps, "VALUES_TIMEOUT", aps.VALUES_TIMEOUT)
        aps.VALUES_TIMEOUT = 1
        (al,) = REAL_VALUES_ALERTS(self.repo)
        self.assertIn("did not finish", al["title"])


class DailyValuesTest(Harness):
    def test_the_daily_run_reports_values_drift(self):
        aps.values_alerts = REAL_VALUES_ALERTS
        self.replay()
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo)
        os.makedirs(os.path.join(repo, "scripts"))
        with open(os.path.join(repo, "scripts", "apporo_parity.py"), "w") as f:
            f.write(PARITY_STUB)
        self.addCleanup(setattr, aps, "REPO", aps.REPO)
        aps.REPO = repo
        old = os.environ.get("PARITY_STUB_EXIT")
        os.environ["PARITY_STUB_EXIT"] = "1"
        self.addCleanup(lambda: os.environ.pop("PARITY_STUB_EXIT") if old is None else
                        os.environ.__setitem__("PARITY_STUB_EXIT", old))
        rc, out = self.run_cli("daily")
        self.assertEqual(rc, 0, out)
        (al,) = [al for al in self.alerts() if al["title"].startswith("values:")]
        self.assertEqual(al["severity"], "action")
        rc, out = self.run_cli("daily")
        als = [al for al in self.alerts() if al["title"].startswith("values:")]
        self.assertEqual([al["new"] for al in als], [True, False])               # e-mail only the first time


class TemplateTest(unittest.TestCase):
    def test_the_daily_templates_default_to_report(self):
        script = read_text(os.path.join(REPO, "scripts", "apporo-sync-daily.sh"))
        self.assertIn('${APPORO_SYNC_MODE:-report}', script)
        self.assertNotIn("APPORO_SYNC_MODE:-apply", script)
        env = read_text(os.path.join(REPO, "deploy", "apporo-sync", "env.example"))
        self.assertIn("\nAPPORO_SYNC_MODE=report\n", env)
        cron = read_text(os.path.join(REPO, "deploy", "apporo-sync", "crontab.example"))
        self.assertIn("umask 077", cron)
        self.assertIn("mkdir -p", cron)

if __name__ == "__main__":
    unittest.main()
