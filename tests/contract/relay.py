"""Minimal OpenAI-style relay client for the F-tests (``/v1/chat/completions``).

A relay request authenticates with ``Authorization: Bearer sk-<key>`` (new-api
trims the ``sk-`` prefix, middleware/auth.go) and is *not* rate-limited by the
/api limiters — only by ModelRequestRateLimit on the user (〔FREE〕§1).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = "woow-newapi-contract-relay/1"


class RelayResult:
    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self.retry_after = headers.get("Retry-After")
        try:
            self.body = json.loads(body) if body else {}
        except ValueError:
            self.body = {}
        self.raw_empty = not body
        self.ok = status == 200

    def __repr__(self):
        return f"<RelayResult {status if (status:=self.status) else '?'} empty={self.raw_empty}>"


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def chat(base, key, model, *, messages=None, max_tokens=16, timeout=60, stream=False):
    """One chat completion. ``key`` is the 48-char key (no sk-); we add sk-."""
    payload = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "ping"}],
        "max_tokens": max_tokens,
        "stream": stream,
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer sk-" + key)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return RelayResult(resp.status, resp.read(), {k: v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as e:
        hdrs = {k: v for k, v in e.headers.items()} if e.headers else {}
        return RelayResult(e.code, e.read(), hdrs)
