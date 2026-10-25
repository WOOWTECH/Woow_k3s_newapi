#!/usr/bin/env python3
"""Admin client for new-api: settings, pricing and channels as code.

Nothing has a default target: every command that talks to a server needs
--base and one credential, and every write also needs --target.
General production mutations are disabled until the approved price-notice gate
and all-writer interlock exist. Bounded exceptions are display-settings --apply
and free-pilot --apply; neither permits arbitrary settings or paid price edits.

  --base URL         the new-api origin itself, e.g. a port-forward
                     http://127.0.0.1:3301. Plain http only to 127.0.0.1, ::1
                     or localhost; no path. A redirect is an error (a token is
                     never sent on to another host, e.g. a Cloudflare Access login).
  --token-file FILE  a personal access token (nap_...) of root, alone in a
                     0600 file. No password login, so it works when root has
                     2FA (production). Scopes per command:
                       get-options, verify-settings   option:read
                       apply-settings, apply-pricing  option:read + option:write
                       display-settings               option:read (PAT only)
                       display-settings --apply       option:read + option:write (PAT only)
                       apply-channels --dry-run       channel:read
                       apply-channels                 channel:read + channel:sensitive_write
                                                      (+ channel:write with --update)
  --creds FILE       0600 file with url= / username= / password= lines: staging
                     root password login while root has no 2FA. Production
                     refuses password login, including for reads, because it
                     creates a session. The password is refused when url=
                     names another site than the server at --base. The login
                     session is revoked when the command ends.
  --target stg|prod  required by every write; checked against the server's
                     ServerAddress (anonymous /api/status) and a public --base.

Commands:
  get-options [--snapshot OUT] [KEY ...]
  display-settings [--apply] (PAT only; fixed CUSTOM / 32; default read-only)
  free-pilot [--apply --expected-plan SHA256 --journal NEWFILE --upstream-key-file FILE]
    PAT scopes: option:read, channel:read; apply also option:write and
    channel:sensitive_write when creating the fixed Qwen channel. Read-only by default.
  verify-settings [--offline] [--plan PLAN | --no-plan] [-v] FILE [FILE ...]
  apply-settings [--snapshot OUT] [--only KEY[,KEY...] | --only-keys-of FILE] FILE [FILE ...]
  apply-pricing [--plan PLAN] [--backup-dir DIR]
  apply-channels [--dry-run] [--update [--rekey]] [--group G] [--with-internal]
                 [--key-alias A --key-file F | --key-env VAR]
                 [--free-key-alias A --free-key-file F | --free-key-env VAR]
  call [--raw] METHOD PATH [JSON] (production GET limited to four known read routes)

Credentials are never printed or written anywhere; values that look like
credentials are masked in every output (`call --raw` opts out).
Settings FILEs are layered, later ones win: staging is
`deploy/settings.json deploy/settings-stg.json`. Keys starting with "_" are
comments. Exit status: 0 ok, 1 a check or write failed, 2 usage, target or
connection error.
"""
import argparse
import datetime
import json
import math
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) woow-newapi-admin/2"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_PLAN = os.path.join(REPO, "deploy", "pricing", "pricing-plan.json")
DEFAULT_SPEC = os.path.join(REPO, "deploy", "pricing", "channels.json")
DEFAULT_CHART_VALUES = os.path.join(REPO, "chart", "values.yaml")

# The two sites, by the ServerAddress each one's settings file sets
# (deploy/settings.json, deploy/settings-stg.json; a unit test keeps them equal).
TARGET_URLS = {"prod": "https://newapi.woowtech.io", "stg": "https://newapi-stg.woowtech.io"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROD_RAW_GET_PATHS = frozenset(("/api/status", "/api/option/", "/api/pricing", "/api/channel/"))

PAT_RE = re.compile(r"nap_[A-Za-z0-9_-]{16,256}")
UPSTREAM_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,256}")
# Values that look like credentials never reach a snapshot or the terminal,
# even if a future release stops hiding them in GET /api/option/
# (rc.44 drops keys ending in Token/Secret/Key/secret/api_key,
# controller/option.go GetOptions). The lookbehind keeps built-in model names
# such as SparkDesk-v3.5 readable.
SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{20,}|nap_[A-Za-z0-9_-]{16,})"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_KEY_RE = re.compile(r"(token|secret|key|password|passwd|api_key)$", re.IGNORECASE)
# JSON fields `call` masks by name (a bare "key" too, except in {key, value} option entries).
SECRET_FIELD_RE = re.compile(r"(token|secret|password|passwd|api_?key|private_?key)$", re.IGNORECASE)
# Synthetic entries GET /api/option/ adds; they are not settable options.
SYNTHETIC_KEYS = {"CompletionRatioMeta"}
# rc.44 start-up exports of the built-in defaults ({"default":1,"svip":1,"vip":1},
# {"vip":{"edit_this":0.9}}). They do not follow GroupRatio / GroupGroupRatio,
# but they are the same maps: a PUT of one replaces the live group ratios
# (setting/ratio_setting/group_ratio.go:31-49, types/rw_map.go:14-19) while
# GroupRatio still reads the old value. Never snapshot, never write.
STALE_KEYS = {"group_ratio_setting.group_ratio", "group_ratio_setting.group_group_ratio"}
NEVER_WRITE = SYNTHETIC_KEYS | STALE_KEYS
PRICING_KEYS = ("ModelRatio", "CompletionRatio", "CacheRatio")
# The description rc.44 gives a caller's own group when GetUserUsableGroups
# adds it to usable_group (service/group.go:34-37).
OWN_GROUP_DESC = "User group"


class Fatal(Exception):
    """Usage, target, connection or authentication error (exit 2)."""


# ---------------------------------------------------------------- transport

def read_creds(path):
    out = {}
    with open(os.path.expanduser(path)) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    return out


def _require_private(path, what):
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise Fatal(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}; a {what} file must be 0600 (chmod 600 {path})")


def read_token_file(path):
    path = os.path.expanduser(path)
    _require_private(path, "PAT")
    with open(path) as f:
        token = f.read().strip()
    if not PAT_RE.fullmatch(token):
        raise Fatal(f"{path}: expected exactly one new-api access token (nap_...)")
    return token


def origin(url):
    """scheme://host[:port] in lower case, or None."""
    try:
        u = urllib.parse.urlsplit((url or "").strip())
        if u.scheme in ("http", "https") and u.hostname:
            return f"{u.scheme}://{u.netloc.lower()}".rstrip("/")
    except ValueError:
        pass
    return None


