"""Standard-library HTTP client for new-api (direct, through a port-forward).

Mirrors ``scripts/newapi_admin.py`` transport rules: no redirects (urllib
copies ``Authorization`` onto the target), no environment proxies, plain http
only to loopback. Unlike the admin tool this one keeps the HTTP status code and
the headers (``X-Oneapi-Request-Id``, ``Retry-After``) so the C-tests can
assert the full response envelope (〔PROV〕§1.1).

Every parsed body goes through :func:`redact.wrap_secrets`, so a token, key,
JWT or proof in an answer is a :class:`redact.Secret` from the start.

Every call to a critical route (〔PROV〕§1.2: login, verify, PAT
create/patch/delete, reveal) first takes one slot from the shared
:class:`budget.CtBudget`, which waits for the 127.0.0.1 window when it is spent.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import budget as budgetmod
from .redact import wrap_secrets

UA = "woow-newapi-contract/2"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class TransportError(Exception):
    """Could not reach new-api / got a non-JSON answer (not an API error)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        to = urllib.parse.urlsplit(newurl)
        raise TransportError(f"unexpected HTTP {code} redirect to {to.scheme}://{to.hostname or '?'}")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def check_origin(base, what="base"):
    """``scheme://host[:port]`` for an http(s) origin; plain http only to loopback."""
    u = urllib.parse.urlsplit(base or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise TransportError(f"{what} must be an http(s) origin")
    if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        raise TransportError(f"{what} must be an origin only (no path, query or user info)")
    if u.scheme == "http" and u.hostname not in LOOPBACK_HOSTS:
        raise TransportError(f"{what}: plain http only to loopback (a credential would travel in clear text)")
    return f"{u.scheme}://{u.netloc}"


class Response:
    """One new-api answer: HTTP status, parsed JSON body and the headers that
    matter. ``code``/``message_key`` are the stable classification fields."""

    def __init__(self, status, body, headers, raw_empty):
        self.status = status
        self.body = wrap_secrets(body) if isinstance(body, dict) else {}
        self.headers = headers
        self.success = self.body.get("success")
        self.message = self.body.get("message")
        self.message_key = self.body.get("message_key")
        self.code = self.body.get("code")
        self.data = self.body.get("data")
        self.request_id = headers.get("X-Oneapi-Request-Id")
        self.retry_after = headers.get("Retry-After")
        self.raw_empty = raw_empty

    def __repr__(self):
        return (f"<Response {self.status} success={self.success} code={self.code} "
                f"key={self.message_key!r} empty={self.raw_empty}>")


class Client:
    """``budget``: the run's CtBudget (None = no accounting, e.g. offline);
    ``budget_ip``: the address new-api sees (127.0.0.1 over a port-forward);
    ``uc_user_id``: this identity's user id, so its /api/verify calls also pace
    the user's UC:security-verification bucket."""

    def __init__(self, base, token=None, budget=None, budget_ip="127.0.0.1", uc_user_id=None):
        self.base = check_origin(base)
        self.token = token
        self.budget = budget
        self.budget_ip = budget_ip
        self.uc_user_id = uc_user_id

    def with_token(self, token, uc_user_id=None):
        return Client(self.base, token=token, budget=self.budget, budget_ip=self.budget_ip,
                      uc_user_id=uc_user_id)

    def _account(self, method, path):
        if self.budget is None or not budgetmod.is_ct_route(method, path):
            return
        self.budget.take(budgetmod.ip_key(self.budget_ip), label=f"{method} {path.split('?')[0]}")
        if self.uc_user_id and path.split("?")[0] == "/api/verify":
            self.budget.take(budgetmod.user_key(self.uc_user_id), label="admin step-up")

    def request(self, method, path, body=None, token=..., headers=None, timeout=60, ct_exempt=False):
        """Return a :class:`Response`. 4xx/5xx are answers, not exceptions;
        only a dead socket / non-JSON raises :class:`TransportError`."""
        if not ct_exempt:
            self._account(method, path)
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
                return self._parse(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            raw = e.read()
            return self._parse(e.code, raw, dict(e.headers.items()) if e.headers else {})
        except TransportError:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(f"{method} {path.split('?')[0]}: {type(getattr(e, 'reason', e)).__name__}") from None

    @staticmethod
    def _parse(status, raw, headers):
        if not raw:
            return Response(status, None, headers, raw_empty=True)
        try:
            return Response(status, json.loads(raw), headers, raw_empty=False)
        except ValueError:
            raise TransportError(f"HTTP {status}: the answer is not JSON (is base a new-api origin?)") from None

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
