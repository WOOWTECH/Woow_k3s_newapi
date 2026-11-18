"""Tiny OpenAI-compatible upstream for the sandbox: fixed usage, no content logged."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE = {"prompt_tokens": 1000, "completion_tokens": 2001, "total_tokens": 3001,
         "prompt_tokens_details": {"cached_tokens": 400}}


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

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send({"object": "list", "data": [{"id": "mock", "object": "model"}]})
        self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._send({"id": "chatcmpl-mock", "object": "chat.completion", "created": 1700000000,
                    "model": body.get("model", "mock"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": USAGE})


ThreadingHTTPServer(("0.0.0.0", 4000), H).serve_forever()
