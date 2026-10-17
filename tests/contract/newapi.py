"""Standard-library HTTP client for new-api (direct, through a port-forward).

Mirrors ``scripts/newapi_admin.py`` transport rules: no redirects (urllib
copies ``Authorization`` onto the target), no environment proxies, plain http
only to loopback. Unlike the admin tool this one keeps the HTTP status code and
the raw headers (``X-Oneapi-Request-Id``, ``Retry-After``) so the C-tests can
assert the full response envelope (〔PROV〕§1.1).

Auth is a Bearer header: an admin PAT, or a user's dashboard JWT / UP. Nothing
here hits the proxy — that is ``proxy.py``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

UA = "woow-newapi-contract/1"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class TransportError(Exception):
    """Could not reach new-api / got a non-JSON answer (not an API error)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError(f"unexpected HTTP {code} redirect to {newurl!r}")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class Response:
    """One new-api answer: HTTP status, parsed JSON body and the headers that
    matter. ``code``/``message_key`` are the stable classification fields."""

    def __init__(self, status, body, headers):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.headers = headers
        self.success = self.body.get("success")
        self.message = self.body.get("message")
        self.message_key = self.body.get("message_key")
        self.code = self.body.get("code")
        self.data = self.body.get("data")
        self.request_id = headers.get("X-Oneapi-Request-Id")
        self.retry_after = headers.get("Retry-After")
        self.raw_empty = body is None

    def __repr__(self):
        return f"<Response {self.status} success={self.success} code={self.code} key={self.message_key!r}>"


class Client:
    def __init__(self, base, token=None):
        u = urllib.parse.urlsplit(base or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TransportError("base must be an http(s) origin")
        if u.scheme == "http" and u.hostname not in LOOPBACK_HOSTS:
            raise TransportError("plain http only to loopback (token in clear text)")
        self.base = f"{u.scheme}://{u.netloc}"
        self.token = token

    def with_token(self, token):
        return Client(self.base, token=token)

    def request(self, method, path, body=None, token=..., headers=None, timeout=60):
        """Return a :class:`Response`. 4xx/5xx are answers, not exceptions;
        only a dead socket / non-JSON raises :class:`TransportError`."""
        use_token = self.token if token is ... else token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Content-Type", "application/json")
        if use_token:
            req.add_header("Authorization", "Bearer " + use_token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
                hdrs = {k: v for k, v in resp.headers.items()}
                return self._parse(resp.status, raw, hdrs)
        except urllib.error.HTTPError as e:
            raw = e.read()
            hdrs = {k: v for k, v in e.headers.items()} if e.headers else {}
            return self._parse(e.code, raw, hdrs)
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(f"{method} {path}: {getattr(e, 'reason', e)}") from None

    @staticmethod
    def _parse(status, raw, headers):
        if not raw:
            return Response(status, None, headers)
        try:
            return Response(status, json.loads(raw), headers)
        except ValueError:
            raise TransportError("new-api answer is not JSON (is base a new-api origin?)") from None

    # -- shorthands ------------------------------------------------------ #
    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def qs(**params):
    """Query string (only non-None values), percent-encoded."""
    items = [(k, str(v)) for k, v in params.items() if v is not None]
    return ("?" + urllib.parse.urlencode(items)) if items else ""
