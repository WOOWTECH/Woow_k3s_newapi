#!/usr/bin/env python3
"""Admin client for new-api: settings, pricing and channels as code.

Authentication (pick one; credentials are never printed or written anywhere):
  --token-file FILE  a personal access token (nap_...) of root, alone in a
                     0600 file. No password login, so it works when root has
                     2FA (production). Scopes per command:
                       get-options, verify-settings   option:read
                       apply-settings, apply-pricing  option:read + option:write
                       apply-channels                 channel:read + channel:sensitive_write
                                                      (+ channel:write with --update)
  --creds FILE       url= / username= / password= lines: root password login.
                     Only while root has no 2FA (staging; first install). The
                     15-minute access token stays in memory.
  --base URL         new-api origin, e.g. a port-forward http://127.0.0.1:3301.
                     Required with --token-file; defaults to the url= line of --creds.

Commands:
  get-options [--snapshot OUT] [KEY ...]
  verify-settings [--offline] [--plan PLAN | --no-plan] [-v] FILE [FILE ...]
  apply-settings [--snapshot OUT] [--only KEY[,KEY...]] FILE [FILE ...]
  apply-pricing [--plan PLAN] [--backup-dir DIR]
  apply-channels --label L --key-alias A [--key-file F | --key-env VAR]
                 [--spec SPEC] [--plan PLAN] [--with-internal] [--update] [--dry-run]
  call METHOD PATH [JSON]

Settings FILEs are layered, later ones win: staging is
`deploy/settings.json deploy/settings-stg.json`. Keys starting with "_" are
comments. Exit status: 0 ok, 1 a check or write failed, 2 usage or
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
DEFAULT_CREDS = os.path.expanduser("~/.config/newapi/root-credentials")

PAT_RE = re.compile(r"nap_[A-Za-z0-9_-]{16,256}")
UPSTREAM_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,256}")
# Values that look like credentials never reach a snapshot or the terminal,
# even if a future release stops hiding them in GET /api/option/
# (rc.44 drops keys ending in Token/Secret/Key/secret/api_key,
# controller/option.go GetOptions).
SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9]{20,}|nap_[A-Za-z0-9_-]{16,})"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_KEY_RE = re.compile(r"(token|secret|key|password|passwd|api_key)$", re.IGNORECASE)
# Synthetic entries GET /api/option/ adds; they are not settable options.
SYNTHETIC_KEYS = {"CompletionRatioMeta"}
PRICING_KEYS = ("ModelRatio", "CompletionRatio", "CacheRatio")


class Fatal(Exception):
    """Usage, connection or authentication error (exit 2)."""


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


class Client:
    """Bearer-token client: a PAT, or a root password login done on first use."""

    def __init__(self, base, token=None, creds=None):
        if not base or not re.match(r"https?://", base):
            raise Fatal("--base must be an http(s) URL (a port-forward such as http://127.0.0.1:3301)")
        self.base = base.rstrip("/")
        self.token = token
        self.creds = creds

    def req(self, method, path, body=None, auth=True, allow_error=False):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("User-Agent", UA)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("Authorization", "Bearer " + self.bearer())
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
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
            raise Fatal(f"{method} {path}: HTTP {e.code} {raw!r}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Fatal(f"{method} {path}: {getattr(e, 'reason', e)}") from None

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
        return self.token


def client_from_args(a):
    if a.token_file:
        if not a.base:
            raise Fatal("--token-file needs --base")
        return Client(a.base, token=read_token_file(a.token_file))
    creds = read_creds(a.creds)
    return Client(a.base or creds.get("url"), creds=creds)


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
    so `apply-settings --only KEY,... <snapshot>` puts values back.
    """
    if opts is None:
        opts = get_options(c)
    status = c.req("GET", "/api/status", auth=False, allow_error=True).get("data") or {}
    out, redacted = {}, []
    for k in sorted(opts):
        if keys is not None and k not in keys:
            continue
        v = opts[k]
        if k in SYNTHETIC_KEYS:
            continue
        if SECRET_KEY_RE.search(k) or SECRET_VALUE_RE.search(v):
            redacted.append(k)
            continue
        out[k] = v
    meta = {"base": c.base, "taken_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": status.get("version"), "keys": len(out), "redacted": redacted,
            "restore": "newapi_admin.py --base URL --token-file PAT apply-settings --only KEY[,KEY...] THIS_FILE"}
    write_private_new(path, json.dumps({"_snapshot": meta, **out}, ensure_ascii=False, indent=1) + "\n")
    print(f"snapshot: {len(out)} options -> {path} (0600)" + (f"; left out {len(redacted)} credential-like" if redacted else ""))


