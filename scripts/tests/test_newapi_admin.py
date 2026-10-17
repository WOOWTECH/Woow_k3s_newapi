"""Unit tests for scripts/newapi_admin.py against an in-process fake new-api.

Run: python3 -m unittest discover -s scripts/tests -v
The fake mimics the rc.44 behaviour the tool relies on: GET /api/option/
returns stored strings, PUT stores Interface2String(value), PAT bearer auth,
/api/pricing derives enable_groups from enabled channels.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import newapi_admin as na  # noqa: E402

SETTINGS = os.path.join(REPO, "deploy", "settings.json")
SETTINGS_STG = os.path.join(REPO, "deploy", "settings-stg.json")
PLAN = na.load_json(os.path.join(REPO, "deploy", "pricing", "pricing-plan.json"))
SPEC = na.load_json(os.path.join(REPO, "deploy", "pricing", "channels.json"))
PAT = "nap_" + "T" * 43
UPSTREAM_KEY = "sk-" + "U" * 22
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"


def go_string(v):
    """common.Interface2String for a decoded JSON value."""
    if isinstance(v, str):
        return v
    return na.encode(v)


class FakeNewAPI:
    def __init__(self):
        settings = na.load_layers([SETTINGS, SETTINGS_STG])
        self.options = {k: na.encode(v) for k, v in settings.items()}
        self.options.update({
            "ModelRatio": json.dumps(PLAN["ModelRatio"]),
            "CompletionRatio": json.dumps(PLAN["CompletionRatio"]),
            "CacheRatio": json.dumps(PLAN["CacheRatio"]),
            "ModelPrice": "{}",
            "payment_setting.compliance_confirmed": "false",
            # rc.44: start-up exports of the defaults, never updated
            "group_ratio_setting.group_ratio": '{"default":1,"svip":1,"vip":1}',
            "group_ratio_setting.group_group_ratio": '{"vip":{"edit_this":0.9}}',
            "SMTPFrom": "noreply@example.com",
        })
        self.status = {
            "version": "v1.0.0-rc.44", "quota_per_unit": 500000, "password_login_enabled": True,
            "turnstile_check": False, "password_login_encryption_enabled": False, "enable_batch_update": False,
            "enable_data_export": True, "register_enabled": False, "self_use_mode_enabled": False,
            "quota_display_type": "CUSTOM", "custom_currency_exchange_rate": 32, "checkin_enabled": False,
        }
        self.channels = []
        for i, ch in enumerate(na.wanted_channels(SPEC, PLAN, "stg", "newapi-stg-litellm", False), 1):
            self.channels.append(dict(ch, id=i, key=UPSTREAM_KEY, status=1))
        self.root_2fa = False
        self.pricing_denied = False
        self.html = False  # answer like a web server that is not new-api
        self.requests = []
        self.lock = threading.Lock()

    # -- views
    def option_list(self):
        meta = {m: {"ratio": v, "locked": False} for m, v in json.loads(self.options.get("CompletionRatio", "{}")).items()}
        out = [{"key": k, "value": v} for k, v in self.options.items()]
        return out + [{"key": "CompletionRatioMeta", "value": json.dumps(meta)}]

    def pricing(self):
        groups = {}
        for ch in self.channels:
            if ch.get("status") != 1:
                continue
            for m in na._norm_set(ch["models"]):
                groups.setdefault(m, set()).update(na._norm_set(ch["group"]))
        usable = json.loads(self.options.get("UserUsableGroups", "{}"))
        ratio = json.loads(self.options.get("GroupRatio", "{}"))
        gg = json.loads(self.options.get("GroupGroupRatio") or "{}").get("default", {})
        eff = {g: gg.get(g, r) for g, r in ratio.items() if g in usable}
        data = [{"model_name": m, "enable_groups": sorted(g)} for m, g in groups.items() if g & set(usable)]
        return {"success": True, "data": data, "group_ratio": eff, "usable_group": usable}


def make_handler(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self):
            return self.headers.get("Authorization") in ("Bearer " + PAT, "Bearer " + JWT)

        def _route(self, method):
            u = urllib.parse.urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            with fake.lock:
                fake.requests.append((method, u.path, self.headers.get("Authorization"), body))
                return self._dispatch(method, u.path, urllib.parse.parse_qs(u.query), body)

        def _dispatch(self, method, path, q, body):
            if fake.html:
                raw = b"<!doctype html><title>console</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            if (method, path) == ("GET", "/api/status"):
                return self._send(200, {"success": True, "data": fake.status})
            if (method, path) == ("POST", "/api/user/login"):
                if (body or {}).get("password") != "pw":
                    return self._send(200, {"success": False, "message": "Username or password is incorrect"})
                if fake.root_2fa:
                    return self._send(200, {"success": True, "data": {"require_verification": True}})
                return self._send(200, {"success": True, "data": {"access_token": JWT}})
            if not self._authed():
                return self._send(401, {"success": False, "code": "AUTH_UNAUTHORIZED", "message": "invalid token"})
            if (method, path) == ("GET", "/api/option/"):
                return self._send(200, {"success": True, "data": fake.option_list()})
            if (method, path) == ("PUT", "/api/option/"):
                key = body["key"]
                if key.startswith("payment_setting.compliance_"):
                    return self._send(200, {"success": False, "message": "Compliance fields cannot be changed"})
                fake.options[key] = go_string(body["value"])
                return self._send(200, {"success": True, "message": ""})
            if (method, path) == ("GET", "/api/pricing"):
                if fake.pricing_denied:
                    return self._send(403, {"success": False, "code": "ACCESS_TOKEN_SCOPE_DENIED", "message": "denied"})
                return self._send(200, fake.pricing())
            if (method, path) == ("GET", "/api/channel/"):
                page, size = int(q.get("p", ["1"])[0]), int(q.get("page_size", ["10"])[0])
                items = [{k: v for k, v in ch.items() if k != "key"} for ch in fake.channels]
                return self._send(200, {"success": True, "data": {
                    "items": items[(page - 1) * size:page * size], "total": len(items), "page": page}})
            if (method, path) == ("POST", "/api/channel/"):
                ch = dict(body["channel"], id=len(fake.channels) + 1, status=1)
                ch.setdefault("param_override", None)
                fake.channels.append(ch)
                return self._send(200, {"success": True, "message": ""})
            if (method, path) == ("PUT", "/api/channel/"):
                ch = next(c for c in fake.channels if c["id"] == body["id"])
                ch.update({k: v for k, v in body.items() if k != "key"})
                return self._send(200, {"success": True, "message": ""})
            return self._send(404, {"success": False, "message": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")
    return H


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNewAPI()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.mkdtemp(prefix="na-test-")
        self.pat_file = self.private("pat", PAT + "\n")
        self.outputs = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp)
        for text in self.outputs:  # nothing secret ever reaches the terminal
            self.assertNotIn(PAT, text)
            self.assertNotIn(UPSTREAM_KEY, text)
            self.assertNotIn(JWT, text)

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def private(self, name, text, mode=0o600):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def run_tool(self, *argv, auth=True):
        args = (["--base", self.base, "--token-file", self.pat_file] if auth else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = na.main(args)
            except SystemExit as e:  # argparse
                rc = e.code
        self.outputs += [out.getvalue(), err.getvalue()]
        return rc, out.getvalue(), err.getvalue()

    def verify(self, *extra):
        return self.run_tool("verify-settings", SETTINGS, SETTINGS_STG, *extra)

    def calls(self, method, path):
        return [r for r in self.fake.requests if r[0] == method and r[1] == path]


class TestHelpers(unittest.TestCase):
    def test_encode_matches_go(self):
        self.assertEqual(na.encode(32.0), "32")
        self.assertEqual(na.encode(1.3), "1.3")
        self.assertEqual(na.encode(True), "true")
        self.assertEqual(na.encode({"free": [10, 120]}), '{"free": [10, 120]}')

    def test_same(self):
        self.assertTrue(na.same(True, "true"))
        self.assertFalse(na.same(True, "1"))
        self.assertTrue(na.same(1.3, "1.3"))
        self.assertFalse(na.same(1, "true"))
        self.assertTrue(na.same({"a": [1, 2.0]}, '{"a":[1,2]}'))
        self.assertFalse(na.same({"a": True}, '{"a":1}'))
        self.assertTrue(na.same("CUSTOM", "CUSTOM"))

    def test_redact(self):
        self.assertEqual(na.redact_value("x " + UPSTREAM_KEY + " y"), "x [redacted] y")
        self.assertEqual(na.redact_value(PAT), "[redacted]")
        # model names that merely contain "sk-" stay readable
        self.assertEqual(na.redact_value("openai/gpt-task-mini-preview-0101"), "openai/gpt-task-mini-preview-0101")


class TestAuth(Base):
    def test_pat_file_must_be_0600(self):
        os.chmod(self.pat_file, 0o644)
        rc, _, err = self.run_tool("get-options")
        self.assertEqual(rc, 2)
        self.assertIn("0600", err)
        self.assertEqual(self.fake.requests, [])

    def test_pat_file_must_hold_a_pat(self):
        self.pat_file = self.private("notpat", UPSTREAM_KEY)
        rc, _, err = self.run_tool("get-options")
        self.assertEqual(rc, 2)
        self.assertIn("nap_", err)

    def test_pat_needs_base(self):
        rc, _, err = self.run_tool("--token-file", self.pat_file, "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("--base", err)

    def test_pat_never_logs_in_even_with_2fa(self):
        self.fake.root_2fa = True
        rc, out, _ = self.run_tool("get-options", "GroupRatio")
        self.assertEqual(rc, 0)
        self.assertIn("GroupRatio", out)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])
        self.assertTrue(all(r[2] == "Bearer " + PAT for r in self.fake.requests if r[1] != "/api/status"))

    def test_password_login_with_2fa_points_to_pat(self):
        creds = self.private("creds", f"url={self.base}\nusername=root\npassword=pw\n")
        self.fake.root_2fa = True
        rc, _, err = self.run_tool("--creds", creds, "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("--token-file", err)

    def test_not_a_new_api_origin(self):
        self.fake.html = True
        rc, _, err = self.run_tool("verify-settings", SETTINGS)
        self.assertEqual(rc, 2)
        self.assertIn("not JSON", err)

    def test_password_login_without_2fa(self):
        creds = self.private("creds", f"url={self.base}\nusername=root\npassword=pw\n")
        rc, _, _ = self.run_tool("--creds", creds, "get-options", "RetryTimes", auth=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls("POST", "/api/user/login")), 1)


class TestSnapshot(Base):
    def test_snapshot_is_private_and_has_no_credentials(self):
        self.fake.options["WorkerUrl"] = "https://worker.example/?k=" + UPSTREAM_KEY
        self.fake.options["SMTPAccount"] = PAT
        out_path = os.path.join(self.tmp, "snap.json")
        rc, out, _ = self.run_tool("get-options", "--snapshot", out_path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
        text = self.read(out_path)
        self.assertNotIn("sk-", text)
        self.assertNotIn("nap_", text)
        snap = json.loads(text)
        self.assertEqual(sorted(snap["_snapshot"]["redacted"]), ["SMTPAccount", "WorkerUrl"])
        self.assertEqual(snap["_snapshot"]["version"], "v1.0.0-rc.44")
        self.assertNotIn("CompletionRatioMeta", snap)
        self.assertEqual(snap["ModelRequestRateLimitCount"], "0")

    def test_snapshot_never_overwrites_or_lands_in_repo(self):
        out_path = self.private("exists.json", "{}")
        rc, _, err = self.run_tool("get-options", "--snapshot", out_path)
        self.assertEqual((rc, self.read(out_path)), (2, "{}"))
        rc, _, err = self.run_tool("get-options", "--snapshot", os.path.join(REPO, "deploy", "snap.json"))
        self.assertEqual(rc, 2)
        self.assertIn("outside the deploy repo", err)
        self.assertFalse(os.path.exists(os.path.join(REPO, "deploy", "snap.json")))

    def test_restore_from_snapshot_needs_only(self):
        snap = os.path.join(self.tmp, "snap.json")
        self.assertEqual(self.run_tool("get-options", "--snapshot", snap)[0], 0)
        self.fake.options["ModelRequestRateLimitCount"] = "5"
        rc, _, err = self.run_tool("apply-settings", snap)
        self.assertEqual(rc, 2)
        self.assertIn("--only", err)
        self.assertEqual(self.calls("PUT", "/api/option/"), [])
        rc, out, _ = self.run_tool("apply-settings", "--only", "ModelRequestRateLimitCount", snap)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fake.options["ModelRequestRateLimitCount"], "0")
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)


class TestVerify(Base):
    def test_clean_server_passes(self):
        rc, out, _ = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn(", 0 failed", out)

    def assertFails(self, needle):
        rc, out, _ = self.verify()
        self.assertEqual(rc, 1, out)
        fails = [line for line in out.splitlines() if line.startswith("FAIL")]
        self.assertTrue(any(needle in line for line in fails), out)
        return fails

    def test_rate_limit_count(self):
        self.fake.options["ModelRequestRateLimitCount"] = "5"
        self.assertFails("ModelRequestRateLimitCount")

    def test_markup(self):
        self.fake.options["GroupRatio"] = '{"default":1.2,"internal":1,"free":0}'
        fails = self.assertFails("GroupRatio")
        self.assertTrue(any("markup" in f for f in fails))

    def test_checkin(self):
        self.fake.options["checkin_setting.enabled"] = "true"
        self.fake.status["checkin_enabled"] = True
        self.assertFails("checkin_setting.enabled")

    def test_status_contract_field(self):
        self.fake.status["register_enabled"] = True
        self.assertFails("status.register_enabled")

    def test_runtime_display_type_from_status(self):
        # DisplayInCurrencyEnabled rewrites the effective value only in memory
        self.fake.status["quota_display_type"] = "USD"
        self.assertFails("status.quota_display_type")

    def test_version_gate(self):
        self.fake.status["version"] = "v1.0.0-rc.45"
        fails = self.assertFails("status.version")
        self.assertTrue(any("INVARIANTS_FOR_VERSION" in f for f in fails))

    def test_group_group_ratio(self):
        self.fake.options["GroupGroupRatio"] = '{"default":{"default":1}}'
        self.assertFails("group-group-ratio")

    def test_extra_rate_limit_group(self):
        self.fake.options["ModelRequestRateLimitGroup"] = '{"free":[10,120],"default":[300,120]}'
        self.assertFails("rate-limit-group")

    def test_free_success_must_match_global(self):
        self.fake.options["ModelRequestRateLimitSuccessCount"] = "100"
        self.assertFails("rate-limit-group")

    def test_payments_live_only(self):
        self.fake.options["payment_setting.compliance_confirmed"] = "true"
        self.assertFails("payments-off")

    def test_unknown_settings_key(self):
        extra = self.private("extra.json", json.dumps({"NoSuchOption": 1}))
        rc, out, _ = self.run_tool("verify-settings", SETTINGS, SETTINGS_STG, extra)
        self.assertEqual(rc, 1)
        self.assertIn("NoSuchOption: not returned", out)

    def test_paid_model_in_free_group(self):
        self.fake.channels[1]["models"] += ",openai/gpt-6-sol"
        self.assertFails("free-group-models")

    def test_qwen_in_paid_group(self):
        self.fake.channels[0]["models"] += ",qwen3-4b-aicoder"
        self.assertFails("local-models-free-only")

    def test_paid_channel_disabled(self):
        self.fake.channels[0]["status"] = 2
        self.assertFails("paid-channel")

    def test_plan_ratio_drift_and_fixed_price(self):
        ratios = json.loads(self.fake.options["ModelRatio"])
        ratios["openai/gpt-6-sol"] = 99
        self.fake.options["ModelRatio"] = json.dumps(ratios)
        self.fake.options["ModelPrice"] = '{"openai/gpt-6-luna": 0.01}'
        fails = self.assertFails("ModelRatio")
        self.assertTrue(any("ModelPrice" in f for f in fails))

    def test_no_plan_skips_pricing(self):
        ratios = json.loads(self.fake.options["ModelRatio"])
        ratios["openai/gpt-6-sol"] = 99
        self.fake.options["ModelRatio"] = json.dumps(ratios)
        rc, out, _ = self.verify("--no-plan")
        self.assertEqual(rc, 0, out)

    def test_pricing_page_refused(self):
        self.fake.pricing_denied = True
        self.assertFails("GET /api/pricing")

    def test_read_only(self):
        self.verify()
        self.assertEqual({r[0] for r in self.fake.requests}, {"GET"})


class TestOffline(Base):
    def test_repo_files_pass(self):
        rc, out, _ = self.run_tool("verify-settings", "--offline", SETTINGS, SETTINGS_STG, auth=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fake.requests, [])

    def mutated(self, **changes):
        s = na.load_json(SETTINGS)
        s.update(changes)
        return self.private("settings.json", json.dumps(s))

    def test_markup_change_fails(self):
        p = self.mutated(GroupRatio={"default": 1.2, "internal": 1, "free": 0})
        rc, out, _ = self.run_tool("verify-settings", "--offline", p, auth=False)
        self.assertEqual(rc, 1)
        self.assertIn("markup", out)

    def test_upgrade_values_fail_on_rc44(self):
        p = self.mutated(ModelRequestRateLimitGroup={"default": [300, 120], "free": [10, 10]})
        rc, out, _ = self.run_tool("verify-settings", "--offline", p, auth=False)
        self.assertEqual(rc, 1)
        self.assertIn("rate-limit-group", out)

    def test_qwen_in_paid_channel_spec_fails(self):
        spec = copy.deepcopy(SPEC)
        spec["channels"][1]["group"] = "default,free"
        p = self.private("channels.json", json.dumps(spec))
        rc, out, _ = self.run_tool("verify-settings", "--offline", "--spec", p, SETTINGS, auth=False)
        self.assertEqual(rc, 1)
        self.assertIn("local models only in free", out)


class TestApply(Base):
    def test_apply_settings_layers_and_encodings(self):
        self.fake.options = {k: v for k, v in self.fake.options.items()
                             if k not in na.load_layers([SETTINGS])}
        rc, out, _ = self.run_tool("apply-settings", SETTINGS, SETTINGS_STG)
        self.assertEqual(rc, 0, out)
        puts = {r[3]["key"]: r[3]["value"] for r in self.calls("PUT", "/api/option/")}
        self.assertEqual(len(puts), len(self.calls("PUT", "/api/option/")))  # each key once
        self.assertEqual(puts["ServerAddress"], "https://newapi-stg.woowtech.io")  # later layer wins
        self.assertEqual(puts["GroupRatio"], '{"default": 1.3, "internal": 1, "free": 0}')
        self.assertIs(puts["RegisterEnabled"], False)
        self.assertEqual(self.fake.options["ModelRequestRateLimitGroup"], '{"free": [10, 120]}')
        self.assertEqual(self.verify()[0], 0)

    def test_apply_settings_snapshot_first_and_reports_failures(self):
        bad = self.private("bad.json", json.dumps({"payment_setting.compliance_confirmed": True, "RetryTimes": 3}))
        snap = os.path.join(self.tmp, "before.json")
        rc, out, _ = self.run_tool("apply-settings", "--snapshot", snap, bad)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL payment_setting.compliance_confirmed", out)
        self.assertEqual(self.fake.options["RetryTimes"], "3")
        self.assertEqual(na.load_json(snap)["RetryTimes"], "2")

    def test_apply_pricing_merges_and_backs_up(self):
        ratios = {"some/other-model": 5, "openai/gpt-6-sol": 99}
        self.fake.options["ModelRatio"] = json.dumps(ratios)
        self.fake.options["ModelPrice"] = '{"openai/gpt-6-luna": 0.01, "mj_imagine": 0.1}'
        backups = os.path.join(self.tmp, "backups")
        rc, out, _ = self.run_tool("apply-pricing", "--backup-dir", backups)
        self.assertEqual(rc, 0, out)
        live = json.loads(self.fake.options["ModelRatio"])
        self.assertEqual(live["some/other-model"], 5)
        self.assertEqual(live["openai/gpt-6-sol"], PLAN["ModelRatio"]["openai/gpt-6-sol"])
        self.assertEqual(json.loads(self.fake.options["ModelPrice"]), {"mj_imagine": 0.1})
        (backup,) = os.listdir(backups)
        b = na.load_json(os.path.join(backups, backup))
        self.assertEqual(json.loads(b["ModelRatio"]), ratios)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(backups, backup)).st_mode), 0o600)
        self.assertEqual(self.verify()[0], 0)
        # idempotent: nothing left to write
        n = len(self.calls("PUT", "/api/option/"))
        self.assertEqual(self.run_tool("apply-pricing", "--backup-dir", backups)[0], 0)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), n)
        self.assertEqual(len(os.listdir(backups)), 2)  # a second backup, never an overwrite


class TestChannels(Base):
    def setUp(self):
        super().setUp()
        self.fake.channels = []
        self.key_file = self.private("litellm-key", UPSTREAM_KEY + "\n")

    def apply(self, *extra):
        return self.run_tool("apply-channels", "--label", "stg", "--key-alias", "newapi-stg-litellm", *extra)

    def test_fresh_install_matches_na01(self):
        rc, out, _ = self.apply("--key-file", self.key_file)
        self.assertEqual(rc, 0, out)
        paid, free = self.fake.channels
        self.assertEqual((paid["name"], paid["group"], paid["type"]), ("LiteLLM svc203 · API（可販售）", "default", 62))
        self.assertEqual(na._norm_set(paid["models"]), sorted(PLAN["api"]))
        self.assertNotIn("param_override", [k for k, v in paid.items() if v])
        self.assertEqual((free["group"], free["param_override"], free["test_model"]),
                         ("free", '{"max_tokens": 2048}', "qwen3-4b-aicoder"))
        self.assertEqual(na._norm_set(free["models"]), sorted(PLAN["local"]))
        self.assertEqual(free["remark"], "stg; LiteLLM key alias newapi-stg-litellm")
        self.assertEqual(free["base_url"], "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000")
        self.assertTrue(all(ch["key"] == UPSTREAM_KEY for ch in self.fake.channels))
        self.assertEqual(self.verify()[0], 0)
        # second run: nothing to do, no key needed
        rc, out, _ = self.apply()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 2)
        self.assertEqual(out.count("\nok ") + out.startswith("ok "), 2)

    def test_with_internal_key_from_env(self):
        with mock.patch.dict(os.environ, {"NA_TEST_KEY": UPSTREAM_KEY}):
            rc, out, _ = self.apply("--key-env", "NA_TEST_KEY", "--with-internal")
        self.assertEqual(rc, 0, out)
        self.assertEqual([c["group"] for c in self.fake.channels], ["default", "free", "internal"])
        self.assertEqual(na._norm_set(self.fake.channels[2]["models"]), sorted(PLAN["internal"]))

    def test_key_checks(self):
        os.chmod(self.key_file, 0o644)
        self.assertEqual(self.apply("--key-file", self.key_file)[0], 2)
        self.assertEqual(self.apply()[0], 2)
        bad = self.private("bad-key", "not-a-key")
        rc, _, err = self.apply("--key-file", bad)
        self.assertEqual(rc, 2)
        self.assertNotIn("not-a-key", err)
        self.assertEqual(self.calls("POST", "/api/channel/"), [])

    def test_dry_run_writes_nothing(self):
        rc, out, _ = self.apply("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("would create"), 2)
        self.assertEqual([r for r in self.fake.requests if r[0] != "GET"], [])

    def test_drift_reported_then_updated_key_kept(self):
        self.apply("--key-file", self.key_file)
        paid = self.fake.channels[0]
        paid["models"] += ",qwen3-4b-aicoder,qwen3-4b-cluster1"  # the prod layout before NA-04
        paid["key"] = "sk-" + "P" * 22
        rc, out, _ = self.apply()
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("qwen3-4b-aicoder", out)
        rc, out, _ = self.apply("--update")
        self.assertEqual(rc, 0, out)
        (put,) = self.calls("PUT", "/api/channel/")
        self.assertNotIn("key", put[3])
        self.assertEqual(paid["key"], "sk-" + "P" * 22)
        self.assertEqual(na._norm_set(paid["models"]), sorted(PLAN["api"]))
        self.assertEqual(self.apply()[0], 0)

    def test_duplicate_names_refused(self):
        self.apply("--key-file", self.key_file)
        self.fake.channels.append(dict(self.fake.channels[1], id=3))
        rc, out, _ = self.apply("--update")
        self.assertEqual(rc, 1)
        self.assertIn("2 channels with this name", out)
        self.assertEqual(self.calls("PUT", "/api/channel/"), [])


if __name__ == "__main__":
    unittest.main()
