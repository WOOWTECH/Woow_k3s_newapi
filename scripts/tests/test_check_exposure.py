"""Offline tests for check-exposure.sh using a local fake HTTP server."""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "check-exposure.sh")
SECRET = "unit-test-secret-must-not-leak"
DEFAULT_CONFIG_MARKER = "fake-default-config-must-not-leak"
ANON_BODY = b"private fake response body"
ANON_POST_BODY = b"anonymous post response secret must not be printed"
DENY_PATHS = (
    "/v1/dashboard/billing/subscription", "/v1/tasks/x", "/v1/mj/submit/imagine",
    "/v1/video/generations/x", "/v1/../api/status", "/v1/%2e%2e/api/status", "//api/status",
)


class FakeServer:
    def __init__(self):
        self.requests = []
        self.pg_post_auth = "not-seen"
        self.anonymous_status = 302
        self.models_status = 200
        self.stream_status = 200

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, code, body=b"private fake response body"):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, self.headers.get("Authorization"),
                                       self.raw_requestline.decode("ascii").split(" ", 2)[1]))
                if self.path in ("/", "/console", "/api/status", "/api/pricing",
                                 "/api/ratio_config", "/api/perf-metrics/summary"):
                    return self.respond(owner.anonymous_status, ANON_BODY)
                if self.path == "/v1/models":
                    return self.respond(owner.models_status if self.headers.get("Authorization") == f"Bearer {SECRET}" else 401,
                                        b"relay response secret must not be printed")
                if self.path in ("/v1", "/v1/", "/v1x", "/V1/models"):
                    return self.respond(200)
                return self.respond(404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(size)
                owner.requests.append(("POST", self.path, self.headers.get("Authorization"),
                                       self.raw_requestline.decode("ascii").split(" ", 2)[1]))
                if self.path == "/pg/chat/completions":
                    owner.pg_post_auth = self.headers.get("Authorization")
                    return self.respond(owner.anonymous_status, ANON_POST_BODY)
                if self.path == "/v1/chat/completions":
                    return self.respond(owner.stream_status,
                                        b'data: {"choices":[]}\r\n\r\ndata: [DONE]\r\n\r\n')
                return self.respond(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.port = self.server.server_address[1]
        self.ipv6_server = None
        try:
            class IPv6Server(ThreadingHTTPServer):
                address_family = __import__("socket").AF_INET6

            self.ipv6_server = IPv6Server(("::1", 0), Handler)
            self.ipv6_thread = threading.Thread(target=self.ipv6_server.serve_forever, daemon=True)
            self.ipv6_thread.start()
            self.ipv6_port = self.ipv6_server.server_address[1]
        except OSError:
            self.ipv6_port = None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.ipv6_server:
            self.ipv6_server.shutdown()
            self.ipv6_server.server_close()
            self.ipv6_thread.join()


class CheckExposureTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.tmp = tempfile.mkdtemp(prefix="check-exposure-test-")
        self.key_file = os.path.join(self.tmp, "key")
        with open(self.key_file, "w", encoding="utf-8") as handle:
            handle.write(SECRET + "\n")
        os.chmod(self.key_file, 0o600)
        self.curl_home = os.path.join(self.tmp, "curl-home")
        os.mkdir(self.curl_home)
        with open(os.path.join(self.curl_home, ".curlrc"), "w", encoding="utf-8") as handle:
            handle.write(f'header = "X-Injected: {DEFAULT_CONFIG_MARKER}"\n')

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmp)

    def run_script(self, *args, url=None):
        env = os.environ.copy()
        env["CURL_HOME"] = self.curl_home
        return subprocess.run([SCRIPT, *args, url or self.server.url], env=env,
                              text=True, capture_output=True, timeout=20)

    def assert_sensitive_absent(self, result):
        for value in (SECRET, DEFAULT_CONFIG_MARKER, ANON_BODY.decode(), ANON_POST_BODY.decode(),
                      "relay response secret must not be printed"):
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)

    def test_anonymous_only_passes_and_skips_relay(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("略過已驗證 relay 測項", result.stdout)
        self.assertEqual(len(self.server.requests), 7)
        self.assertTrue(all(auth is None for _, _, auth, _ in self.server.requests))
        anonymous_post = [request for request in self.server.requests
                          if request[0] == "POST" and request[3] == "/pg/chat/completions"]
        self.assertEqual(anonymous_post, [("POST", "/pg/chat/completions", None, "/pg/chat/completions")])
        self.assert_sensitive_absent(result)

    def test_anonymous_origin_401_fails_without_printing_body_or_secret(self):
        self.server.anonymous_status = 401
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected Cloudflare Access block (302/403), got 401", result.stderr)
        self.assert_sensitive_absent(result)
        self.assertTrue(all(auth is None for _, _, auth, _ in self.server.requests))

    def test_key_mode_checks_allow_deny_and_route_evidence(self):
        result = self.run_script("--key-file", self.key_file, "--exercise-qwen", "Qwen-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS authenticated /v1/models: 200", result.stdout)
        for route in ("/v1", "/v1/", "/v1x", "/V1/models"):
            self.assertIn(f"EVIDENCE {route}: 200", result.stdout)
        self.assertTrue(any(path == "/v1/models" and auth == f"Bearer {SECRET}"
                            for _, path, auth, _ in self.server.requests))
        denied_seen = [target for _, _, _, target in self.server.requests if target in DENY_PATHS]
        self.assertCountEqual(denied_seen, DENY_PATHS)
        self.assertEqual(len(self.server.requests[:7]), 7)
        self.assertEqual([auth for _, _, auth, _ in self.server.requests[:7]], [None] * 7)
        self.assertIsNone(self.server.pg_post_auth)
        relay_requests = self.server.requests[7:]
        self.assertTrue(relay_requests)
        self.assertTrue(all(auth == f"Bearer {SECRET}" for _, _, auth, _ in relay_requests))
        self.assertTrue(all(target.startswith("/") for _, _, _, target in self.server.requests))
        self.assertTrue(all("X-Injected" not in str(request) for request in self.server.requests))
        self.assert_sensitive_absent(result)

    def test_key_only_fails_before_requests(self):
        result = self.run_script("--key-file", self.key_file)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--key-file requires --exercise-qwen MODEL", result.stderr)
        self.assertEqual(self.server.requests, [])
        self.assert_sensitive_absent(result)

    def test_models_non_200_fails_without_printing_body_or_key(self):
        self.server.models_status = 201
        result = self.run_script("--key-file", self.key_file, "--exercise-qwen", "Qwen-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/v1/models expected 200, got 201", result.stderr)
        self.assert_sensitive_absent(result)

    def test_stream_non_200_fails_without_printing_body_or_key(self):
        self.server.stream_status = 202
        result = self.run_script("--key-file", self.key_file, "--exercise-qwen", "Qwen-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Qwen stream expected 200, got 202", result.stderr)
        self.assert_sensitive_absent(result)

    def test_mode_unsafe_is_rejected_before_request(self):
        os.chmod(self.key_file, 0o640)
        result = self.run_script("--key-file", self.key_file, "--exercise-qwen", "Qwen-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mode 0600 or stricter", result.stderr)
        self.assertEqual(self.server.requests, [])
        self.assert_sensitive_absent(result)

    def test_unsafe_key_characters_rejected(self):
        with open(self.key_file, "w", encoding="utf-8") as handle:
            handle.write('bad"key\n')
        result = self.run_script("--key-file", self.key_file)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.server.requests, [])
        self.assert_sensitive_absent(result)

    def test_stream_is_explicit_opt_in_and_requires_key(self):
        no_key = self.run_script("--exercise-qwen", "Qwen-test")
        self.assertNotEqual(no_key.returncode, 0)
        self.assertEqual(len(self.server.requests), 0)
        result = self.run_script("--key-file", self.key_file, "--exercise-qwen", "Qwen-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SSE terminator", result.stdout)
        self.assertTrue(any(method == "POST" and path == "/v1/chat/completions"
                            for method, path, _, _ in self.server.requests))
        self.assert_sensitive_absent(result)

    def test_exercise_model_must_be_safe_and_qwen_named(self):
        for model in ("paid-model", "Qwen model", 'Qwen"x'):
            result = self.run_script("--key-file", self.key_file, "--exercise-qwen", model)
            self.assertNotEqual(result.returncode, 0, model)
            self.assertEqual(self.server.requests, [])

    def test_authority_validation_and_ipv6_loopback_port(self):
        for target in ("https://user@localhost", "https://localhost/path", "https://localhost?q=1",
                       "https://localhost#fragment", "https://localhost:bad", "https://local host"):
            result = self.run_script(url=target)
            self.assertNotEqual(result.returncode, 0, target)
        if self.server.ipv6_port is None:
            self.skipTest("IPv6 loopback is unavailable in this environment")
        result = self.run_script(url=f"http://[::1]:{self.server.ipv6_port}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(auth is None for _, _, auth, _ in self.server.requests))

    def test_http_mode_is_localhost_only_and_base_has_no_path(self):
        result = self.run_script(url="http://example.invalid")
        self.assertNotEqual(result.returncode, 0)
        bad_base = self.run_script(url=self.server.url + "/path")
        self.assertNotEqual(bad_base.returncode, 0)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