# ------------------------------------------------------------ invariants
#
# PaaS-dependent new-api settings (AI-models DESIGN §7.1 "I8" and §4.4 I8;
# the PaaS checks the same list through the OP PAT and fails closed). Change
# one only after the PaaS side has been told and updated
# (docs/OPERATIONS.md, "PaaS 依賴的設定不變量").

# Version the rate-limit invariants were validated on: rc.44 counts every
# group in one bucket (upstream fix 45fbb149f, unreleased). Another server
# version fails verify-settings until DESIGN §7.1 has been re-checked and
# this constant and deploy/settings.json are updated together.
INVARIANTS_FOR_VERSION = "v1.0.0-rc.44"
MARKUP = 1.3                  # DECISIONS D5: GroupRatio.default
POINTS_PER_USD = 32           # DECISIONS D5: R, never changes
USABLE_GROUPS = {"default", "free"}


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


@invariant("special-usable-groups", ["group_ratio_setting.group_special_usable_group"],
           "group_ratio_setting.group_special_usable_group is empty (it adds usable groups per user group)")
def _(o):
    r = o_obj(o, "group_ratio_setting.group_special_usable_group")
    return None if not r else f"set for {sorted(r)}"


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
           "ModelRequestRateLimitGroup overrides only free, and free[1] == ModelRequestRateLimitSuccessCount")
def _(o):
    g = o_obj(o, "ModelRequestRateLimitGroup")
    if set(g) != {"free"}:
        return f"overrides for {sorted(g)}"
    free = g["free"]
    if not (isinstance(free, list) and len(free) == 2 and all(_num(x) for x in free)):
        return f"free = {short(free)}"
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
                            " re-check DESIGN §7.1, then update INVARIANTS_FOR_VERSION and deploy/settings.json")
            out.append((f"status.{field}", group, problem))
    return out


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
    """Plan ratios == option values; no fixed per-request price overrides them."""
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
    """GET /api/pricing as root: effective group ratios and which group serves which model."""
    out = []
    usable = set(pricing.get("usable_group") or {})
    out.append(("usable_group", None if usable == USABLE_GROUPS else f"usable groups {sorted(usable)}"))
    ratio = pricing.get("group_ratio") or {}
    out.append(("group_ratio", None if close(ratio.get("default"), MARKUP) and close(ratio.get("free"), 0)
                else f"effective default = {ratio.get('default')!r}, free = {ratio.get('free')!r}"))
    if plan is None:
        return out
    groups = {}
    for item in pricing.get("data") or []:
        groups[item.get("model_name")] = set(item.get("enable_groups") or [])
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
        if plan is not None:
            spec = load_json(a.spec)
            rows += [("channels", n, p) for n, p in check_channel_spec(spec, plan)]
    else:
        c = client_from_args(a)
        status = c.req("GET", "/api/status", auth=False)
        rows += [("status", n, p) for n, _, p in check_status(status.get("data") or {})]
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
    where = "offline (settings files only)" if a.offline else c.base
    print(f"verify-settings {where}: {len(rows)} checks, {len(failed)} failed ("
          + ", ".join(f"{s} {n - f}/{n}" for s, (n, f) in by.items()) + ")")
    return 1 if failed else 0


# ----------------------------------------------------------- commands

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
        print(f"{k} = {v!r}")
    return 0


