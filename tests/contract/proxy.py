"""Signed client for the newapi-proxy (X-tests).

Signs requests exactly as Odoo does (``signing.py``) and keeps the proxy's
answer verbatim, including ``X-Woow-Proxy-Error`` / ``X-Woow-Proxy-Precheck``,
so the X-tests can assert the error class and the "did it reach new-api"
contract (extra/newapi-proxy/README.md 〈回應與錯誤〉).

Knobs the negative tests need: send unsigned, skew the timestamp, reuse a
nonce (replay), reuse but re-sign, inject extra headers (forged XFF), and send
a raw query string that the proxy must reject. The transport does not follow
redirects and ignores environment proxies.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import signing

UA = "woow-newapi-contract-proxy/1"


class TransportError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError(f"unexpected HTTP {code} redirect to {newurl!r}")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class ProxyResponse:
    def __init__(self, status, body_bytes, headers):
        self.status = status
        self.headers = headers
        self.proxy_error = headers.get("X-Woow-Proxy-Error")
        self.precheck = headers.get("X-Woow-Proxy-Precheck")
        self.request_id = headers.get("X-Oneapi-Request-Id")
        self.retry_after = headers.get("Retry-After")
        self.raw = body_bytes
        try:
            self.body = json.loads(body_bytes) if body_bytes else {}
        except ValueError:
            self.body = {}
        if not isinstance(self.body, dict):
            self.body = {}
        self.success = self.body.get("success")
        self.code = self.body.get("code")
        self.message_key = self.body.get("message_key")
        self.reason = self.body.get("reason")
        self.data = self.body.get("data")

    def __repr__(self):
        return f"<ProxyResponse {self.status} proxy_error={self.proxy_error} code={self.code}>"


class ProxyClient:
    def __init__(self, base, key_list, default_identity="anon"):
        u = urllib.parse.urlsplit(base or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TransportError("proxy base must be an http(s) origin")
        self.base = f"{u.scheme}://{u.netloc}"
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
        extra_headers=None,
        raw_query=None,
        timeout=35,
    ):
        """One request to the proxy. ``query_pairs`` is canonicalised; pass
        ``raw_query`` to send a hand-built query string unchanged (encoding
        tests). ``nonce`` lets two calls reuse one nonce (replay)."""
        identity = identity if identity is not None else self.default_identity
        if isinstance(body, str):
            body = body.encode()
        pairs = list(query_pairs or [])
        canon_query = signing.canonical_query(pairs)
        url_query = raw_query if raw_query is not None else canon_query
        url = self.base + path + (("?" + url_query) if url_query else "")
        req = urllib.request.Request(url, data=(body or None), method=method)
        req.add_header("User-Agent", UA)
        if signed:
            ts = str(int(time.time()) + ts_offset)
            nonce = nonce or signing.new_nonce()
            message = signing.string_to_sign(
                method, path, canon_query, body or b"", ts, nonce, self.kid, identity, client_ip
            )
            req.add_header(signing.KID_HEADER, self.kid)
            req.add_header(signing.TS_HEADER, ts)
            req.add_header(signing.NONCE_HEADER, nonce)
            req.add_header(signing.SIG_HEADER, signing.sign(self.secret, message))
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
            with _OPENER.open(req, timeout=timeout) as resp:
                return ProxyResponse(resp.status, resp.read(), {k: v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:
            hdrs = {k: v for k, v in e.headers.items()} if e.headers else {}
            return ProxyResponse(e.code, e.read(), hdrs)
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(f"{method} {path}: {getattr(e, 'reason', e)}") from None
