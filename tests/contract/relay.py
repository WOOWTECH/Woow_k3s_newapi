"""Minimal OpenAI-style relay client for the F-tests (``/v1/chat/completions``).

A relay request authenticates with ``Authorization: Bearer sk-<key>`` (new-api
trims the ``sk-`` prefix, middleware/auth.go) and is *not* rate-limited by the
/api limiters — only by ModelRequestRateLimit on the user (〔FREE〕§1).

Same transport rules as ``newapi.py``: no redirects (urllib would copy the
``Authorization`` header — the relay key — onto the redirect target), no
environment proxies, plain http only to loopback. A transport failure raises
:class:`RelayTransportError`, whose message never contains the key.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .newapi import Headers, _NoRedirect, check_origin, open_url

UA = "woow-newapi-contract-relay/2"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class RelayTransportError(Exception):
    pass


class RelayResult:
    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers = Headers(headers or {})
        self.retry_after = headers.get("Retry-After")
        try:
            self.body = json.loads(body) if body else {}
        except ValueError:
            self.body = {}
        if not isinstance(self.body, dict):
            self.body = {}
        self.raw_empty = not body
        self.ok = status == 200
        usage = self.body.get("usage") if isinstance(self.body.get("usage"), dict) else {}
        self.completion_tokens = usage.get("completion_tokens")
        choices = self.body.get("choices") if isinstance(self.body.get("choices"), list) else []
        self.finish_reason = (choices[0] or {}).get("finish_reason") if choices else None

    def error_summary(self):
        """Status plus new-api's error type/message (never echoes a key)."""
        err = self.body.get("error") if isinstance(self.body.get("error"), dict) else {}
        msg = str(err.get("message") or self.body.get("message") or "")[:160]
        return f"HTTP {self.status} {err.get('type') or ''} {msg}".strip()

    def __repr__(self):
        return f"<RelayResult {self.status} empty={self.raw_empty}>"


#: The default prompt: answered in ~2 tokens and ~0.6 s by the free Qwen. The
#: free channel's param_override {"max_tokens": 2048} *replaces* the client's
#: max_tokens (U18, F01), so a plain "ping" made the thinking model write ~350
#: tokens in ~22 s and the F-tests' timing (10 burst, 1 per 6 s) meaningless.
QUICK = [{"role": "user", "content": "/no_think Reply with the single word: ok"}]


def chat(base, key, model, *, messages=None, max_tokens=16, timeout=60, stream=False):
    """One chat completion. ``key`` is the 48-char key (no sk-); we add sk-."""
    origin = check_origin(base, "relay base")
    payload = {
        "model": model,
        "messages": messages or QUICK,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    req = urllib.request.Request(origin + "/v1/chat/completions", data=json.dumps(payload).encode(), method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer sk-" + key)
    try:
        with open_url(_OPENER, req, timeout) as resp:
            return RelayResult(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as e:
        return RelayResult(e.code, e.read(), dict(e.headers.items()) if e.headers else {})
    except (urllib.error.URLError, OSError) as e:
        raise RelayTransportError(f"relay /v1/chat/completions: {type(getattr(e, 'reason', e)).__name__}") from None
    except Exception as e:  # noqa: BLE001 — e.g. TransportError from _NoRedirect: no key in it
        raise RelayTransportError(f"relay /v1/chat/completions: {type(e).__name__}") from None