def cmd_apply_settings(a):
    raw = {}
    for p in a.files:
        raw.update(load_json(p))
    if "_snapshot" in raw and not a.only:
        raise Fatal("restoring a snapshot needs --only KEY[,KEY...]: never write back every option")
    settings = load_layers(a.files)
    if a.only:
        only = [k for part in a.only for k in part.split(",") if k]
        unknown = [k for k in only if k not in settings]
        if unknown:
            raise Fatal(f"--only: {unknown} not in {a.files}")
        settings = {k: settings[k] for k in only}
    bad = sorted(set(settings) & SYNTHETIC_KEYS)
    if bad:
        raise Fatal(f"{bad} are computed by new-api and cannot be set")
    c = client_from_args(a)
    if a.snapshot:
        take_snapshot(c, a.snapshot)
    failed = 0
    for key, value in settings.items():
        d = c.req("PUT", "/api/option/", {"key": key, "value": wire(value)}, allow_error=True)
        ok = bool(d.get("success"))
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {key} = {short(encode(value))}" + ("" if ok else f"  ({d.get('message')})"))
    print(f"apply-settings: {len(settings) - failed}/{len(settings)} applied; run verify-settings next")
    return 1 if failed else 0


def cmd_apply_pricing(a):
    """Merge the plan into ModelRatio / CompletionRatio / CacheRatio
    (ratio 1 = US$2 per 1M tokens; model ratios equal LiteLLM cost, the
    markup lives in GroupRatio) and drop fixed prices for plan models."""
    plan = load_json(a.plan)
    c = client_from_args(a)
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


def wanted_channels(spec, plan, label, key_alias, with_internal):
    out = []
    for ch in spec["channels"]:
        if ch.get("optional") and not with_internal:
            continue
        override = ch.get("param_override")
        out.append({
            "name": ch["name"], "type": spec["type"], "base_url": spec["base_url"], "group": ch["group"],
            "models": ",".join(plan[ch["models"]]), "test_model": ch["test_model"],
            "param_override": json.dumps(override, ensure_ascii=False) if override else "",
            "auto_ban": ch.get("auto_ban", 1), "priority": ch.get("priority", 0), "weight": ch.get("weight", 0),
            "remark": f"{label}; LiteLLM key alias {key_alias}",
        })
    return out


def check_channel_spec(spec, plan):
    """Offline: the spec's model lists exist, test models are served, groups are the PaaS ones."""
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
    leaked = sorted(set(plan["local"]) & set().union(*(m for g, m in groups.items() if g != "free")))
    out.append(("local models only in free", None if not leaked else f"{leaked} also in a paid group"))
    return out


def _norm_set(s):
    return sorted(x.strip() for x in (s or "").split(",") if x.strip())


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
                diff.append(f"models +{sorted(ws - hs)[:5]} -{sorted(hs - ws)[:5]}")
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


def read_upstream_key(a):
    if a.key_file and a.key_env:
        raise Fatal("pass one of --key-file / --key-env")
    if a.key_file == "-":
        key = sys.stdin.read().strip()
    elif a.key_file:
        _require_private(os.path.expanduser(a.key_file), "key")
        with open(os.path.expanduser(a.key_file)) as f:
            key = f.read().strip()
    elif a.key_env:
        key = os.environ.get(a.key_env, "").strip()
    else:
        raise Fatal("a channel is missing: pass the LiteLLM key with --key-file FILE (0600, or - for stdin) or --key-env VAR")
    if not UPSTREAM_KEY_RE.fullmatch(key):
        raise Fatal("the upstream key does not look like a LiteLLM key (sk-...)")
    return key