def target_of(url):
    """'prod' / 'stg' when url is one of the two public origins."""
    o = origin(url)
    return next((t for t, u in TARGET_URLS.items() if o == u), None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies Authorization onto the redirect target: never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        to = urllib.parse.urlsplit(newurl)
        raise Fatal(f"{req.get_method()} {urllib.parse.urlsplit(req.full_url).path}: HTTP {code} redirect to "
                    f"{to.scheme}://{to.hostname or '?'}; --base must be the new-api origin itself "
                    "(a port-forward, not a hostname behind Cloudflare Access); nothing was sent there")


# No environment proxies (the bearer token would pass through them) and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class Client:
    """Bearer-token client: a PAT, or a root password login done on first use."""

    def __init__(self, base, token=None, creds=None):
        try:
            u = urllib.parse.urlsplit(base or "")
            host = u.hostname
        except ValueError:
            u, host = None, None
        if not u or u.scheme not in ("http", "https") or not host:
            raise Fatal("--base must be an http(s) origin, e.g. a port-forward http://127.0.0.1:3301")
        if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise Fatal("--base must be an origin only (scheme://host[:port]): no path, query or user info")
        if u.scheme == "http" and host not in LOOPBACK_HOSTS:
            raise Fatal("--base: plain http only to a port-forward on 127.0.0.1, ::1 or localhost "
                        "(the token would travel in clear text); use https for anything else")
        self.base = f"{u.scheme}://{u.netloc}"
        self.token = token
        self.creds = creds
        self.sid = None
        self._status = None

    def req(self, method, path, body=None, auth=True, allow_error=False):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("User-Agent", UA)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("Authorization", "Bearer " + self.bearer())
        try:
            with _OPENER.open(r, timeout=60) as resp:
                raw = resp.read()
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                raise Fatal(f"{method} {path}: the answer is not JSON (is --base a new-api origin?)") from None
        except urllib.error.HTTPError as e:
            raw = e.read()[:400]
            if allow_error:
                try:
                    d = json.loads(raw or b"{}")
                except ValueError:
                    d = {}
                d = d if isinstance(d, dict) else {}
                d.setdefault("success", False)
                d.setdefault("message", f"HTTP {e.code}")
                return d
            raise Fatal(f"{method} {path}: HTTP {e.code} {redact_value(raw.decode('utf-8', 'replace'))!r}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Fatal(f"{method} {path}: {getattr(e, 'reason', e)}") from None

    def status(self):
        """Anonymous GET /api/status data (cached)."""
        if self._status is None:
            d = self.req("GET", "/api/status", auth=False)
            self._status = d.get("data") if isinstance(d.get("data"), dict) else {}
        return self._status

    def bearer(self):
        if self.token is None:
            if not self.creds or not {"username", "password"} <= set(self.creds):
                raise Fatal("no credentials: pass --token-file (PAT) or --creds (username= and password= lines)")
            d = self.req("POST", "/api/user/login",
                         {"username": self.creds["username"], "password": self.creds["password"]}, auth=False)
            data = d.get("data") or {}
            if not d.get("success") or "access_token" not in data:
                hint = " (root has 2FA: use --token-file)" if data.get("require_verification") else ""
                raise Fatal(f"login failed: {d.get('message')}{hint}")
            self.token = data["access_token"]
            self.sid = (data.get("session") or {}).get("sid")
        return self.token

    def close(self):
        """Revoke the login session a password login opened (rc.44 keeps at
        most 50 active and issues 100 per 24 h per user; service/auth_session.go)."""
        sid, self.sid = self.sid, None
        if not sid:
            return
        try:
            d = self.req("DELETE", "/api/user/sessions/" + urllib.parse.quote(sid, safe=""), allow_error=True)
            ok = bool(d.get("success"))
        except Fatal as e:
            ok, d = False, {"message": str(e)}
        if not ok:
            print(f"warning: the login session {sid[:8]}... was not revoked ({d.get('message')}); "
                  "revoke it under 個人設定 -> 登入工作階段", file=sys.stderr)


def client_from_args(a):
    if not a.base:
        raise Fatal("--base is required: the new-api origin, e.g. a port-forward http://127.0.0.1:3300 "
                    "(there is no default, so nothing reaches a site by accident)")
    if a.token_file and a.creds:
        raise Fatal("pass one of --token-file / --creds")
    if a.token_file:
        c = Client(a.base, token=read_token_file(a.token_file))
    elif a.creds:
        path = os.path.expanduser(a.creds)
        _require_private(path, "credentials")
        creds = read_creds(path)
        c = Client(a.base, creds=creds)
        if is_production_target(c, a):
            raise Fatal("production access requires --token-file; password login creates a session")
        guard_password(c, creds, a.target)
    else:
        raise Fatal("no credentials: pass --token-file (a root PAT) or --creds (root password; only before 2FA)")
    if getattr(a, "clients", None) is not None:
        a.clients.append(c)
    return c


def guard_password(c, creds, target):
    """Send a site's root password to that site only."""
    mine = target_of(creds.get("url"))
    if not mine:
        return
    server = target_of(c.status().get("server_address"))
    if server and server != mine:
        raise Fatal(f"--creds is the {mine} root ({creds.get('url')}), but the server at {c.base} is {server}: "
                    "refusing to send that password")
    if target and target != mine:
        raise Fatal(f"--creds is the {mine} root, but --target is {target}")


def require_target(c, a, writes, out=None):
    """Print which server this is; for writes, insist on a matching --target."""
    st = c.status()
    server = target_of(st.get("server_address"))
    print(f"target: {c.base} (new-api {st.get('version') or '?'}, ServerAddress {st.get('server_address') or '?'}"
          f" -> {server or 'not a known site: a fresh install?'})", file=out or sys.stdout)
    if not writes:
        return a.target or server
    if a.target is None:
        raise Fatal("this command writes: pass --target stg|prod"
                    + (f" (this server's ServerAddress is the {server} one)" if server else ""))
    if server and server != a.target:
        raise Fatal(f"--target {a.target}, but this server's ServerAddress is the {server} one: refusing to write")
    by_base = target_of(c.base)
    if by_base and by_base != a.target:
        raise Fatal(f"--target {a.target}, but --base is the {by_base} URL: refusing to write")
    return a.target


def is_production_target(c, a):
    """Recognize a stated or anonymously identified production origin."""
    return (a.target == "prod" or target_of(c.base) == "prod"
            or target_of(c.status().get("server_address")) == "prod")


def deny_prod_write(target):
    """NA-04 brake: no production mutation through this client before GA evidence."""
    if target == "prod":
        raise Fatal("production writes are disabled until the approved price-notice gate "
                    "and all-writer interlock are implemented; use read-only commands")


def get_options(c):
    d = c.req("GET", "/api/option/")
    if not d.get("success"):
        raise Fatal(f"GET /api/option/: {d.get('message')}")
    return {o["key"]: o["value"] if isinstance(o["value"], str) else json.dumps(o["value"])
            for o in d.get("data") or []}


# ------------------------------------------------------------ value helpers

def encode(value):
    """The string new-api stores for a JSON value sent to PUT /api/option/."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        # Object options are stored as JSON strings; anything else is
        # formatted with Go's %v (map[...]), which some keys accept silently
        # and then ignore.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # strconv.FormatFloat(v, 'f', -1, 64)
    return str(value)


def wire(value):
    """What apply-settings sends as "value"."""
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def close(a, b):
    return _num(a) and _num(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def deep_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if _num(a) or _num(b):
        return close(a, b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def same(expected, live):
    """Does the stored string `live` hold the settings-file value `expected`?"""
    if isinstance(expected, bool):
        return live == encode(expected)
    if _num(expected):
        try:
            return close(float(live), expected)
        except ValueError:
            return False
    if isinstance(expected, (dict, list)):
        try:
            return deep_equal(expected, json.loads(live))
        except ValueError:
            return False
    return live == expected


def short(v, n=120):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = redact_value(s)
    return s if len(s) <= n else s[:n] + f"... ({len(s)} chars)"


def redact_value(s):
    return SECRET_VALUE_RE.sub("[redacted]", s) if isinstance(s, str) else s


def redact_tree(x):
    """A JSON answer with credential-like strings and fields masked."""
    if isinstance(x, str):
        return redact_value(x)
    if isinstance(x, list):
        return [redact_tree(v) for v in x]
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            by_name = isinstance(k, str) and (SECRET_FIELD_RE.search(k) or (k == "key" and "value" not in x))
            out[k] = "[redacted]" if by_name and isinstance(v, str) and v else redact_tree(v)
        return out
    return x


def load_layers(paths):
    merged = {}
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise Fatal(f"{p}: not a JSON object")
        merged.update({k: v for k, v in d.items() if not k.startswith("_")})
    return merged


def load_json(path):
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------------------- snapshots

def write_private_new(path, text):
    """Create path 0600; refuse to overwrite or to write inside this repo."""
    real = os.path.realpath(path)
    if real == REPO or real.startswith(REPO + os.sep):
        raise Fatal(f"{path}: keep snapshots outside the deploy repo (e.g. ~/.config/newapi-backups/)")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Fatal(f"{path} exists; snapshots are never overwritten") from None
    with os.fdopen(fd, "w") as f:
        f.write(text)


def take_snapshot(c, path, opts=None, keys=None):
    """Write the current option values as a flat {key: stored string} file.

    The file is itself a settings file (its metadata key starts with "_"),
    so `apply-settings --only-keys-of deploy/settings.json <snapshot>` (or
    --only KEY,...) puts values back.
    """
    if opts is None:
        opts = get_options(c)
    status = c.status()
    out, redacted, excluded = {}, [], []
    for k in sorted(opts):
        if keys is not None and k not in keys:
            continue
        v = opts[k]
        if k in NEVER_WRITE:
            excluded.append(k)
            continue
        if SECRET_KEY_RE.search(k) or SECRET_VALUE_RE.search(v):
            redacted.append(k)
            continue
        out[k] = v
    meta = {"base": c.base, "taken_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": status.get("version"), "server_address": status.get("server_address"),
            "keys": len(out), "redacted": redacted, "excluded": excluded,
            "restore": "newapi_admin.py --base URL --token-file PAT --target T apply-settings "
                       "--only-keys-of deploy/settings.json THIS_FILE (or --only KEY[,KEY...] THIS_FILE)"}
    write_private_new(path, json.dumps({"_snapshot": meta, **out}, ensure_ascii=False, indent=1) + "\n")
    print(f"snapshot: {len(out)} options -> {path} (0600)"
          + (f"; left out {len(redacted)} credential-like" if redacted else "")
          + (f"; {len(excluded)} computed or stale never included" if excluded else ""))


# ------------------------------------------------------------ invariants
#
# PaaS-dependent new-api settings (AI-models DESIGN §7.1 "I8" and §4.4 I8;
# this list is the single source: the PaaS I8 check implements the entries
# DESIGN §7.1 marks, through the OP PAT, and fails closed). Change one only
# after the PaaS side has been told and updated (docs/OPERATIONS.md,
# "PaaS 依賴的設定不變量").

# Version the rate-limit invariants were validated on: rc.44 counts every
# group in one bucket (upstream fix 45fbb149f, unreleased). Another server
# version fails verify-settings until DESIGN §7.1 has been re-checked and
# this constant and deploy/settings.json are updated together; the offline
# check also fails while chart/values.yaml pins another newapi.image.tag.
INVARIANTS_FOR_VERSION = "v1.0.0-rc.44"
MARKUP = 1.3                  # DECISIONS D5: GroupRatio.default
POINTS_PER_USD = 32           # DECISIONS D5: R, never changes
# Narrow non-price repair. Set the exchange rate before selecting CUSTOM so
# an old rate is not briefly used with the new display type. No caller input,
# settings file, environment override or arbitrary option can enter this tuple.
DISPLAY_SETTINGS = (
    ("general_setting.custom_currency_exchange_rate", "custom_currency_exchange_rate", POINTS_PER_USD),
    ("general_setting.quota_display_type", "quota_display_type", "CUSTOM"),
)
FREE_PER_MINUTE = 10          # DECISIONS D11: free-trial requests per minute
USABLE_GROUPS = {"default", "free"}
SPECIAL_USABLE_KEY = "group_ratio_setting.group_special_usable_group"


class Bad(Exception):
    """A value is missing or has the wrong type."""


def o_raw(o, k):
    if k not in o:
        raise Bad(f"{k} missing")
    return o[k]


def o_bool(o, k):
    v = o_raw(o, k)
    if v not in ("true", "false"):
        raise Bad(f"{k} = {short(v)} is not a bool")
    return v == "true"


def o_num(o, k):
    v = o_raw(o, k)
    try:
        return float(v)
    except ValueError:
        raise Bad(f"{k} = {short(v)} is not a number") from None


def o_obj(o, k, kind=dict):
    v = o_raw(o, k)
    if kind is dict and v.strip() == "":
        return {}
    try:
        d = json.loads(v)
    except ValueError:
        raise Bad(f"{k} = {short(v)} is not JSON") from None
    if not isinstance(d, kind):
        raise Bad(f"{k} = {short(v)} is not a JSON {kind.__name__}")
    return d


class Invariant:
    def __init__(self, name, keys, text, fn, live_only):
        self.name, self.keys, self.text, self.fn, self.live_only = name, keys, text, fn, live_only


INVARIANTS = []


def invariant(name, keys, text, live_only=False):
    def deco(fn):
        INVARIANTS.append(Invariant(name, tuple(keys), text, fn, live_only))
        return fn
    return deco


def _is(o, k, want):
    if isinstance(want, bool):
        got = o_bool(o, k)
        return None if got is want else f"{k} = {encode(got)}"
    got = o_num(o, k)
    return None if close(got, want) else f"{k} = {encode(got)}"


@invariant("usable-groups", ["UserUsableGroups"],
           "UserUsableGroups has exactly the groups default and free (internal is not user-selectable)")
def _(o):
    g = set(o_obj(o, "UserUsableGroups"))
    return None if g == USABLE_GROUPS else f"groups {sorted(g)}"


@invariant("markup", ["GroupRatio"], f"GroupRatio.default == {MARKUP} (D5)")
def _(o):
    r = o_obj(o, "GroupRatio")
    return None if close(r.get("default"), MARKUP) else f"GroupRatio.default = {r.get('default')!r}"


@invariant("free-ratio", ["GroupRatio"], "GroupRatio.free == 0 and no other group has ratio 0")
def _(o):
    r = o_obj(o, "GroupRatio")
    zero = sorted(g for g, v in r.items() if g != "free" and close(v, 0))
    if not close(r.get("free"), 0):
        return f"GroupRatio.free = {r.get('free')!r}"
    return f"ratio 0 also on {zero}" if zero else None


@invariant("group-group-ratio", ["GroupGroupRatio"],
           "GroupGroupRatio is empty (pricing and billing both apply it)")
def _(o):
    r = o_obj(o, "GroupGroupRatio")
    return None if not r else f"GroupGroupRatio has {sorted(r)}"


@invariant("special-usable-groups", [SPECIAL_USABLE_KEY],
           f"{SPECIAL_USABLE_KEY} only removes groups ('-:' entries; an add would open a group to a "
           "user group) and keeps the trial fallback free_blocked -> -:free (DESIGN §1.3.3)")
def _(o):
    r = o_obj(o, SPECIAL_USABLE_KEY)
    if any(not isinstance(m, dict) for m in r.values()):
        return f"entries must be objects: {short(r)}"
    adds = sorted(f"{g}: {s}" for g, m in r.items() for s in m if not s.startswith("-:"))
    if adds:
        return f"adds groups ({adds[:5]}); only '-:' removals are allowed"
    if "-:free" not in (r.get("free_blocked") or {}):
        return "free_blocked does not remove free (the LLM-10 fallback only changes a user's group)"
    return None


@invariant("free-pre-consume", ["quota_setting.enable_free_model_pre_consume"],
           "quota_setting.enable_free_model_pre_consume == false (trial works with 0 balance)")
def _(o):
    return _is(o, "quota_setting.enable_free_model_pre_consume", False)


@invariant("trust-quota", ["quota_setting.trust_quota_usd"],
           "quota_setting.trust_quota_usd == 0 (every paid request pre-consumes)")
def _(o):
    return _is(o, "quota_setting.trust_quota_usd", 0)


@invariant("rate-limit-on", ["ModelRequestRateLimitEnabled", "ModelRequestRateLimitDurationMinutes"],
           "ModelRequestRateLimitEnabled == true with a 1-minute window (D11 free trial per minute)")
def _(o):
    return _is(o, "ModelRequestRateLimitEnabled", True) or _is(o, "ModelRequestRateLimitDurationMinutes", 1)


@invariant("rate-limit-count", ["ModelRequestRateLimitCount"],
           f"ModelRequestRateLimitCount == 0 on {INVARIANTS_FOR_VERSION} (groups share one counter)")
def _(o):
    return _is(o, "ModelRequestRateLimitCount", 0)


@invariant("rate-limit-group", ["ModelRequestRateLimitGroup", "ModelRequestRateLimitSuccessCount"],
           f"ModelRequestRateLimitGroup overrides only free, free[0] == {FREE_PER_MINUTE} (D11) and "
           "free[1] == ModelRequestRateLimitSuccessCount")
def _(o):
    g = o_obj(o, "ModelRequestRateLimitGroup")
    if set(g) != {"free"}:
        return f"overrides for {sorted(g)}"
    free = g["free"]
    if not (isinstance(free, list) and len(free) == 2 and all(_num(x) for x in free)):
        return f"free = {short(free)}"
    if not close(free[0], FREE_PER_MINUTE):
        return f"free[0] = {encode(free[0])}, D11 is {FREE_PER_MINUTE} per minute (PaaS copy and terms say so)"
    success = o_num(o, "ModelRequestRateLimitSuccessCount")
    return None if close(free[1], success) else f"free[1] = {encode(free[1])}, SuccessCount = {encode(success)}"


@invariant("register-off", ["RegisterEnabled"], "RegisterEnabled == false (accounts come from the PaaS)")
def _(o):
    return _is(o, "RegisterEnabled", False)


@invariant("new-user-quota", ["QuotaForNewUser"], "QuotaForNewUser == 0")
def _(o):
    return _is(o, "QuotaForNewUser", 0)


@invariant("aff-off", ["QuotaForInviter", "QuotaForInvitee"], "QuotaForInviter == QuotaForInvitee == 0 (no aff rewards)")
def _(o):
    return _is(o, "QuotaForInviter", 0) or _is(o, "QuotaForInvitee", 0)


@invariant("payments-off", ["payment_setting.compliance_confirmed"],
           "payment_setting.compliance_confirmed == false (redemption codes, subscriptions, top-up, aff stay off)",
           live_only=True)
def _(o):
    return _is(o, "payment_setting.compliance_confirmed", False)


@invariant("checkin-off", ["checkin_setting.enabled"],
           "checkin_setting.enabled == false (check-in credit is logged as type 4, outside the I3 scan)")
def _(o):
    return _is(o, "checkin_setting.enabled", False)


@invariant("consume-log", ["LogConsumeEnabled"], "LogConsumeEnabled == true (usage pages and reconciliation)")
def _(o):
    return _is(o, "LogConsumeEnabled", True)


@invariant("data-export", ["DataExportEnabled"], "DataExportEnabled == true (usage charts)")
def _(o):
    return _is(o, "DataExportEnabled", True)


@invariant("self-use-off", ["SelfUseModeEnabled"], "SelfUseModeEnabled == false (unpriced models stay refused)")
def _(o):
    return _is(o, "SelfUseModeEnabled", False)


@invariant("nav", ["HeaderNavModules"], "HeaderNavModules: rankings == false, pricing.requireAuth == true")
def _(o):
    n = o_obj(o, "HeaderNavModules")
    pricing = n.get("pricing")
    if n.get("rankings") is not False:
        return f"rankings = {short(n.get('rankings'))}"
    if not (isinstance(pricing, dict) and pricing.get("requireAuth") is True):
        return f"pricing = {short(pricing)}"
    return None


@invariant("expose-ratio-off", ["ExposeRatioEnabled"], "ExposeRatioEnabled == false (/api/ratio_config stays 403)")
def _(o):
    return _is(o, "ExposeRatioEnabled", False)


@invariant("points-display", ["general_setting.quota_display_type", "general_setting.custom_currency_exchange_rate"],
           f"general_setting.quota_display_type == CUSTOM at exchange rate {POINTS_PER_USD} (errors show points, N10)")
def _(o):
    t = o_raw(o, "general_setting.quota_display_type")
    if t != "CUSTOM":
        return f"quota_display_type = {short(t)}"
    return _is(o, "general_setting.custom_currency_exchange_rate", POINTS_PER_USD)


def check_invariants(opts, offline=False):
    """[(name, keys, text, problem or None)] for every applicable invariant."""
    out = []
    for inv in INVARIANTS:
        if offline and inv.live_only:
            continue
        try:
            problem = inv.fn(opts)
        except Bad as e:
            problem = str(e) + (" (not set in the settings files)" if offline else "")
        out.append((inv.name, inv.keys, inv.text, problem))
    return out


# Anonymous GET /api/status: the I2 contract C22 fields, then runtime values
# that cross-check options (the option list can lag the effective setting,
# e.g. DisplayInCurrencyEnabled rewrites quota_display_type in memory only).
# The PaaS checks C22 and the runtime fields except version (DESIGN §7.1):
# the version gate is for this tool only, so an upgrade never puts the AI
# tab into maintenance by itself.
STATUS_C22 = [
    ("quota_per_unit", 500000),
    ("password_login_enabled", True),
    ("turnstile_check", False),
    ("password_login_encryption_enabled", False),
    ("enable_batch_update", False),
    ("enable_data_export", True),
    ("register_enabled", False),
    ("self_use_mode_enabled", False),
]
STATUS_RUNTIME = [
    ("quota_display_type", "CUSTOM"),
    ("custom_currency_exchange_rate", POINTS_PER_USD),
    ("checkin_enabled", False),
    ("version", INVARIANTS_FOR_VERSION),
]


def check_status(status):
    out = []
    for group, fields in (("C22", STATUS_C22), ("runtime", STATUS_RUNTIME)):
        for field, want in fields:
            got = status.get(field)
            ok = deep_equal(want, got)
            problem = None if ok else f"{field} = {short(got)}, want {short(want)}"
            if not ok and field == "version":
                problem += ("; the rate-limit invariants were validated on that version only:"
                            " re-check DESIGN §7.1, then update INVARIANTS_FOR_VERSION and deploy/settings.json"
                            " (docs/OPERATIONS.md 〈升版〉)")
            out.append((f"status.{field}", group, problem))
    return out


def chart_image_tag(path):
    """newapi.image.tag in chart/values.yaml (no YAML library in CI)."""
    section = sub = None
    with open(path) as f:
        for line in f:
            text = line.split("#", 1)[0].rstrip()
            if not text.strip():
                continue
            indent = len(text) - len(text.lstrip())
            key = text.strip().split(":", 1)[0]
            if indent == 0:
                section, sub = key, None
            elif section == "newapi" and indent == 2:
                sub = key
            elif section == "newapi" and sub == "image" and indent == 4 and key == "tag":
                return text.split(":", 1)[1].strip().strip("'\"")
    return None


def check_version_pin(path):
    tag = chart_image_tag(path)
    if tag == INVARIANTS_FOR_VERSION:
        return None
    return (f"{os.path.relpath(path, REPO)} pins newapi.image.tag {tag!r}, the invariants are for "
            f"{INVARIANTS_FOR_VERSION}: re-check DESIGN §7.1 and bump INVARIANTS_FOR_VERSION in the same PR "
            "(docs/OPERATIONS.md 〈升版〉)")


def check_settings(expected, opts):
    out = []
    for k, want in expected.items():
        if k not in opts:
            problem = "not returned by GET /api/option/ (unknown on this version, or hidden as a secret)"
        elif not same(want, opts[k]):
            problem = f"want {short(want)}, got {short(opts[k])}"
        else:
            problem = None
        out.append((k, problem))
    return out


def check_pricing_options(plan, opts):
    """Plan ratios == option values; no fixed price or billing expression overrides them."""
    out = []
    for k in PRICING_KEYS:
        try:
            live = o_obj(opts, k)
        except Bad as e:
            out.append((k, str(e)))
            continue
        bad = sorted(m for m, v in plan[k].items() if not close(live.get(m), v))
        out.append((k, None if not bad else f"{len(bad)} model(s) differ from the plan, e.g. "
                    + ", ".join(f"{m}: {live.get(m)!r} != {plan[k][m]!r}" for m in bad[:3])))
    try:
        price = o_obj(opts, "ModelPrice")
        fixed = sorted(set(price) & set(plan["ModelRatio"]))
        out.append(("ModelPrice", None if not fixed else
                    f"fixed prices override the plan ratios for {fixed[:5]}" + ("..." if len(fixed) > 5 else "")))
    except Bad as e:
        out.append(("ModelPrice", str(e)))
    try:
        # rc.44 bills a model by its expression when billing_mode is tiered_expr
        # (relay/helper/price.go ModelPriceHelper), ignoring the ratios.
        mode = o_obj(opts, "billing_setting.billing_mode")
        expr = sorted(m for m in plan["ModelRatio"] if mode.get(m, "ratio") != "ratio")
        out.append(("billing_setting.billing_mode", None if not expr else
                    f"a billing expression overrides the plan ratios for {expr[:5]}" + ("..." if len(expr) > 5 else "")))
    except Bad as e:
        out.append(("billing_setting.billing_mode", str(e)))
    try:
        meta = o_obj(opts, "CompletionRatioMeta")
        locked = sorted(m for m, v in plan["CompletionRatio"].items()
                        if isinstance(meta.get(m), dict) and not close(meta[m].get("ratio"), v))
        out.append(("CompletionRatioMeta", None if not locked else
                    "effective completion ratio differs from the plan (locked by new-api) for "
                    + ", ".join(f"{m}: {meta[m].get('ratio')!r}" for m in locked[:3])))
    except Bad:
        pass  # older servers have no CompletionRatioMeta
    return out


def check_pricing_page(pricing, plan):
    """GET /api/pricing: the effective (in-memory) group ratios, and which
    group serves which model at what price.

    It answers for the caller. rc.44 adds the caller's own group to
    usable_group when it is not user-selectable (service/group.go) and applies
    GroupGroupRatio[caller's group] (controller/pricing.go): the first is
    ignored here, the second is empty by the group-group-ratio invariant, so
    the answer equals what a tenant in group default sees.
    """
    out = []
    usable = dict(pricing.get("usable_group") or {})
    own = [g for g, desc in usable.items() if desc == OWN_GROUP_DESC and g not in USABLE_GROUPS]
    for g in own[:1]:
        del usable[g]
    name = "usable_group" + (f" (caller's own group {own[0]} ignored)" if own[:1] else "")
    out.append((name, None if set(usable) == USABLE_GROUPS else f"usable groups {sorted(usable)}"))
    ratio = pricing.get("group_ratio") or {}
    out.append(("group_ratio", None if close(ratio.get("default"), MARKUP) and close(ratio.get("free"), 0)
                else f"effective default = {ratio.get('default')!r}, free = {ratio.get('free')!r}"))
    groups, items = {}, {}
    for item in pricing.get("data") or []:
        groups[item.get("model_name")] = set(item.get("enable_groups") or [])
        items[item.get("model_name")] = item
    # With enable_free_model_pre_consume false, rc.44 treats ModelRatio 0 (or
    # ModelPrice 0) as a free model in any group (relay/helper/price.go): it
    # skips billing, the free group's rate limit and the PaaS daily meter.
    unpriced = []
    for m, g in groups.items():
        it = items[m]
        if not (g - {"free"}) or it.get("billing_mode") == "tiered_expr":
            continue
        price = it.get("model_price") if it.get("quota_type") == 1 else it.get("model_ratio")
        if not (_num(price) and price > 0):
            unpriced.append(m)
    unpriced.sort()
    out.append(("paid-models-priced", None if not unpriced else
                f"{len(unpriced)} model(s) in a paid group have price 0 and are free for everyone, e.g. {unpriced[:5]}"))
    if plan is None:
        return out
    local, api = set(plan["local"]), set(plan["api"])
    free_models = sorted(m for m, g in groups.items() if "free" in g and m not in local)
    out.append(("free-group-models", None if not free_models else f"group free also serves {free_models[:5]}"))
    paid_local = sorted(m for m in local if groups.get(m, set()) - {"free"})
    out.append(("local-models-free-only", None if not paid_local else
                f"{paid_local} served in {sorted(set().union(*(groups[m] for m in paid_local)) - {'free'})}"))
    missing_local = sorted(m for m in local if "free" not in groups.get(m, set()))
    out.append(("free-channel", None if not missing_local else f"group free does not serve {missing_local}"))
    missing = sorted(m for m in api if "default" not in groups.get(m, set()))
    out.append(("paid-channel", None if not missing else
                f"group default does not serve {len(missing)} plan model(s) (channel missing or disabled?), e.g. {missing[:3]}"))
    return out


def cmd_verify_settings(a):
    expected = load_layers(a.files)
    plan = None if a.no_plan else load_json(a.plan)
    rows = []  # (section, name, problem)
    if a.offline:
        encoded = {k: encode(v) for k, v in expected.items()}
        rows += [("invariant", f"{n} [{','.join(k)}]: {t}", p) for n, k, t, p in check_invariants(encoded, offline=True)]
        rows.append(("version", f"chart newapi.image.tag == INVARIANTS_FOR_VERSION ({INVARIANTS_FOR_VERSION})",
                     check_version_pin(a.chart)))
        if plan is not None:
            spec = load_json(a.spec)
            rows += [("channels", n, p) for n, p in check_channel_spec(spec, plan)]
        where = "offline (settings files only)"
    else:
        c = client_from_args(a)
        status = c.status()
        rows += [("status", n, p) for n, _, p in check_status(status)]
        opts = get_options(c)
        rows += [("invariant", f"{n} [{','.join(k)}]: {t}", p) for n, k, t, p in check_invariants(opts)]
        rows += [("settings", n, p) for n, p in check_settings(expected, opts)]
        if plan is not None:
            rows += [("pricing", n, p) for n, p in check_pricing_options(plan, opts)]
        pricing = c.req("GET", "/api/pricing", allow_error=True)
        if pricing.get("success"):
            rows += [("pricing-page", n, p) for n, p in check_pricing_page(pricing, plan)]
        else:
            rows.append(("pricing-page", "GET /api/pricing", str(pricing.get("message"))))
        where = f"{c.base} ({status.get('version') or '?'}, ServerAddress {status.get('server_address') or '?'})"
    failed = [r for r in rows if r[2] is not None]
    for section, name, problem in rows:
        if problem is not None:
            print(f"FAIL {section} {name}: {problem}")
        elif a.verbose:
            print(f"ok   {section} {name}")
    by = {}
    for section, _, problem in rows:
        n, f = by.get(section, (0, 0))
        by[section] = (n + 1, f + (problem is not None))
    print(f"verify-settings {where}: {len(rows)} checks, {len(failed)} failed ("
          + ", ".join(f"{s} {n - f}/{n}" for s, (n, f) in by.items()) + ")")
    return 1 if failed else 0


# ----------------------------------------------------------- commands

def _display_status(c, a):
    """Require the reviewed server and all status invariants except the repair."""
    try:
        answer = c.req("GET", "/api/status", auth=False)
    except Fatal:
        raise Fatal("display-settings status read failed; upstream details withheld") from None
    if not isinstance(answer, dict) or answer.get("success") is not True or not isinstance(answer.get("data"), dict):
        raise Fatal("display-settings requires a successful status response")
    status = answer["data"]
    if status.get("server_address") not in (TARGET_URLS[a.target], TARGET_URLS[a.target] + "/"):
        raise Fatal("display-settings requires a known ServerAddress matching --target")
    exempt = {"status." + field for _, field, _ in DISPLAY_SETTINGS}
    if any(problem for name, _, problem in check_status(status) if name not in exempt):
        raise Fatal("display-settings requires rc.44 and matching non-display status invariants")
    return status


def _display_options(c):
    try:
        answer = c.req("GET", "/api/option/")
    except Fatal:
        raise Fatal("display-settings option read failed; upstream details withheld") from None
    if not isinstance(answer, dict) or answer.get("success") is not True or not isinstance(answer.get("data"), list):
        raise Fatal("display-settings requires a successful option response")
    opts = {}
    for entry in answer["data"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("key"), str)
                or not isinstance(entry.get("value"), str) or entry["key"] in opts):
            raise Fatal("display-settings requires unambiguous string-valued options")
        opts[entry["key"]] = entry["value"]
    if any(key not in opts for key, _, _ in DISPLAY_SETTINGS):
        raise Fatal("display-settings requires both known display options in the option response")
    return opts


def cmd_display_settings(a):
    """Plan or repair only two fixed display options; no broad prod permit."""
    if a.target is None:
        raise Fatal("display-settings requires --target stg|prod, including for a plan")
    if not a.token_file or a.creds:
        raise Fatal("display-settings requires --token-file; password sessions are not permitted")
    c = client_from_args(a)
    if (origin(c.base) != TARGET_URLS[a.target]
            and urllib.parse.urlsplit(c.base).hostname not in LOOPBACK_HOSTS):
        raise Fatal("display-settings --base must be the matching public origin or a loopback port-forward")
    before_status = _display_status(c, a)
    before = _display_options(c)
    pending = [(key, field, value) for key, field, value in DISPLAY_SETTINGS
               if not same(value, before[key]) or not deep_equal(value, before_status.get(field))]
    report = {
        "command": "display-settings", "target": a.target,
        "mode": "apply" if a.apply else "plan", "version": INVARIANTS_FOR_VERSION,
        "settings": [{"key": key, "desired": value,
                      "option_matches": same(value, before[key]),
                      "runtime_matches": deep_equal(value, before_status.get(field))}
                     for key, field, value in DISPLAY_SETTINGS],
        "would_write": [key for key, _, _ in pending], "attempted": [], "applied": [],
    }
    if not a.apply:
        report["result"] = "plan"
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    # Retain only an in-memory baseline; no broad option dump/snapshot. Check
    # every other visible option, including pricing, after the bounded writes.
    display_keys = {key for key, _, _ in DISPLAY_SETTINGS}
    baseline = {k: v for k, v in before.items() if k not in display_keys | SYNTHETIC_KEYS}
    failure = None
    for key, _, value in pending:
        report["attempted"].append(key)
        try:
            answer = c.req("PUT", "/api/option/", {"key": key, "value": value}, allow_error=True)
            if not isinstance(answer, dict) or answer.get("success") is not True:
                failure = "display update rejected; stopped before any further write"
                break
            report["applied"].append(key)
        except Fatal:
            # A timeout can occur after the server committed. Do not retry or
            # echo a potentially sensitive upstream response. Read back below.
            failure = "display update outcome uncertain; stopped before any further write"
            break
    try:
        after_status = _display_status(c, a)
        after = _display_options(c)
        report["remaining"] = [key for key, field, value in DISPLAY_SETTINGS
                               if not same(value, after[key]) or not deep_equal(value, after_status.get(field))]
        report["other_options_unchanged"] = (
            baseline == {k: v for k, v in after.items() if k not in display_keys | SYNTHETIC_KEYS})
        if report["remaining"] or not report["other_options_unchanged"]:
            failure = failure or "readback failed; inspect current state before rerunning"
    except Fatal:
        failure = failure or "readback unavailable or target/status changed; inspect current state"
    report["result"] = "failed" if failure else "verified"
    if failure:
        report["reason"] = failure
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if failure else 0


def cmd_get_options(a):
    c = client_from_args(a)
    opts = get_options(c)
    if a.snapshot:
        take_snapshot(c, a.snapshot, opts, keys=set(a.keys) or None)
        return 0
    for k in sorted(opts):
        if a.keys and k not in a.keys:
            continue
        v = "[redacted]" if SECRET_KEY_RE.search(k) else redact_value(opts[k])
        print(f"{k} = {v!r}" + ("  # stale start-up export, not the live value" if k in STALE_KEYS else ""))
    return 0


def _settings_selection(a):
    """(settings to write, keys skipped) from the files and --only / --only-keys-of."""
    snapshot = False
    for p in a.files:
        d = load_json(p)
        if not isinstance(d, dict):
            raise Fatal(f"{p}: not a JSON object")
        snapshot |= "_snapshot" in d
        bad = sorted(set(d) & NEVER_WRITE)
        if bad:
            raise Fatal(f"{p} holds {bad}: computed by new-api or a stale rc.44 export that would overwrite the "
                        "live group ratios; remove them from the file")
    if a.only and a.only_keys_of:
        raise Fatal("pass one of --only / --only-keys-of")
    if snapshot and not (a.only or a.only_keys_of):
        raise Fatal("restoring a snapshot needs --only-keys-of deploy/settings.json or --only KEY[,KEY...]: "
                    "never write back every option")
    settings = load_layers(a.files)
    if a.only:
        only = [k for part in a.only for k in part.split(",") if k]
        never = sorted(set(only) & NEVER_WRITE)
        if never:
            raise Fatal(f"--only: {never} must never be written")
        unknown = [k for k in only if k not in settings]
        if unknown:
            raise Fatal(f"--only: {unknown} not in {a.files}")
        return {k: settings[k] for k in only}, []
    if a.only_keys_of:
        wanted = list(load_layers(a.only_keys_of))
        skipped = [k for k in wanted if k not in settings]
        return {k: settings[k] for k in wanted if k in settings}, skipped
    return settings, []


def cmd_apply_settings(a):
    settings, skipped = _settings_selection(a)
    c = client_from_args(a)
    target = require_target(c, a, writes=True)
    if target == "prod" and any(os.path.basename(p) == "settings-stg.json" for p in a.files):
        raise Fatal("deploy/settings-stg.json is the staging layer: never apply it to prod")
    if "ServerAddress" in settings and target_of(settings["ServerAddress"]) != target:
        raise Fatal(f"the files set ServerAddress {settings['ServerAddress']!r}, not the {target} URL "
                    f"{TARGET_URLS[target]}" + (": staging layers deploy/settings-stg.json on top" if target == "stg" else ""))
    deny_prod_write(target)
    if a.snapshot:
        take_snapshot(c, a.snapshot)
    failed = 0
    for key, value in settings.items():
        d = c.req("PUT", "/api/option/", {"key": key, "value": wire(value)}, allow_error=True)
        ok = bool(d.get("success"))
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {key} = {short(encode(value))}" + ("" if ok else f"  ({d.get('message')})"))
    for key in skipped:
        print(f"skip {key} (not in {', '.join(a.files)})")
    print(f"apply-settings: {len(settings) - failed}/{len(settings)} applied"
          + (f", {len(skipped)} skipped" if skipped else "") + "; run verify-settings next")
    return 1 if failed else 0


def cmd_apply_pricing(a):
    """Merge the plan into ModelRatio / CompletionRatio / CacheRatio
    (ratio 1 = US$2 per 1M tokens; model ratios equal LiteLLM cost, the
    markup lives in GroupRatio) and drop fixed prices for plan models."""
    plan = load_json(a.plan)
    c = client_from_args(a)
    target = require_target(c, a, writes=True)
    deny_prod_write(target)
    opts = get_options(c)
    os.makedirs(a.backup_dir, mode=0o700, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9.-]+", "_", c.base.split("//", 1)[-1])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup, n = os.path.join(a.backup_dir, f"pricing-before-{host}-{stamp}.json"), 1
    while os.path.exists(backup):
        n += 1
        backup = os.path.join(a.backup_dir, f"pricing-before-{host}-{stamp}-{n}.json")
    take_snapshot(c, backup, opts, keys=set(PRICING_KEYS) | {"ModelPrice"})
    failed = 0
    for k in PRICING_KEYS:
        cur = json.loads(opts.get(k) or "{}")
        new = {**cur, **plan[k]}
        if deep_equal(new, cur):
            print(f"{k}: {len(plan[k])} from plan, {len(cur)} total, unchanged")
            continue
        d = c.req("PUT", "/api/option/", {"key": k, "value": json.dumps(new, ensure_ascii=False)}, allow_error=True)
        failed += not d.get("success")
        print(f"{k}: {len(plan[k])} from plan, {len(new)} total -> {d.get('success')} {d.get('message') or ''}".rstrip())
    price = json.loads(opts.get("ModelPrice") or "{}")
    fixed = sorted(set(price) & set(plan["ModelRatio"]))
    if fixed:
        for m in fixed:
            del price[m]
        d = c.req("PUT", "/api/option/", {"key": "ModelPrice", "value": json.dumps(price, ensure_ascii=False)},
                  allow_error=True)
        failed += not d.get("success")
        print(f"ModelPrice: dropped fixed prices for {fixed} -> {d.get('success')} {d.get('message') or ''}".rstrip())
    return 1 if failed else 0


# ------------------------------------------------------------- channels

CHANNEL_FIELDS = ("type", "base_url", "group", "models", "test_model", "param_override", "auto_ban", "priority", "weight")


def wanted_channel(spec, plan, ch, label, key_alias):
    override = ch.get("param_override")
    return {
        "name": ch["name"], "type": spec["type"], "base_url": spec["base_url"], "group": ch["group"],
        "models": ",".join(plan[ch["models"]]), "test_model": ch["test_model"],
        "param_override": json.dumps(override, ensure_ascii=False) if override else "",
        "auto_ban": ch.get("auto_ban", 1), "priority": ch.get("priority", 0), "weight": ch.get("weight", 0),
        "remark": f"{label}; LiteLLM key alias {key_alias}",
    }


def select_channels(spec, groups=None, with_internal=False):
    """Spec channels to handle: --group narrows; optional ones need --with-internal or their --group."""
    known = {ch["group"] for ch in spec["channels"]}
    unknown = sorted(set(groups or ()) - known)
    if unknown:
        raise Fatal(f"--group {unknown}: the spec has groups {sorted(known)}")
    out = []
    for ch in spec["channels"]:
        if groups and ch["group"] not in groups:
            continue
        if ch.get("optional") and not with_internal and not groups:
            continue
        out.append(ch)
    return out


def wanted_channels(spec, plan, label, key_alias, with_internal):
    return [wanted_channel(spec, plan, ch, label, key_alias) for ch in select_channels(spec, None, with_internal)]


def check_channel_spec(spec, plan):
    """Offline: the spec's model lists exist, test models are served, groups are
    the PaaS ones, and paid groups serve only priced models."""
    out = []
    groups = {}
    for ch in spec["channels"]:
        models = plan.get(ch["models"])
        if not isinstance(models, list) or not models:
            out.append((ch["name"], f"plan has no model list {ch['models']!r}"))
            continue
        if ch["test_model"] not in models:
            out.append((ch["name"], f"test_model {ch['test_model']} is not in plan[{ch['models']!r}]"))
            continue
        groups.setdefault(ch["group"], set()).update(models)
        out.append((ch["name"], None))
    free = groups.get("free", set())
    out.append(("free group = plan local models", None if free == set(plan["local"]) else f"free serves {sorted(free)}"))
    paid = set().union(*(m for g, m in groups.items() if g != "free"))
    leaked = sorted(set(plan["local"]) & paid)
    out.append(("local models only in free", None if not leaked else f"{leaked} also in a paid group"))
    # ModelRatio 0 is free for everyone with enable_free_model_pre_consume false
    # (relay/helper/price.go), in whatever group serves the model.
    zero = sorted(m for m in paid if not (_num(plan["ModelRatio"].get(m)) and plan["ModelRatio"][m] > 0))
    out.append(("paid groups serve only priced models (ModelRatio > 0)", None if not zero else
                f"{len(zero)} model(s) with ratio 0 or none, e.g. {zero[:5]}"))
    return out


def _norm_set(s):
    return sorted(x.strip() for x in (s or "").split(",") if x.strip())


def _count_sample(names, n=5):
    """'13 [a, b, c, d, e, ...]': the count, so a cut-off list is not read as all of it."""
    s = sorted(names)
    return f"{len(s)} {s[:n]}" + ("..." if len(s) > n else "")


def channel_diff(want, have):
    diff = []
    for f in CHANNEL_FIELDS:
        w, h = want[f], have.get(f)
        if f in ("group", "models"):
            eq = _norm_set(w) == _norm_set(h)
        elif f == "param_override":
            try:
                eq = (json.loads(w) if w else None) == (json.loads(h) if h else None)
            except ValueError:
                eq = False
        else:
            eq = (w == h) or (_num(w) and close(w, h))
        if not eq:
            if f == "models":
                ws, hs = set(_norm_set(w)), set(_norm_set(h))
                diff.append(f"models +{_count_sample(ws - hs)} -{_count_sample(hs - ws)}")
            else:
                diff.append(f"{f} {short(h, 60)} -> {short(w, 60)}")
    return diff


def list_channels(c):
    items, page = [], 1
    while True:
        d = c.req("GET", f"/api/channel/?p={page}&page_size=100")
        if not d.get("success"):
            raise Fatal(f"GET /api/channel/: {d.get('message')}")
        data = d.get("data") or {}
        batch = data.get("items", []) if isinstance(data, dict) else data
        items += batch
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        if not batch or len(items) >= total:
            return items
        page += 1


def read_upstream_key(path, env, what):
    if path and env:
        raise Fatal(f"{what}: pass one of the -file / -env options")
    if path == "-":
        key = sys.stdin.read().strip()
    elif path:
        _require_private(os.path.expanduser(path), "key")
        with open(os.path.expanduser(path)) as f:
            key = f.read().strip()
    elif env:
        key = os.environ.get(env, "").strip()
    else:
        raise Fatal(f"{what} is needed: a channel is missing (or --rekey): pass it with a 0600 file "
                    "(or - for stdin) or an environment variable")
    if not UPSTREAM_KEY_RE.fullmatch(key):
        raise Fatal(f"{what} does not look like a LiteLLM key (sk-...)")
    return key


def cmd_apply_channels(a):
    """Plan every create / update first; read and check the keys they need
    before anything is written (in --dry-run too); creates go first, so a
    failure never leaves the trial without its channel."""
    spec, plan = load_json(a.spec), load_json(a.plan)
    problems = [f"{n}: {p}" for n, p in check_channel_spec(spec, plan) if p]
    if problems:
        raise Fatal("channel spec does not match the plan: " + "; ".join(problems))
    if a.rekey and not a.update:
        raise Fatal("--rekey needs --update")
    if a.key_file == "-" and a.free_key_file == "-":
        raise Fatal("only one key can come from stdin")
    free_key = bool(a.free_key_file or a.free_key_env)
    if free_key and not a.free_key_alias:
        raise Fatal("--free-key-file / --free-key-env need --free-key-alias (it goes into the remark)")
    selected = select_channels(spec, a.group, a.with_internal)
    c = client_from_args(a)
    target = require_target(c, a, writes=not a.dry_run)
    if not a.dry_run:
        deny_prod_write(target)
    label = a.label or target or "?"
    have = {}
    for ch in list_channels(c):
        have.setdefault(ch.get("name"), []).append(ch)

    steps = []  # (kind, want, existing channel, diff, key slot)
    for ch in selected:
        slot = "free" if ch["group"] == "free" and free_key else "main"
        want = wanted_channel(spec, plan, ch, label, a.free_key_alias if slot == "free" else a.key_alias)
        found = have.get(want["name"], [])
        if len(found) > 1:
            steps.append(("dup", want, None, None, slot))
        elif not found:
            steps.append(("create", want, None, None, slot))
        else:
            diff = channel_diff(want, found[0])
            steps.append(("drift" if diff else "ok", want, found[0], diff, slot))

    keys = {}  # slot -> key; a dry run without key options only reports what is missing
    for kind, want, _, _, slot in steps:
        if not (kind == "create" or (a.rekey and kind in ("ok", "drift"))) or slot in keys:
            continue
        if slot == "free":
            if a.dry_run and not free_key:
                continue
            keys[slot] = read_upstream_key(a.free_key_file, a.free_key_env, "the free-channel key (--free-key-*)")
        else:
            if a.dry_run and not (a.key_file or a.key_env):
                continue
            if not a.key_alias:
                raise Fatal("--key-alias is needed to create or rekey a channel (it goes into the remark)")
            keys[slot] = read_upstream_key(a.key_file, a.key_env, "the LiteLLM key (--key-file / --key-env)")

    failed = 0
    order = [s for s in steps if s[0] == "create"] + [s for s in steps if s[0] != "create"]
    for kind, want, ch, diff, slot in order:
        tag = f"{want['name']} (group {want['group']}, {len(_norm_set(want['models']))} models)"
        if kind == "dup":
            print(f"FAIL {tag}: {len(have[want['name']])} channels with this name; fix by hand")
            failed += 1
        elif kind == "create":
            if a.dry_run:
                print(f"MISSING {tag}: would create "
                      + ("(key checked)" if slot in keys else "(no key given: the real run needs one)"))
                failed += 1
                continue
            channel = dict(want, key=keys[slot])
            if not channel["param_override"]:
                del channel["param_override"]
            d = c.req("POST", "/api/channel/", {"mode": "single", "channel": channel}, allow_error=True)
            del channel
            ok = bool(d.get("success"))
            failed += not ok
            print(f"{'created' if ok else 'FAIL create'} {tag}" + ("" if ok else f": {d.get('message')}"))
        else:
            note = "" if ch.get("status") == 1 else f" [status {ch.get('status')}: disabled]"
            what = "; ".join(diff) + ("; " if diff and a.rekey else "") + ("key -> " + want["remark"] if a.rekey else "")
            if kind == "ok" and not a.rekey:
                print(f"ok {tag} id={ch.get('id')}{note}")
            elif a.dry_run or not a.update:
                print(f"DRIFT {tag} id={ch.get('id')}{note}: {what}"
                      + (" (would update)" if a.update else " (--update to fix; the key is kept)"))
                failed += kind == "drift"
            else:
                body = {"id": ch["id"], "name": want["name"], **{f: want[f] for f in CHANNEL_FIELDS}}
                if a.rekey:
                    body.update(key=keys[slot], remark=want["remark"])
                d = c.req("PUT", "/api/channel/", body, allow_error=True)
                del body
                ok = bool(d.get("success"))
                failed += not ok
                print(f"{'updated' if ok else 'FAIL update'} {tag} id={ch['id']}: {what}"
                      + ("" if ok else f" ({d.get('message')})"))
    return 1 if failed else 0


def cmd_call(a):
    c = client_from_args(a)
    if a.method.upper() != "GET":
        target = require_target(c, a, writes=True, out=sys.stderr)  # stdout stays JSON
        deny_prod_write(target)
    elif is_production_target(c, a) and a.path not in PROD_RAW_GET_PATHS:
        raise Fatal("production raw GET is limited to known read-only endpoints")
    body = json.loads(a.json) if a.json else None
    if a.path.rstrip("/") == "/api/option" and isinstance(body, dict) and body.get("key") in NEVER_WRITE:
        raise Fatal(f"{body['key']} must never be written (on rc.44 it replaces the live group ratios)")
    answer = c.req(a.method.upper(), a.path, body)
    if a.raw:
        print("warning: --raw output is not redacted and may hold credentials", file=sys.stderr)
    print(json.dumps(answer if a.raw else redact_tree(answer), ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="new-api origin (a port-forward URL); required, no default")
    ap.add_argument("--token-file", help="0600 file with a root PAT (nap_...); no password login")
    ap.add_argument("--creds", help="0600 url=/username=/password= file for a root password login (before 2FA only)")
    ap.add_argument("--target", choices=sorted(TARGET_URLS), help="site a write is meant for; checked against the server")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get-options", help="print options, or --snapshot them to a 0600 file")
    p.add_argument("--snapshot", metavar="OUT")
    p.add_argument("keys", nargs="*")
    p.set_defaults(fn=cmd_get_options)

    p = sub.add_parser("display-settings", help="plan two fixed display values; PAT only, no price writes")
    p.add_argument("--apply", action="store_true", help="write only CUSTOM / 32 and verify; default is read-only")
    p.set_defaults(fn=cmd_display_settings)

    from free_pilot import cmd_free_pilot
    p = sub.add_parser("free-pilot", help="bounded Qwen free route; preserves paid prices; PAT only")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-plan", help="SHA256 of the current read-only plan")
    p.add_argument("--journal", help="exclusive new intent journal; never overwritten or auto-resumed")
    p.add_argument("--upstream-key-file", help="private LiteLLM key file; only needed for channel creation")
    p.set_defaults(fn=cmd_free_pilot)

    p = sub.add_parser("verify-settings", help="check settings files, PaaS invariants, status, pricing")
    p.add_argument("files", nargs="+")
    p.add_argument("--offline", action="store_true", help="check the files against the invariants only (CI)")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--no-plan", action="store_true", help="skip the pricing and channel checks")
    p.add_argument("--spec", default=DEFAULT_SPEC)
    p.add_argument("--chart", default=DEFAULT_CHART_VALUES, help="chart values whose newapi.image.tag --offline checks")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_verify_settings)

    p = sub.add_parser("apply-settings", help="PUT every key of the settings files")
    p.add_argument("files", nargs="+")
    p.add_argument("--only", action="append", metavar="KEY[,KEY]")
    p.add_argument("--only-keys-of", action="append", metavar="FILE",
                   help="write only the keys this settings file sets (restore from a snapshot; missing keys are skipped)")
    p.add_argument("--snapshot", metavar="OUT", help="snapshot all options to OUT first")
    p.set_defaults(fn=cmd_apply_settings)

    p = sub.add_parser("apply-pricing", help="merge deploy/pricing/pricing-plan.json into the ratio options")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--backup-dir", default=os.path.expanduser("~/.config/newapi-backups"))
    p.set_defaults(fn=cmd_apply_pricing)

    p = sub.add_parser("apply-channels", help="create / check / update the channels of deploy/pricing/channels.json")
    p.add_argument("--label", help="goes into the remark (default: --target)")
    p.add_argument("--key-alias", help="LiteLLM key alias of --key-file / --key-env (goes into the remark)")
    p.add_argument("--key-file", help="0600 file with the LiteLLM key, or - for stdin")
    p.add_argument("--key-env", help="environment variable holding the LiteLLM key")
    p.add_argument("--free-key-alias", help="alias of the dedicated free-channel key (NA-08)")
    p.add_argument("--free-key-file", help="0600 file with the key for the group free channel, or - for stdin")
    p.add_argument("--free-key-env", help="environment variable holding the free-channel key")
    p.add_argument("--group", action="append", help="only the spec channels of this group (repeatable)")
    p.add_argument("--spec", default=DEFAULT_SPEC)
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--with-internal", action="store_true", help="also the ChatGPT-subscription test channel")
    p.add_argument("--update", action="store_true", help="PUT drifted channels back to the spec (key unchanged)")
    p.add_argument("--rekey", action="store_true", help="with --update: also replace the key (and remark) of existing channels")
    p.add_argument("--dry-run", action="store_true", help="write nothing; drift or a missing channel exits 1")
    p.set_defaults(fn=cmd_apply_channels)

    p = sub.add_parser("call", help="one raw API call (answer redacted unless --raw)")
    p.add_argument("--raw", action="store_true", help="print the answer unredacted (may hold credentials)")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("json", nargs="?")
    p.set_defaults(fn=cmd_call)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    a.clients = []
    try:
        return a.fn(a)
    except Fatal as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # missing settings, plan, creds or token file
        print(f"error: {e.filename or ''}: {e.strerror or e}", file=sys.stderr)
        return 2
    finally:
        for c in a.clients:
            c.close()


if __name__ == "__main__":
    # The bounded command module shares this client's exception/transport
    # identity when this file is executed as a script rather than imported.
    sys.modules.setdefault("newapi_admin", sys.modules[__name__])
    sys.exit(main())
