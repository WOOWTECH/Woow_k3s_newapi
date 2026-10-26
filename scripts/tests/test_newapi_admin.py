"""Unit tests for scripts/newapi_admin.py against an in-process fake new-api.

Run: python3 -m unittest discover -s scripts/tests -v
The fake mimics the rc.44 behaviour the tool relies on: GET /api/option/
returns stored strings, PUT stores Interface2String(value), PAT bearer auth,
password login with a session sid, /api/pricing derives enable_groups from
enabled channels and answers for the caller's group.
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
CHART_VALUES = os.path.join(REPO, "chart", "values.yaml")
PLAN = na.load_json(os.path.join(REPO, "deploy", "pricing", "pricing-plan.json"))
SPEC = na.load_json(os.path.join(REPO, "deploy", "pricing", "channels.json"))
PAT = "nap_" + "T" * 43
UPSTREAM_KEY = "sk-" + "U" * 22
FREE_KEY = "sk-" + "F" * 22
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
SID = "6f1c2d3e-sid"
STG = na.TARGET_URLS["stg"]
PROD = na.TARGET_URLS["prod"]


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
            # rc.44 built-in billing expressions (none for a plan model)
            "billing_setting.billing_mode": '{"gpt-6-astra":"tiered_expr","gpt-image-2":"tiered_expr"}',
            "SMTPFrom": "noreply@example.com",
        })
        self.status = {
            "version": "v1.0.0-rc.44", "quota_per_unit": 500000, "password_login_enabled": True,
            "turnstile_check": False, "password_login_encryption_enabled": False, "enable_batch_update": False,
            "enable_data_export": True, "register_enabled": False, "self_use_mode_enabled": False,
            "quota_display_type": "CUSTOM", "custom_currency_exchange_rate": 32, "checkin_enabled": False,
            "server_address": STG,
        }
        self.channels = []
        for i, ch in enumerate(na.wanted_channels(SPEC, PLAN, "stg", "newapi-stg-litellm", False), 1):
            self.channels.append(dict(ch, id=i, key=UPSTREAM_KEY, status=1))
        self.root_2fa = False
        self.pricing_denied = False
        self.caller_group = "default"
        self.live_group_ratio = None  # in-memory GroupRatio when it no longer equals the option
        self.html = False  # answer like a web server that is not new-api
        self.redirect_to = None  # answer every request with a 302 there
        self.sessions = {}
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
        if self.caller_group not in usable:
            usable[self.caller_group] = na.OWN_GROUP_DESC  # service/group.go
        ratio = self.live_group_ratio or json.loads(self.options.get("GroupRatio", "{}"))
        gg = json.loads(self.options.get("GroupGroupRatio") or "{}").get(self.caller_group, {})
        eff = {g: gg.get(g, r) for g, r in ratio.items() if g in usable}
        ratios = json.loads(self.options.get("ModelRatio") or "{}")
        prices = json.loads(self.options.get("ModelPrice") or "{}")
        data = []
        for m, g in groups.items():
            if not g & set(usable):
                continue
            item = {"model_name": m, "enable_groups": sorted(g), "quota_type": 0, "model_price": 0,
                    "model_ratio": ratios.get(m, 37.5)}
            if m in prices:
                item.update(quota_type=1, model_price=prices[m], model_ratio=0)
            data.append(item)
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
            if fake.redirect_to:
                self.send_response(302)
                self.send_header("Location", fake.redirect_to + path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
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
                fake.sessions[SID] = "active"
                return self._send(200, {"success": True, "data": {"access_token": JWT, "session": {"sid": SID}}})
            if not self._authed():
                return self._send(401, {"success": False, "code": "AUTH_UNAUTHORIZED", "message": "invalid token"})
            if method == "DELETE" and path.startswith("/api/user/sessions/"):
                sid = path.rsplit("/", 1)[1]
                if self.headers.get("Authorization") != "Bearer " + JWT or fake.sessions.get(sid) != "active":
                    return self._send(403, {"success": False, "code": "AUTH_SESSION_REQUIRED", "message": "x"})
                fake.sessions[sid] = "revoked"
                return self._send(200, {"success": True, "data": {"revoked_sid": sid}})
            if (method, path) == ("GET", "/api/option/"):
                return self._send(200, {"success": True, "data": fake.option_list()})
            if (method, path) == ("PUT", "/api/option/"):
                key = body["key"]
                if key.startswith("payment_setting.compliance_"):
                    return self._send(200, {"success": False, "message": "Compliance fields cannot be changed"})
                if key == "ModelRequestRateLimitGroup":
                    # setting/rate_limit.go: CheckModelRequestRateLimitGroup
                    # validates the option before it is stored (rc.44).
                    try:
                        rates = json.loads(go_string(body["value"]))
                        valid = isinstance(rates, dict) and all(
                            isinstance(pair, list) and len(pair) == 2
                            and all(type(n) is int and n <= (2**63 - 1) // 86400 for n in pair)
                            and pair[0] >= 0 and pair[1] >= 1
                            for pair in rates.values())
                    except (ValueError, TypeError):
                        valid = False
                    if not valid:
                        return self._send(200, {"success": False, "message": "invalid group rate limit"})
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
                ch.update(body)
                return self._send(200, {"success": True, "message": ""})
            if (method, path) == ("POST", "/api/user/access_tokens"):
                return self._send(200, {"success": True, "data": {
                    "token": "nap_" + "N" * 43, "item": {"id": 7, "token_hint": "nap_NNNN", "name": "x"}}})
            return self._send(404, {"success": False, "message": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")
    return H


class Server:
    def __init__(self, fake):
        self.fake = fake
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNewAPI()
        self.server = Server(self.fake)
        self.base = self.server.base
        self.tmp = tempfile.mkdtemp(prefix="na-test-")
        self.pat_file = self.private("pat", PAT + "\n")
        self.outputs = []

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp)
        for text in self.outputs:  # nothing secret ever reaches the terminal
            self.assertNotIn(PAT, text)
            self.assertNotIn(UPSTREAM_KEY, text)
            self.assertNotIn(FREE_KEY, text)
            self.assertNotIn(JWT, text)
            self.assertNotIn("pw\n", text)

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

    def creds(self, url=""):
        return self.private("creds", f"url={url}\nusername=root\npassword=pw\n")

    def run_tool(self, *argv, auth=True, target="stg"):
        args = (["--base", self.base, "--token-file", self.pat_file] if auth else [])
        args += (["--target", target] if target else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = na.main(args)
            except SystemExit as e:  # argparse
                rc = e.code
        self.outputs += [out.getvalue(), err.getvalue()]
        return rc, out.getvalue(), err.getvalue()

    def verify(self, *extra):
        return self.run_tool("verify-settings", SETTINGS, SETTINGS_STG, *extra, target=None)

    def calls(self, method, path):
        return [r for r in self.fake.requests if r[0] == method and r[1] == path]

    def writes(self):
        return [r for r in self.fake.requests if r[0] != "GET"]


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
        # LiteLLM keys are token_urlsafe: "-" and "_" inside the first 20 characters
        for key in ("sk-ab_cd-" + "x" * 16, "sk-" + "-" * 3 + "Q" * 20, "sk-_" + "z" * 21):
            self.assertEqual(na.redact_value(f"k={key};"), "k=[redacted];", key)
        # model names that merely contain "sk-" stay readable
        for name in ("openai/gpt-task-mini-preview-0101", "SparkDesk-v3.5", "SparkDesk-v4.0-Ultra-long-name-x"):
            self.assertEqual(na.redact_value(name), name)

    def test_redact_tree(self):
        answer = {"success": True, "data": {"token": "nap_" + "N" * 43, "item": {"id": 7, "token_hint": "nap_NNNN"},
                                            "access_token": "abc", "key": "plainhexkey0123",
                                            "list": [{"key": "GroupRatio", "value": "{}"}, "x " + UPSTREAM_KEY]}}
        out = na.redact_tree(answer)
        self.assertEqual(out["data"]["token"], "[redacted]")
        self.assertEqual(out["data"]["access_token"], "[redacted]")
        self.assertEqual(out["data"]["key"], "[redacted]")
        self.assertEqual(out["data"]["item"], {"id": 7, "token_hint": "nap_NNNN"})
        self.assertEqual(out["data"]["list"][0], {"key": "GroupRatio", "value": "{}"})
        self.assertEqual(out["data"]["list"][1], "x [redacted]")

    def test_target_urls_match_the_settings_files(self):
        self.assertEqual(na.load_layers([SETTINGS])["ServerAddress"], PROD)
        self.assertEqual(na.load_layers([SETTINGS, SETTINGS_STG])["ServerAddress"], STG)
        self.assertEqual(na.target_of("https://NEWAPI.woowtech.io/"), "prod")
        self.assertIsNone(na.target_of("http://localhost:3000"))

    def test_chart_pins_the_validated_version(self):
        self.assertEqual(na.chart_image_tag(CHART_VALUES), na.INVARIANTS_FOR_VERSION)
        self.assertIsNone(na.check_version_pin(CHART_VALUES))


class TestTransport(Base):
    def test_base_is_required_everywhere(self):
        creds = self.creds(PROD)  # the url= line is never a fallback
        for argv in (["get-options"], ["verify-settings", SETTINGS], ["apply-settings", SETTINGS],
                     ["apply-pricing"], ["apply-channels", "--dry-run"], ["call", "GET", "/api/status"]):
            for auth in (["--token-file", self.pat_file], ["--creds", creds], []):
                rc, _, err = self.run_tool(*auth, *argv, auth=False)
                self.assertEqual(rc, 2, (argv, auth))
                self.assertIn("--base is required", err)
        self.assertEqual(self.fake.requests, [])

    def test_no_credentials_no_default(self):
        rc, _, err = self.run_tool("--base", self.base, "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("no credentials", err)
        self.assertEqual(self.fake.requests, [])

    def test_plain_http_only_to_loopback(self):
        for base in ("http://newapi.woowtech.io", "http://10.0.0.5:3000", "https://x.example/api", "ftp://127.0.0.1"):
            rc, _, err = self.run_tool("--base", base, "--token-file", self.pat_file, "get-options", auth=False)
            self.assertEqual(rc, 2, base)
            self.assertIn("--base", err)

    def test_redirect_never_carries_the_token(self):
        other = FakeNewAPI()
        receiver = Server(other)
        try:
            self.fake.redirect_to = receiver.base
            rc, _, err = self.run_tool("get-options", target=None)
            self.assertEqual(rc, 2)
            self.assertIn("redirect", err)
            self.assertEqual(other.requests, [])  # nothing, and so no Authorization, reached the other origin
        finally:
            receiver.stop()


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

    def test_pat_never_logs_in_even_with_2fa(self):
        self.fake.root_2fa = True
        rc, out, _ = self.run_tool("get-options", "GroupRatio")
        self.assertEqual(rc, 0)
        self.assertIn("GroupRatio", out)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])
        self.assertTrue(all(r[2] == "Bearer " + PAT for r in self.fake.requests if r[1] != "/api/status"))

    def test_password_login_with_2fa_points_to_pat(self):
        self.fake.root_2fa = True
        rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(), "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("--token-file", err)

    def test_creds_file_must_be_0600(self):
        creds = self.creds()
        os.chmod(creds, 0o640)
        rc, _, err = self.run_tool("--base", self.base, "--creds", creds, "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("0600", err)
        self.assertEqual(self.fake.requests, [])

    def test_prod_password_never_sent_to_stg(self):
        rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(PROD), "get-options", auth=False)
        self.assertEqual(rc, 2)
        self.assertIn("refusing to send that password", err)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])
        rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(STG), "apply-pricing",
                                   auth=False, target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])

    def test_not_a_new_api_origin(self):
        self.fake.html = True
        rc, _, err = self.run_tool("verify-settings", SETTINGS)
        self.assertEqual(rc, 2)
        self.assertIn("not JSON", err)

    def test_password_login_session_is_revoked(self):
        rc, _, _ = self.run_tool("--base", self.base, "--creds", self.creds(STG), "get-options", "RetryTimes",
                                 auth=False, target=None)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls("POST", "/api/user/login")), 1)
        (rev,) = self.calls("DELETE", f"/api/user/sessions/{SID}")
        self.assertEqual(rev[2], "Bearer " + JWT)
        self.assertEqual(self.fake.sessions[SID], "revoked")

    def test_session_revoked_after_a_failed_check_too(self):
        self.fake.options["ModelRequestRateLimitCount"] = "5"
        rc, _, _ = self.run_tool("--base", self.base, "--creds", self.creds(), "verify-settings", SETTINGS,
                                 SETTINGS_STG, auth=False, target=None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.fake.sessions[SID], "revoked")


class TestTarget(Base):
    def test_writes_need_target(self):
        for argv in (["apply-settings", SETTINGS, SETTINGS_STG], ["apply-pricing"],
                     ["apply-channels", "--key-alias", "a", "--update"], ["call", "PUT", "/api/option/", "{}"]):
            rc, out, err = self.run_tool(*argv, target=None)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--target", err)
            self.assertIn("target: " + self.base, out + err)  # what it would have written to
            self.assertIn("ServerAddress " + STG, out + err)
        self.assertEqual(self.writes(), [])

    def test_target_mismatch_refused(self):
        rc, _, err = self.run_tool("apply-settings", SETTINGS, target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("refusing to write", err)
        self.assertEqual(self.writes(), [])

    def test_stg_layer_never_applied_to_prod(self):
        self.fake.status["server_address"] = PROD
        rc, _, err = self.run_tool("apply-settings", SETTINGS, SETTINGS_STG, target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("settings-stg.json", err)
        rc, _, err = self.run_tool("apply-settings", "--only", "RetryTimes", SETTINGS, SETTINGS_STG, target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(self.writes(), [])

    def test_stg_without_its_layer_refused(self):
        rc, _, err = self.run_tool("apply-settings", SETTINGS)
        self.assertEqual(rc, 2)
        self.assertIn("ServerAddress", err)
        self.assertEqual(self.writes(), [])

    def test_fresh_install_takes_the_named_target(self):
        self.fake.status["server_address"] = "http://localhost:3000"
        rc, out, _ = self.run_tool("apply-settings", SETTINGS, SETTINGS_STG)
        self.assertEqual(rc, 0, out)
        self.assertIn("a fresh install?", out)

    def test_dry_run_needs_no_target(self):
        rc, out, _ = self.run_tool("apply-channels", "--dry-run", target=None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(), [])


class TestSnapshot(Base):
    def test_snapshot_is_private_and_has_no_credentials(self):
        self.fake.options["WorkerUrl"] = "https://worker.example/?k=" + UPSTREAM_KEY
        self.fake.options["SMTPAccount"] = PAT
        self.fake.options["SomeNote"] = "key sk-ab_cd-efghijklmnopqrstu"
        out_path = os.path.join(self.tmp, "snap.json")
        rc, out, _ = self.run_tool("get-options", "--snapshot", out_path, target=None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
        text = self.read(out_path)
        self.assertNotIn("sk-", text)
        self.assertNotIn("nap_", text)
        snap = json.loads(text)
        self.assertEqual(sorted(snap["_snapshot"]["redacted"]), ["SMTPAccount", "SomeNote", "WorkerUrl"])
        self.assertEqual(snap["_snapshot"]["version"], "v1.0.0-rc.44")
        self.assertEqual(snap["_snapshot"]["server_address"], STG)
        self.assertEqual(sorted(snap["_snapshot"]["excluded"]), sorted(na.NEVER_WRITE))
        for k in na.NEVER_WRITE:
            self.assertNotIn(k, snap)
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
        self.assertEqual(self.writes(), [])
        rc, out, _ = self.run_tool("apply-settings", "--only", "ModelRequestRateLimitCount", snap)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fake.options["ModelRequestRateLimitCount"], "0")
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)

    def test_restore_keys_of_a_settings_file(self):
        """NA-04 rollback: put back every key deploy/settings.json sets."""
        snap = os.path.join(self.tmp, "before.json")
        self.assertEqual(self.run_tool("get-options", "--snapshot", snap)[0], 0)
        s = na.load_json(snap)
        del s["DataExportEnabled"]  # e.g. a key added to settings.json after the snapshot
        os.chmod(snap, 0o600)
        with open(snap, "w") as f:
            json.dump(s, f)
        self.fake.options["ModelRequestRateLimitCount"] = "5"
        self.fake.options["GroupRatio"] = '{"default":1.2,"internal":1,"free":0}'
        rc, out, _ = self.run_tool("apply-settings", "--only-keys-of", SETTINGS, snap)
        self.assertEqual(rc, 0, out)
        self.assertIn("skip DataExportEnabled", out)
        keys = [r[3]["key"] for r in self.calls("PUT", "/api/option/")]
        self.assertEqual(sorted(keys), sorted(k for k in na.load_layers([SETTINGS]) if k != "DataExportEnabled"))
        self.assertEqual(self.verify()[0], 0)

    def test_stale_group_exports_never_written(self):
        """A PUT of either would replace the live group ratios on rc.44."""
        for key, value in (("group_ratio_setting.group_ratio", '{"default":1}'),
                           ("group_ratio_setting.group_group_ratio", "{}")):
            f = self.private("stale.json", json.dumps({key: value, "RetryTimes": 2}))
            rc, _, err = self.run_tool("apply-settings", "--only", "RetryTimes", f)
            self.assertEqual(rc, 2)
            self.assertIn(key, err)
            rc, _, err = self.run_tool("apply-settings", "--only", key, SETTINGS, SETTINGS_STG)
            self.assertEqual(rc, 2)
            self.assertIn("never be written", err)
        self.assertEqual(self.writes(), [])


class TestVerify(Base):
    def test_clean_server_passes(self):
        rc, out, _ = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn(", 0 failed", out)
        self.assertIn("ServerAddress " + STG, out)

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

    def test_live_group_ratio_reset_through_stale_key(self):
        """GroupRatio still reads 1.3 while the in-memory map was replaced:
        only the effective ratio in /api/pricing shows it."""
        self.fake.live_group_ratio = {"default": 1, "svip": 1, "vip": 1}  # PUT group_ratio_setting.group_ratio
        fails = self.assertFails("group_ratio")
        self.assertFalse(any("invariant" in f for f in fails), fails)  # every option still looks right
        self.assertTrue(any("effective default = 1" in f for f in fails))

    def test_special_usable_groups_only_remove(self):
        for value, needle in (('{"free_blocked":{"-:free":""},"default":{"+:internal":"x"}}', "adds groups"),
                              ('{"free_blocked":{"-:free":""},"default":{"internal":"x"}}', "adds groups"),
                              ("{}", "free_blocked does not remove free")):
            self.fake.options["group_ratio_setting.group_special_usable_group"] = value
            fails = self.assertFails("special-usable-groups")
            self.assertTrue(any(needle in f for f in fails), (value, fails))

    def test_extra_rate_limit_group(self):
        self.fake.options["ModelRequestRateLimitGroup"] = '{"free":[10,120],"default":[300,120]}'
        self.assertFails("rate-limit-group")

    def test_free_per_minute_is_d11(self):
        self.fake.options["ModelRequestRateLimitGroup"] = '{"free":[20,120]}'
        fails = self.assertFails("rate-limit-group")
        self.assertTrue(any("D11" in f for f in fails))

    def test_free_success_must_match_global(self):
        self.fake.options["ModelRequestRateLimitSuccessCount"] = "100"
        self.assertFails("rate-limit-group")

    def test_payments_live_only(self):
        self.fake.options["payment_setting.compliance_confirmed"] = "true"
        self.assertFails("payments-off")

    def test_unknown_settings_key(self):
        extra = self.private("extra.json", json.dumps({"NoSuchOption": 1}))
        rc, out, _ = self.run_tool("verify-settings", SETTINGS, SETTINGS_STG, extra, target=None)
        self.assertEqual(rc, 1)
        self.assertIn("NoSuchOption: not returned", out)

    def test_paid_model_in_free_group(self):
        self.fake.channels[1]["models"] += ",openai/gpt-6-sol"
        self.assertFails("free-group-models")

    def test_qwen_in_paid_group(self):
        self.fake.channels[0]["models"] += ",qwen3-4b-aicoder"
        self.assertFails("local-models-free-only")

    def test_zero_ratio_model_in_paid_group(self):
        """rc.44 bills ModelRatio 0 as free in any group (NA-02 review #13)."""
        self.fake.channels[0]["models"] += ",openrouter/free"
        ratios = json.loads(self.fake.options["ModelRatio"])
        ratios["openrouter/free"] = 0
        self.fake.options["ModelRatio"] = json.dumps(ratios)
        fails = self.assertFails("paid-models-priced")
        self.assertTrue(any("openrouter/free" in f for f in fails))
        self.fake.options["ModelPrice"] = '{"openrouter/free": 0}'
        self.assertFails("paid-models-priced")
        self.fake.options["ModelPrice"] = '{"openrouter/free": 0.002}'
        rc, out, _ = self.verify("--no-plan")
        self.assertEqual(rc, 0, out)

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

    def test_billing_expression_on_a_plan_model(self):
        mode = json.loads(self.fake.options["billing_setting.billing_mode"])
        mode["openai/gpt-6-astra"] = "tiered_expr"
        self.fake.options["billing_setting.billing_mode"] = json.dumps(mode)
        fails = self.assertFails("billing_setting.billing_mode")
        self.assertTrue(any("openai/gpt-6-astra" in f for f in fails))
        mode["openai/gpt-6-astra"] = "ratio"  # explicit ratio mode is the plan
        self.fake.options["billing_setting.billing_mode"] = json.dumps(mode)
        self.assertEqual(self.verify()[0], 0)

    def test_pricing_page_ignores_the_callers_own_group(self):
        self.fake.caller_group = "vip"  # root outside default: rc.44 adds it as "User group"
        rc, out, _ = self.verify("-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("caller's own group vip ignored", out)
        users = json.loads(self.fake.options["UserUsableGroups"])
        users["vip"] = "VIP"  # a real extra selectable group is not ignored
        self.fake.options["UserUsableGroups"] = json.dumps(users)
        self.assertFails("usable_group")

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
    def offline(self, *argv):
        return self.run_tool("verify-settings", "--offline", *argv, auth=False, target=None)

    def test_repo_files_pass(self):
        rc, out, _ = self.offline(SETTINGS, SETTINGS_STG)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fake.requests, [])

    def mutated(self, **changes):
        s = na.load_json(SETTINGS)
        s.update(changes)
        return self.private("settings.json", json.dumps(s))

    def test_markup_change_fails(self):
        rc, out, _ = self.offline(self.mutated(GroupRatio={"default": 1.2, "internal": 1, "free": 0}))
        self.assertEqual(rc, 1)
        self.assertIn("markup", out)

    def test_upgrade_values_fail_on_rc44(self):
        rc, out, _ = self.offline(self.mutated(ModelRequestRateLimitGroup={"default": [300, 120], "free": [10, 10]}))
        self.assertEqual(rc, 1)
        self.assertIn("rate-limit-group", out)

    def test_free_per_minute_change_fails(self):
        rc, out, _ = self.offline(self.mutated(ModelRequestRateLimitGroup={"free": [20, 120]}))
        self.assertEqual(rc, 1)
        self.assertIn("D11", out)

    def test_special_usable_group_emptied_fails(self):
        rc, out, _ = self.offline(self.mutated(**{na.SPECIAL_USABLE_KEY: {}}))
        self.assertEqual(rc, 1)
        self.assertIn("special-usable-groups", out)

    def test_qwen_in_paid_channel_spec_fails(self):
        spec = copy.deepcopy(SPEC)
        spec["channels"][1]["group"] = "default,free"
        p = self.private("channels.json", json.dumps(spec))
        rc, out, _ = self.offline("--spec", p, SETTINGS)
        self.assertEqual(rc, 1)
        self.assertIn("local models only in free", out)

    def test_zero_ratio_model_in_paid_plan_fails(self):
        plan = copy.deepcopy(PLAN)
        plan["api"].append(plan["unpriced"][0])
        plan["ModelRatio"][plan["unpriced"][0]] = 0
        p = self.private("plan.json", json.dumps(plan))
        rc, out, _ = self.offline("--plan", p, SETTINGS)
        self.assertEqual(rc, 1)
        self.assertIn("paid groups serve only priced models", out)

    def test_plan_sells_no_zero_cost_model(self):
        self.assertTrue(PLAN["unpriced"])
        self.assertFalse(set(PLAN["unpriced"]) & set(PLAN["api"]))
        self.assertTrue(all(PLAN["ModelRatio"][m] > 0 for m in PLAN["api"] + PLAN["local"]))

    def test_chart_version_bump_without_invariants_fails(self):
        with open(CHART_VALUES) as f:
            text = f.read().replace(f"tag: {na.INVARIANTS_FOR_VERSION}", "tag: v1.0.0-rc.45", 1)
        p = self.private("values.yaml", text)
        rc, out, _ = self.offline("--chart", p, SETTINGS)
        self.assertEqual(rc, 1)
        self.assertIn("INVARIANTS_FOR_VERSION", out)


class TestApply(Base):
    def test_apply_settings_layers_and_encodings(self):
        self.fake.options = {k: v for k, v in self.fake.options.items()
                             if k not in na.load_layers([SETTINGS])}
        rc, out, _ = self.run_tool("apply-settings", SETTINGS, SETTINGS_STG)
        self.assertEqual(rc, 0, out)
        puts = {r[3]["key"]: r[3]["value"] for r in self.calls("PUT", "/api/option/")}
        self.assertEqual(len(puts), len(self.calls("PUT", "/api/option/")))  # each key once
        self.assertEqual(puts["ServerAddress"], STG)  # later layer wins
        self.assertEqual(puts["GroupRatio"], '{"default": 1.3, "internal": 1, "free": 0}')
        self.assertEqual(puts[na.SPECIAL_USABLE_KEY], '{"free_blocked": {"-:free": ""}}')
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


class TestCall(Base):
    def test_call_redacts_by_default(self):
        rc, out, _ = self.run_tool("call", "POST", "/api/user/access_tokens", '{"name":"x"}')
        self.assertEqual(rc, 0, out)
        self.assertNotIn("nap_" + "N" * 43, out)
        self.assertIn('"token": "[redacted]"', out)
        self.assertIn('"token_hint": "nap_NNNN"', out)

    def test_call_raw_warns(self):
        rc, out, err = self.run_tool("call", "--raw", "POST", "/api/user/access_tokens", '{"name":"x"}')
        self.assertEqual(rc, 0, out)
        self.assertIn("nap_" + "N" * 43, out)
        self.assertIn("not redacted", err)

    def test_call_refuses_the_stale_group_exports(self):
        for key in sorted(na.NEVER_WRITE):
            rc, _, err = self.run_tool("call", "PUT", "/api/option/", json.dumps({"key": key, "value": "{}"}))
            self.assertEqual(rc, 2, key)
            self.assertIn("never be written", err)
        self.assertEqual(self.writes(), [])

    def test_get_call_reads_without_target(self):
        rc, out, _ = self.run_tool("call", "GET", "/api/option/", target=None)
        self.assertEqual(rc, 0, out)
        self.assertIn('"key": "GroupRatio"', out)


class TestProdDenyOnly(Base):
    def setUp(self):
        super().setUp()
        self.fake.status["server_address"] = PROD

    def test_every_cli_mutation_is_rejected_before_an_authenticated_request(self):
        cases = [
            ("apply-settings", SETTINGS),
            ("apply-settings", "--only", "RetryTimes", SETTINGS),
            ("apply-pricing",),
            ("apply-channels", "--update"),
            ("call", "PUT", "/api/option/", '{"key":"GroupRatio","value":"{}"}'),
            ("call", "POST", "/api/user/access_tokens", '{"name":"x"}'),
            ("call", "DELETE", "/api/channel/1"),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.fake.requests.clear()
                rc, _, err = self.run_tool(*args, target="prod")
                self.assertEqual(rc, 2)
                self.assertIn("production writes are disabled", err)
                self.assertEqual(self.writes(), [])

    def test_snapshot_and_backup_are_not_created_before_denial(self):
        snapshot = os.path.join(self.tmp, "snapshot.json")
        backup_dir = os.path.join(self.tmp, "backup")
        rc, _, _ = self.run_tool("apply-settings", "--snapshot", snapshot, SETTINGS, target="prod")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(snapshot))
        rc, _, _ = self.run_tool("apply-pricing", "--backup-dir", backup_dir, target="prod")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(backup_dir))

    def test_production_reads_still_work(self):
        rc, _, _ = self.run_tool("get-options", "GroupRatio", target="prod")
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_tool("call", "GET", "/api/status", target="prod")
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_tool("call", "GET", "/api/option/", target="prod")
        self.assertEqual(rc, 0)
        self.run_tool("apply-channels", "--dry-run", target="prod")
        self.assertTrue(self.calls("GET", "/api/channel/"))
        self.assertEqual(self.writes(), [])

    def test_production_raw_get_requires_known_read_only_path(self):
        for target in ("prod", "stg", None):
            with self.subTest(target=target):
                self.fake.requests.clear()
                rc, _, err = self.run_tool("call", "GET", "/api/user/aff", target=target)
                self.assertEqual(rc, 2)
                self.assertIn("production raw GET", err)
                self.assertEqual(self.calls("GET", "/api/user/aff"), [])
        for path in ("/api/status?mode=write", "/api/option/../user/aff"):
            with self.subTest(path=path):
                rc, _, err = self.run_tool("call", "GET", path, target="prod")
                self.assertEqual(rc, 2)
                self.assertIn("production raw GET", err)

    def test_unknown_server_address_is_not_a_prod_bypass(self):
        self.fake.status["server_address"] = "http://localhost:3000"
        rc, _, err = self.run_tool("apply-settings", SETTINGS, target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("production writes are disabled", err)
        self.assertEqual(self.writes(), [])

    def test_password_login_is_not_attempted_before_denial(self):
        rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(url=PROD),
                                   "apply-settings", SETTINGS, auth=False, target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("production access requires --token-file", err)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])
        self.assertEqual(self.writes(), [])

    def test_production_read_rejects_password_session_creation(self):
        for command in (("get-options", "GroupRatio"), ("apply-channels", "--dry-run")):
            with self.subTest(command=command):
                self.fake.requests.clear()
                rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(url=PROD),
                                           *command, auth=False, target=None)
                self.assertEqual(rc, 2)
                self.assertIn("production access requires --token-file", err)
                self.assertEqual(self.calls("POST", "/api/user/login"), [])
                self.assertEqual(self.writes(), [])