def cmd_apply_channels(a):
    spec, plan = load_json(a.spec), load_json(a.plan)
    problems = [f"{n}: {p}" for n, p in check_channel_spec(spec, plan) if p]
    if problems:
        raise Fatal("channel spec does not match the plan: " + "; ".join(problems))
    c = client_from_args(a)
    have = {}
    for ch in list_channels(c):
        have.setdefault(ch.get("name"), []).append(ch)
    key, failed = None, 0
    for want in wanted_channels(spec, plan, a.label, a.key_alias, a.with_internal):
        found = have.get(want["name"], [])
        tag = f"{want['name']} (group {want['group']}, {len(_norm_set(want['models']))} models)"
        if len(found) > 1:
            print(f"FAIL {tag}: {len(found)} channels with this name; fix by hand")
            failed += 1
            continue
        if not found:
            if a.dry_run:
                print(f"would create {tag}")
                continue
            if key is None:
                key = read_upstream_key(a)
            channel = dict(want, key=key)
            if not channel["param_override"]:
                del channel["param_override"]
            d = c.req("POST", "/api/channel/", {"mode": "single", "channel": channel}, allow_error=True)
            del channel
            ok = bool(d.get("success"))
            failed += not ok
            print(f"{'created' if ok else 'FAIL create'} {tag}" + ("" if ok else f": {d.get('message')}"))
            continue
        ch = found[0]
        note = "" if ch.get("status") == 1 else f" [status {ch.get('status')}: disabled]"
        diff = channel_diff(want, ch)
        if not diff:
            print(f"ok {tag} id={ch.get('id')}{note}")
        elif a.update and not a.dry_run:
            body = {"id": ch["id"], "name": want["name"], **{f: want[f] for f in CHANNEL_FIELDS}}
            d = c.req("PUT", "/api/channel/", body, allow_error=True)
            ok = bool(d.get("success"))
            failed += not ok
            print(f"{'updated' if ok else 'FAIL update'} {tag} id={ch['id']}: {'; '.join(diff)}"
                  + ("" if ok else f" ({d.get('message')})"))
        else:
            print(f"DRIFT {tag} id={ch.get('id')}{note}: {'; '.join(diff)}"
                  + (" (would update)" if a.update else " (--update to fix; the key is kept)"))
            failed += not a.update
    return 1 if failed else 0


def cmd_call(a):
    c = client_from_args(a)
    body = json.loads(a.json) if a.json else None
    print(json.dumps(c.req(a.method, a.path, body), ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="new-api origin (port-forward URL)")
    ap.add_argument("--token-file", help="0600 file with a root PAT (nap_...); no password login")
    ap.add_argument("--creds", default=DEFAULT_CREDS, help="url=/username=/password= file for password login")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get-options", help="print options, or --snapshot them to a 0600 file")
    p.add_argument("--snapshot", metavar="OUT")
    p.add_argument("keys", nargs="*")
    p.set_defaults(fn=cmd_get_options)

    p = sub.add_parser("verify-settings", help="check settings files, PaaS invariants, status, pricing")
    p.add_argument("files", nargs="+")
    p.add_argument("--offline", action="store_true", help="check the files against the invariants only (CI)")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--no-plan", action="store_true", help="skip the pricing and channel checks")
    p.add_argument("--spec", default=DEFAULT_SPEC)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_verify_settings)

    p = sub.add_parser("apply-settings", help="PUT every key of the settings files")
    p.add_argument("files", nargs="+")
    p.add_argument("--only", action="append", metavar="KEY[,KEY]")
    p.add_argument("--snapshot", metavar="OUT", help="snapshot all options to OUT first")
    p.set_defaults(fn=cmd_apply_settings)

    p = sub.add_parser("apply-pricing", help="merge deploy/pricing/pricing-plan.json into the ratio options")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--backup-dir", default=os.path.expanduser("~/.config/newapi-backups"))
    p.set_defaults(fn=cmd_apply_pricing)

    p = sub.add_parser("apply-channels", help="create the channels of deploy/pricing/channels.json")
    p.add_argument("--label", required=True, help="stg or prod (goes into the remark)")
    p.add_argument("--key-alias", required=True, help="LiteLLM key alias (goes into the remark)")
    p.add_argument("--key-file", help="0600 file with the LiteLLM key, or - for stdin")
    p.add_argument("--key-env", help="environment variable holding the LiteLLM key")
    p.add_argument("--spec", default=DEFAULT_SPEC)
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--with-internal", action="store_true", help="also the ChatGPT-subscription test channel")
    p.add_argument("--update", action="store_true", help="PUT drifted channels back to the spec (key unchanged)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_apply_channels)

    p = sub.add_parser("call", help="one raw API call")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("json", nargs="?")
    p.set_defaults(fn=cmd_call)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except Fatal as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # missing settings, plan, creds or token file
        print(f"error: {e.filename or ''}: {e.strerror or e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
