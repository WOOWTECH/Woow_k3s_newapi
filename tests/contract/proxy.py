"""Signed client for the newapi-proxy (X-tests).

Signs requests exactly as Odoo does (``signing.py``) and keeps the proxy's
answer verbatim, including ``X-Woow-Proxy-Error`` / ``X-Woow-Proxy-Precheck``,
so the X-tests can assert the error class and the "did it reach new-api"
contract (extra/newapi-proxy/README.md 〈回應與錯誤〉).

Knobs the negative tests need: send unsigned, skew the timestamp, reuse a
nonce (replay), sign with a wrong secret or an unknown kid, inject extra
headers (forged XFF), and send a raw query string. The transport does not
follow redirects and ignores environment proxies; answers go through
:func:`redact.wrap_secrets`; the signing secret is never in a message.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import signing
from .newapi import Headers, _NoRedirect, check_origin, open_url
from .redact import wrap_secrets

UA = "woow-newapi-contract-proxy/2"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

#: Headers a fixture may keep (DESIGN §3.4 classifies on them).
FIXTURE_HEADERS = ("X-Woow-Proxy-Error", "X-Woow-Proxy-Precheck", "Retry-After", "Content-Type")


class TransportError(Exception):
    def __init__(self, message, exc_type="OSError"):
        super().__init__(message)
        self.exc_type = exc_type


class ProxyResponse:
    def __init__(self, status, body_bytes, headers):
        self.status = status
        self.headers = headers = Headers(headers or {})
        self.proxy_error = headers.get("X-Woow-Proxy-Error")
        self.precheck = headers.get("X-Woow-Proxy-Precheck")
        self.request_id = headers.get("X-Oneapi-Request-Id")
        self.retry_after = headers.get("Retry-After")
        self.raw_empty = not body_bytes
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except ValueError:
            body = {}
        self.body = wrap_secrets(body) if isinstance(body, dict) else {}
        self.success = self.body.get("success")
        self.code = self.body.get("code")
        self.message_key = self.body.get("message_key")
        self.reason = self.body.get("reason")
        self.data = self.body.get("data")

    def __repr__(self):
        return (f"<ProxyResponse {self.status} proxy_error={self.proxy_error} reason={self.reason} "
                f"precheck={self.precheck} code={self.code} key={self.message_key!r}>")


class ProxyClient:
    def __init__(self, base, key_list, default_identity="anon"):
        self.base = check_origin(base, "proxy base")
        self.kid, self.secret = signing.first_key(key_list)
        self.default_identity = default_identity

    def send(
        self,
        method,
        path,
        *,
        query_pairs=None,
        body=b"",
        identity=None,
        client_ip="",
        authorization=None,
        security_proof=None,
        read_timeout=None,
        signed=True,
        ts_offset=0,
        nonce=None,
        kid=None,
        secret=None,
        extra_headers=None,
        raw_query=None,
        timeout=35,
    ):
        """One request to the proxy. ``query_pairs`` is canonicalised; pass
        ``raw_query`` to send a hand-built query string unchanged. ``nonce``
        lets two calls reuse one nonce (replay); ``kid``/``secret`` override
        the signing pair (unknown kid, wrong signature)."""
        identity = identity if identity is not None else self.default_identity
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        pairs = list(query_pairs or [])
        canon_query = signing.canonical_query(pairs)
        url_query = raw_query if raw_query is not None else canon_query
        url = self.base + path + (("?" + url_query) if url_query else "")
        req = urllib.request.Request(url, data=(body or None), method=method)
        req.add_header("User-Agent", UA)
        if body:
            req.add_header("Content-Type", "application/json")
        if signed:
            use_kid = kid or self.kid
            use_secret = secret if secret is not None else self.secret
            ts = str(int(time.time()) + ts_offset)
            nonce = nonce or signing.new_nonce()
            message = signing.string_to_sign(
                method, path, canon_query, body or b"", ts, nonce, use_kid, identity, client_ip
            )
            req.add_header(signing.KID_HEADER, use_kid)
            req.add_header(signing.TS_HEADER, ts)
            req.add_header(signing.NONCE_HEADER, nonce)
            req.add_header(signing.SIG_HEADER, signing.sign(use_secret, message))
            req.add_header(signing.IDENTITY_HEADER, identity)
            if client_ip:
                req.add_header(signing.CLIENT_IP_HEADER, client_ip)
        if authorization is not None:
            req.add_header("Authorization", authorization)
        if security_proof is not None:
            req.add_header("X-Security-Proof", security_proof)
        if read_timeout is not None:
            req.add_header(signing.READ_TIMEOUT_HEADER, str(read_timeout))
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        try:
            with open_url(_OPENER, req, timeout, retry_seconds=20) as resp:  # the signature's ts is good for 30 s
                return ProxyResponse(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            return ProxyResponse(e.code, e.read(), dict(e.headers.items()) if e.headers else {})
        except (urllib.error.URLError, OSError) as e:
            inner = getattr(e, "reason", e)
            raise TransportError(f"{method} {path}: {type(inner).__name__}", type(inner).__name__) from None
        except Exception as e:  # noqa: BLE001 — http.client.RemoteDisconnected etc.
            raise TransportError(f"{method} {path}: {type(e).__name__}", type(e).__name__) from None