class TestDisplaySettings(Base):
    def setUp(self):
        super().setUp()
        self.fake.status.update(server_address=PROD, quota_display_type="USD", custom_currency_exchange_rate=1)
        self.fake.options.update({"ServerAddress": PROD, "GroupRatio": '{"default":1,"free":0}',
                                  "general_setting.quota_display_type": "USD",
                                  "general_setting.custom_currency_exchange_rate": "1"})
        self.hook = lambda method, path, body, answer: answer
        original = na.Client.req

        def request(client, method, path, body=None, **kwargs):
            answer = original(client, method, path, body, **kwargs)
            # The fake transport normally keeps status independent. These two
            # rc.44 settings also update the effective anonymous display fields.
            if method == "PUT" and path == "/api/option/" and answer.get("success"):
                for key, field, _ in na.DISPLAY_SETTINGS:
                    if body["key"] == key:
                        self.fake.status[field] = body["value"]
            return self.hook(method, path, body, answer)

        patch = mock.patch.object(na.Client, "req", request)
        patch.start()
        self.addCleanup(patch.stop)

    def display(self, *extra, target="prod"):
        return self.run_tool("display-settings", *extra, target=target)

    def test_default_plan_is_read_only_and_prints_only_fixed_values(self):
        self.fake.options["UnknownSensitiveThing"] = PAT
        rc, out, err = self.display()
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["result"], "plan")
        self.assertEqual(report["would_write"], [key for key, _, _ in na.DISPLAY_SETTINGS])
        self.assertEqual(self.writes(), [])
        self.assertNotIn("GroupRatio", out)
        self.assertNotIn("UnknownSensitiveThing", out)

    def test_apply_changes_only_fixed_values_and_preserves_existing_prices(self):
        before = dict(self.fake.options)
        rc, out, err = self.display("--apply")
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["result"], "verified")
        self.assertEqual(report["remaining"], [])
        self.assertTrue(report["other_options_unchanged"])
        self.assertEqual([(r[0], r[1], r[3]) for r in self.writes()], [
            ("PUT", "/api/option/", {"key": "general_setting.custom_currency_exchange_rate", "value": 32}),
            ("PUT", "/api/option/", {"key": "general_setting.quota_display_type", "value": "CUSTOM"}),
        ])
        for key in set(before) - {k for k, _, _ in na.DISPLAY_SETTINGS}:
            self.assertEqual(self.fake.options[key], before[key], key)
        self.assertEqual(len(self.calls("GET", "/api/status")), 2)
        self.fake.requests.clear()
        self.assertEqual(self.display("--apply")[0], 0)
        self.assertEqual(self.writes(), [])

    def test_staging_uses_the_same_bounded_command(self):
        self.fake.status["server_address"] = STG
        self.assertEqual(self.display("--apply", target="stg")[0], 0)
        self.assertEqual(len(self.writes()), 2)

    def test_requires_explicit_target_even_for_plan(self):
        rc, _, err = self.display(target=None)
        self.assertEqual(rc, 2)
        self.assertIn("requires --target", err)
        self.assertEqual(self.fake.requests, [])

    def test_password_sessions_are_never_created_for_this_command(self):
        for target in ("stg", "prod"):
            rc, _, err = self.run_tool("--base", self.base, "--creds", self.creds(),
                                       "display-settings", "--apply", auth=False, target=target)
            self.assertEqual(rc, 2)
            self.assertIn("requires --token-file", err)
        self.assertEqual(self.fake.requests, [])

    def test_unknown_mismatched_or_path_server_address_refused_before_auth(self):
        for address in (STG, "http://localhost:3000", PROD + "/other", "https://other.example"):
            with self.subTest(address=address):
                self.fake.status["server_address"] = address
                self.fake.requests.clear()
                rc, _, _ = self.display("--apply")
                self.assertEqual(rc, 2)
                self.assertFalse(any(r[2] for r in self.fake.requests))
                self.assertEqual(self.writes(), [])

    def test_mismatched_and_unknown_public_bases_are_refused_before_any_request(self):
        for base in (STG, "https://attacker.example", PROD + ":444", "http://127.0.0.1.evil.example"):
            with self.subTest(base=base), mock.patch.object(na.Client, "__init__", autospec=True) as init:
                def create(client, supplied_base, token=None, creds=None):
                    client.base, client.token = base, token
                    client.creds, client.sid, client._status = creds, None, None
                init.side_effect = create
                with mock.patch.object(na.Client, "req", return_value={"success": True, "data": self.fake.status}) as req:
                    self.assertEqual(self.display("--apply")[0], 2)
                    self.assertEqual(req.call_count, 0)

    def test_matching_public_origin_is_allowed(self):
        with mock.patch.object(na.Client, "__init__", autospec=True) as init:
            def create(client, supplied_base, token=None, creds=None):
                client.base, client.token = PROD, token
                client.creds, client.sid, client._status = creds, None, None
            init.side_effect = create
            options = self.fake.option_list()
            with mock.patch.object(na.Client, "req", side_effect=[
                    {"success": True, "data": self.fake.status}, {"success": True, "data": options}]) as req:
                self.assertEqual(self.display()[0], 0)
                self.assertEqual(req.call_count, 2)

    def test_initial_http_errors_with_arbitrary_sensitive_body_are_not_echoed(self):
        import urllib.error
        sensitive = b'private-backend-value-not-a-recognized-token'
        for path in ("/api/status", "/api/option/"):
            with self.subTest(path=path):
                original = na._OPENER.open
                def open_request(req, **kwargs):
                    if req.full_url.endswith(path):
                        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(sensitive))
                    return original(req, **kwargs)
                with mock.patch.object(na._OPENER, "open", side_effect=open_request):
                    rc, out, err = self.display("--apply")
                self.assertEqual(rc, 2)
                self.assertNotIn(sensitive.decode(), out + err)
                self.assertIn("details withheld", err)
                self.assertEqual(self.writes(), [])

    def test_version_and_non_display_status_guards_precede_authenticated_reads(self):
        for field, value in (("version", "v1.0.0-rc.45"), ("register_enabled", True), ("checkin_enabled", True)):
            with self.subTest(field=field):
                original = self.fake.status[field]
                self.fake.status[field] = value
                self.fake.requests.clear()
                self.assertEqual(self.display("--apply")[0], 2)
                self.assertFalse(any(r[2] for r in self.fake.requests))
                self.fake.status[field] = original

    def test_runtime_drift_is_repaired_even_when_stored_option_already_matches(self):
        self.fake.options["general_setting.quota_display_type"] = "CUSTOM"
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.writes()), 2)

    def test_missing_or_ambiguous_options_are_refused_without_writes(self):
        original = self.fake.option_list
        for response in (original() + [original()[0]], [{"key": "bad", "value": 32}], []):
            with self.subTest(response_type=type(response).__name__):
                with mock.patch.object(self.fake, "option_list", return_value=response):
                    self.assertEqual(self.display("--apply")[0], 2)
                    self.assertEqual(self.writes(), [])

    def test_unsuccessful_status_or_option_envelopes_are_refused(self):
        for endpoint in ("/api/status", "/api/option/"):
            def hook(method, path, body, answer):
                return {**answer, "success": False, "message": PAT} if path == endpoint else answer
            self.hook = hook
            rc, _, _ = self.display("--apply")
            self.assertEqual(rc, 2)
            self.assertEqual(self.writes(), [])

    def test_rejection_stops_after_first_attempt_and_redacts_upstream_error(self):
        def hook(method, path, body, answer):
            if method == "PUT":
                return {"success": False, "message": PAT}
            return answer
        self.hook = hook
        rc, out, err = self.display("--apply")
        self.assertEqual((rc, err), (1, ""))
        report = json.loads(out)
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(report["remaining"], ["general_setting.quota_display_type"])

    def test_timeout_after_commit_is_uncertain_and_never_blindly_retried(self):
        def hook(method, path, body, answer):
            if method == "PUT":
                raise na.Fatal("timeout " + PAT)
            return answer
        self.hook = hook
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("uncertain", json.loads(out)["reason"])
        self.assertEqual(len(self.writes()), 1)

    def test_second_failure_reports_partial_progress(self):
        def hook(method, path, body, answer):
            if method == "PUT" and body["key"].endswith("quota_display_type"):
                raise na.Fatal("transport unavailable")
            return answer
        self.hook = hook
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 1)
        report = json.loads(out)
        self.assertEqual(report["applied"], ["general_setting.custom_currency_exchange_rate"])
        self.assertEqual(report["remaining"], [])
        self.assertEqual(len(self.writes()), 2)

    def test_runtime_readback_failure_is_not_success(self):
        def hook(method, path, body, answer):
            if method == "PUT":
                self.fake.status["quota_display_type"] = "USD"
            return answer
        self.hook = hook
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["remaining"], ["general_setting.quota_display_type"])

    def test_other_option_drift_is_reported_without_rollback(self):
        def hook(method, path, body, answer):
            if method == "PUT":
                self.fake.options["GroupRatio"] = '{"default":1.3}'
            return answer
        self.hook = hook
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["other_options_unchanged"])
        self.assertEqual(len(self.writes()), 2)

    def test_post_write_status_target_change_fails_verification(self):
        def hook(method, path, body, answer):
            if method == "PUT":
                self.fake.status["server_address"] = STG
            return answer
        self.hook = hook
        rc, out, _ = self.display("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("readback", json.loads(out)["reason"])

    def test_arbitrary_files_keys_and_values_cannot_be_supplied(self):
        for extra in (("--only", "GroupRatio"), ("--value", "1.3"), (SETTINGS,), ("--force",)):
            with self.subTest(extra=extra):
                self.assertEqual(self.display("--apply", *extra)[0], 2)
                self.assertEqual(self.fake.requests, [])


class TestChannels(Base):
    def setUp(self):
        super().setUp()
        self.fake.channels = []
        self.key_file = self.private("litellm-key", UPSTREAM_KEY + "\n")
        self.free_key_file = self.private("litellm-free-key", FREE_KEY + "\n")

    def apply(self, *extra, target="stg"):
        return self.run_tool("apply-channels", "--key-alias", "newapi-stg-litellm", *extra, target=target)

    def test_fresh_install_matches_na01(self):
        rc, out, _ = self.apply("--key-file", self.key_file)
        self.assertEqual(rc, 0, out)
        paid, free = sorted(self.fake.channels, key=lambda c: c["group"])
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
        self.assertEqual(out.count("\nok "), 2)

    def test_with_internal_key_from_env(self):
        with mock.patch.dict(os.environ, {"NA_TEST_KEY": UPSTREAM_KEY}):
            rc, out, _ = self.apply("--key-env", "NA_TEST_KEY", "--with-internal")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(c["group"] for c in self.fake.channels), ["default", "free", "internal"])
        internal = next(c for c in self.fake.channels if c["group"] == "internal")
        self.assertEqual(na._norm_set(internal["models"]), sorted(PLAN["internal"]))

    def test_key_checks(self):
        os.chmod(self.key_file, 0o644)
        self.assertEqual(self.apply("--key-file", self.key_file)[0], 2)
        self.assertEqual(self.apply()[0], 2)
        bad = self.private("bad-key", "not-a-key")
        rc, _, err = self.apply("--key-file", bad)
        self.assertEqual(rc, 2)
        self.assertNotIn("not-a-key", err)
        self.assertEqual(self.writes(), [])

    def test_dry_run_reports_missing_as_failure(self):
        rc, out, _ = self.apply("--dry-run", target=None)
        self.assertEqual(rc, 1, out)
        self.assertEqual(out.count("MISSING"), 2)
        self.assertIn("no key given", out)
        # with a key, the dry run checks it too
        bad = self.private("bad-key", "not-a-key")
        self.assertEqual(self.apply("--dry-run", "--key-file", bad, target=None)[0], 2)
        rc, out, _ = self.apply("--dry-run", "--key-file", self.key_file, target=None)
        self.assertEqual(rc, 1, out)
        self.assertIn("key checked", out)
        self.assertEqual(self.writes(), [])

    def test_na04_case_bad_key_writes_nothing(self):
        """Paid channel drifted, free channel missing, bad key: no partial update."""
        self.apply("--key-file", self.key_file)
        paid = next(c for c in self.fake.channels if c["group"] == "default")
        self.fake.channels = [paid]
        paid["models"] += ",qwen3-4b-aicoder,qwen3-4b-cluster1"  # the prod layout before NA-04
        self.fake.requests.clear()
        bad = self.private("bad-key", "not-a-key")
        for extra in (["--update", "--key-file", bad], ["--update"]):
            rc, _, _ = self.apply(*extra)
            self.assertEqual(rc, 2, extra)
        self.assertEqual(self.writes(), [])
        self.assertIn("qwen3-4b-aicoder", paid["models"])
        # a good key: the free channel is created first, then the paid one fixed
        rc, out, _ = self.apply("--update", "--key-file", self.key_file)
        self.assertEqual(rc, 0, out)
        self.assertEqual([r[0] for r in self.writes()], ["POST", "PUT"])
        self.assertEqual(na._norm_set(paid["models"]), sorted(PLAN["api"]))

    def test_dedicated_free_key(self):
        rc, out, _ = self.apply("--key-file", self.key_file, "--free-key-file", self.free_key_file,
                                "--free-key-alias", "newapi-stg-litellm-free")
        self.assertEqual(rc, 0, out)
        by = {c["group"]: c for c in self.fake.channels}
        self.assertEqual((by["default"]["key"], by["free"]["key"]), (UPSTREAM_KEY, FREE_KEY))
        self.assertEqual(by["free"]["remark"], "stg; LiteLLM key alias newapi-stg-litellm-free")
        self.assertEqual(self.apply("--free-key-file", self.free_key_file)[0], 2)  # alias required

    def test_rekey_only_the_free_channel(self):
        self.apply("--key-file", self.key_file)
        self.fake.requests.clear()
        rc, out, _ = self.apply("--group", "free", "--update", "--rekey", "--free-key-file", self.free_key_file,
                                "--free-key-alias", "newapi-stg-litellm-free")
        self.assertEqual(rc, 0, out)
        (put,) = self.calls("PUT", "/api/channel/")
        self.assertEqual(put[3]["key"], FREE_KEY)
        by = {c["group"]: c for c in self.fake.channels}
        self.assertEqual((by["default"]["key"], by["free"]["key"]), (UPSTREAM_KEY, FREE_KEY))
        self.assertEqual(self.apply("--rekey", "--key-file", self.key_file)[0], 2)  # needs --update

    def test_drift_reported_then_updated_key_kept(self):
        self.apply("--key-file", self.key_file)
        paid = next(c for c in self.fake.channels if c["group"] == "default")
        paid["models"] += ",qwen3-4b-aicoder,qwen3-4b-cluster1"
        paid["key"] = "sk-" + "P" * 22
        rc, out, _ = self.apply()
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("qwen3-4b-aicoder", out)
        self.assertIn("models +0 [] -2 ['qwen3-4b-aicoder', 'qwen3-4b-cluster1']", out)
        rc, out, _ = self.apply("--dry-run", target=None)
        self.assertEqual(rc, 1)
        rc, out, _ = self.apply("--update")
        self.assertEqual(rc, 0, out)
        (put,) = self.calls("PUT", "/api/channel/")
        self.assertNotIn("key", put[3])
        self.assertEqual(paid["key"], "sk-" + "P" * 22)
        self.assertEqual(na._norm_set(paid["models"]), sorted(PLAN["api"]))
        self.assertEqual(self.apply()[0], 0)

    def test_model_drift_gives_the_count_when_cut(self):
        want = {f: None for f in na.CHANNEL_FIELDS}
        want["models"] = ",".join(f"m{i:02d}" for i in range(13))
        have = dict(want, models="m00")
        (line,) = na.channel_diff(want, have)
        self.assertEqual(line, "models +12 ['m01', 'm02', 'm03', 'm04', 'm05']... -0 []")

    def test_duplicate_names_refused(self):
        self.apply("--key-file", self.key_file)
        self.fake.channels.append(dict(self.fake.channels[1], id=3))
        rc, out, _ = self.apply("--update")
        self.assertEqual(rc, 1)
        self.assertIn("2 channels with this name", out)
        self.assertEqual(self.calls("PUT", "/api/channel/"), [])


if __name__ == "__main__":
    unittest.main()
