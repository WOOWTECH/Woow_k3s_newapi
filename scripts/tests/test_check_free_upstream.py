import io
import json
import os
import tempfile
import sys
import signal
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_free_upstream as check


VALID_BODY = b'{"data":[{"id":"qwen3-4b-aicoder"},{"id":"qwen3-4b-cluster1"}]}'


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.body[:limit]


class FakeOpener:
    def __init__(self, body=VALID_BODY, error=None):
        self.body, self.error = body, error

    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        if self.error:
            raise self.error
        return FakeResponse(self.body)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_path = os.path.join(self.tmp.name, "key")
        with open(self.key_path, "w") as f:
            f.write("sk-test-secret-value\n")
        os.chmod(self.key_path, 0o600)
        self.base = "http://127.0.0.1:14000"
        self.opener = FakeOpener()
        self.patcher = mock.patch.object(check.urllib.request, "build_opener", return_value=self.opener)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_valid_inventory(self):
        result = check.fetch_models(self.base, "secret")
        self.assertEqual(result, {m: True for m in check.MODELS})

    def test_missing_model_fails(self):
        self.opener.body = b'{"data":[{"id":"qwen3-4b-aicoder"}]}'
        result = check.fetch_models(self.base, "secret")
        self.assertFalse(result["qwen3-4b-cluster1"])
        self.opener.body = VALID_BODY

    def test_key_file_requires_owner_0600_regular_nofollow(self):
        os.chmod(self.key_path, 0o644)
        with self.assertRaisesRegex(check.CheckError, "invalid_key_file"):
            check.read_key(self.key_path)
        os.chmod(self.key_path, 0o600)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.key_path, link)
        with self.assertRaisesRegex(check.CheckError, "invalid_key_file"):
            check.read_key(link)
        with self.assertRaisesRegex(check.CheckError, "invalid_key_file"):
            check.read_key(self.tmp.name)

    def test_rejects_unapproved_urls(self):
        good = "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000"
        self.assertEqual(check.validate_base(good), good)
        for value in ("https://127.0.0.1:4000", "http://evil.example:4000",
                      "http://user:pass@127.0.0.1:4000", self.base + "/path",
                      self.base + "?x=1", self.base + "#frag", "http://localhost:4000"):
            with self.subTest(value=value), self.assertRaisesRegex(check.CheckError, "invalid_url"):
                check.validate_base(value)

    def test_fifo_and_header_characters_fail_without_blocking(self):
        fifo = os.path.join(self.tmp.name, "fifo")
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(check.CheckError, "invalid_key_file"):
            check.read_key(fifo)
        with open(self.key_path, "w") as f:
            f.write("sk-invalid\r\nInjected: value")
        with self.assertRaisesRegex(check.CheckError, "invalid_key_file"):
            check.read_key(self.key_path)

    def test_http_failure_and_redirect(self):
        self.opener.error = urllib.error.HTTPError(self.base, 503, "secret payload", {}, None)
        with self.assertRaisesRegex(check.CheckError, "http_failure"):
            check.fetch_models(self.base, "secret")
        self.opener.error = urllib.error.HTTPError(self.base, 302, "secret redirect", {}, None)
        with self.assertRaisesRegex(check.CheckError, "redirect"):
            check.fetch_models(self.base, "secret")
        self.opener.error = None

    def test_response_bound(self):
        self.opener.body = b" " * (check.MAX_RESPONSE + 1)
        with self.assertRaisesRegex(check.CheckError, "response_too_large"):
            check.fetch_models(self.base, "secret")
        self.opener.body = VALID_BODY

    def test_output_never_echoes_secret_or_error_payload(self):
        self.opener.error = urllib.error.HTTPError(
            self.base, 500, "sk-test-secret-value private-upstream-error-payload", {}, None)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = check.main(["--key-file", self.key_path, "--base", self.base])
        self.assertEqual(rc, 1)
        self.assertNotIn("sk-test-secret-value", out.getvalue())
        self.assertNotIn("private-upstream-error-payload", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["error"], "http_failure")
        self.opener.error = None

    def test_real_slow_body_has_total_deadline(self):
        # Run the actual urllib path, unlike the response fixture tests above.
        self.patcher.stop()
        stop = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    while not stop.wait(.02):
                        self.wfile.write(b" "); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        prior = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        try:
            with mock.patch.object(check, "TIMEOUT", .25):
                with self.assertRaisesRegex(check.CheckError, "deadline_exceeded"):
                    check.bounded_fetch_models("http://127.0.0.1:" + str(server.server_port), "sk-test-secret-value")
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            self.assertEqual(signal.getsignal(signal.SIGALRM), prior)
        finally:
            stop.set(); server.shutdown(); server.server_close(); worker.join(timeout=1)
            self.patcher.start()

    def test_existing_alarm_is_preserved(self):
        with mock.patch.object(check.signal, "getitimer", return_value=(5.0, 0.0)):
            with mock.patch.object(check.signal, "signal") as change:
                with self.assertRaisesRegex(check.CheckError, "deadline_conflict"):
                    check.bounded_fetch_models(self.base, "sk-test-secret-value")
                change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
