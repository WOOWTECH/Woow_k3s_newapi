"""New API content as data: read-only export, Go-exact JSON, a site transform, fingerprints.

Used by scripts/apporo_sync.py (the Apporo replay and daily sync). Python 3
standard library only. Nothing here writes to a server: reads go through
`kubectl exec ... psql` with `SET default_transaction_read_only = on` as the
first statement, an explicit column list and no secret column.

What is read (and nothing else):
  - options: first the key names only; then the values of the keys that pass
    the secret-name check (option_key_class). A secret-looking key is never
    read unless the profile whitelists it, and a value that looks like a
    credential is dropped after reading (never printed or stored).
  - channels: every column except `key` and the counters (used_quota,
    balance, test_time, response_time, created_time, balance_updated_time).
    For the *target* only, sha256(key) truncated to 16 hex characters, to
    compare with the Apporo Secret without reading the key. A field that may
    hold a credential (a credential-like member name in header_override,
    param_override, setting ..., an auth scheme, a long hex or random-looking
    token, URL user info or a key-like query parameter) is replaced by
    "[redacted]" as soon as it is read: such a channel is never copied,
    printed or exported.
  - abilities, setups.version and channels_id_seq.
  Never: users, tokens, user_access_tokens, two_fas, passkeys, sessions,
  logs, quota_data, audit_logs or any other customer or identity table.

setups.version is only shown (snapshot "setup_version"): rc.44 writes that row
once, at /api/setup, and never updates it, so after an upgrade it still names
the first version. The version a site runs, which selects its built-in prices
(SEED_IMAGES, TOOL_PRICE_SEEDS, VIOLATION_FEE_DEFAULTS), is read by
apporo_sync.running_version from the image digest its Deployments pin.

The read-only SQL is one of the tool's own two queries, byte for byte
(check_read_only_sql; the templates are pinned by sha256 in
SQL_TEMPLATE_SHA256): the only variable part is the list of option key names,
each a literal matching OPTION_KEY_RE. Any other statement is refused.

rc.44 facts this module depends on (docs/APPORO-SYNC.md):
  - The 11 pricing options are stored as Go json.Marshal output
    (map[string]float64 / map[string]any, keys sorted, compact).
    go_json() reproduces those bytes; every source map must round-trip
    through it, or the transform refuses (fail closed).
  - Every other option is stored as the raw string sent to PUT /api/option/.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import urllib.parse

# The New API version the storage and API rules of this module were verified on
# (profile.newapi_version names it). The versions a site may RUN are the keys of
# SEED_IMAGES: each one was re-checked against those rules (see SEED_IMAGES).
NEWAPI_VERSION = "v1.0.0-rc.44"

# rc.44 model/model_pricing_config.go modelPricingOptionKeys: written in one
# transaction and re-marshaled with Go JSON on every pricing write.
PRICING_KEYS = (
    "AudioCompletionRatio", "AudioRatio", "CacheRatio", "CompletionRatio",
    "CreateCacheRatio", "ImageRatio", "ModelPrice", "ModelRatio",
    "billing_setting.billing_expr", "billing_setting.billing_mode", "billing_setting.plugin_billing_expr",
)
RATIO_MAPS = ("ModelRatio", "ModelPrice", "CompletionRatio", "CacheRatio", "CreateCacheRatio",
              "ImageRatio", "AudioRatio", "AudioCompletionRatio")
BILLING_MAPS = ("billing_setting.billing_expr", "billing_setting.billing_mode", "billing_setting.plugin_billing_expr")
# Raw-stored JSON maps whose entries are prices per group.
GROUP_PRICE_MAPS = ("GroupRatio", "GroupGroupRatio", "TopupGroupRatio")
# Write order on a fresh target (rc.44 creates the other pricing rows from its
# built-in defaults on the first pricing write; later writes replace them).
PRICING_WRITE_ORDER = ("ModelRatio", "CompletionRatio", "CacheRatio", "ModelPrice", "CreateCacheRatio",
                       "ImageRatio", "AudioRatio", "AudioCompletionRatio") + BILLING_MAPS
# rc.44 defaults used when a model has no entry (setting/ratio_setting).
ABSENT_DEFAULTS = {"CacheRatio": 1.0, "CreateCacheRatio": 1.25, "ImageRatio": 1.0,
                   "AudioRatio": 1.0, "AudioCompletionRatio": 1.0}

# Option keys that belong to one instance and are never copied or compared.
SERVER_KEYS = ("ServerAddress",)
DERIVED_KEYS = ("passkey.origins", "passkey.legacy_rp_ids", "passkey.rp_id")  # rc.44 derives them from ServerAddress
INSTANCE_KEYS = ("LegacyAccessTokenRetireAt",)                                # server-managed, PUT refused
# rc.44 start-up exports and computed entries: never written (newapi_admin.NEVER_WRITE).
NEVER_COPY = ("group_ratio_setting.group_ratio", "group_ratio_setting.group_group_ratio", "CompletionRatioMeta")

OPTION_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# A key that may hold a credential (anywhere in the name, case-insensitive).
# rc.44 examples: SMTPToken, GitHubClientSecret, TurnstileSecretKey,
# StripeApiSecret, StripeWebhookSecret, EpayKey, WorkerValidKey,
# TelegramBotToken, WeChatServerToken, CreemApiKey, WaffoPrivateKey,
# oidc.client_secret, discord.client_secret.
SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|private|credential|cert|apikey|api_key|webhook)"
                           r"|key$", re.IGNORECASE)
# Values that look like credentials (newapi_admin.SECRET_VALUE_RE plus PEM and URL user info; the
# sk- length bound equals UPSTREAM_KEY_RE, so every key the tool accepts is also redacted).
SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|nap_[A-Za-z0-9_-]{16,})"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
UPSTREAM_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,256}")

# Channel columns read from the database, as (API field, SQL expression).
# Never `key`; never the counters. `openai_organization` is the API name of
# the column open_ai_organization.
CHANNEL_COLUMNS = (
    ("id", "id"), ("type", "type"), ("name", "name"), ("status", "status"),
    ("base_url", "base_url"), ("group", '"group"'), ("models", "models"),
    ("model_mapping", "model_mapping"), ("status_code_mapping", "status_code_mapping"),
    ("priority", "priority"), ("weight", "weight"), ("auto_ban", "auto_ban"), ("tag", "tag"),
    ("test_model", "test_model"), ("openai_organization", "open_ai_organization"),
    ("other", "other"), ("other_info", "other_info"), ("setting", "setting"),
    ("param_override", "param_override"), ("header_override", "header_override"),
    ("remark", "remark"), ("channel_info", "channel_info::text"), ("settings", "settings"),
)
# Fields the transform copies from a source channel to its target channel.
CHANNEL_COPY_FIELDS = (
    "type", "name", "base_url", "group", "models", "model_mapping", "status_code_mapping",
    "priority", "weight", "auto_ban", "tag", "test_model", "openai_organization", "other",
    "other_info", "setting", "param_override", "header_override", "remark", "settings",
)
# Channel fields whose values may be printed (alerts, plan, verify). Every other
# field is shown only as a sha256 prefix and a length: header_override,
# param_override, setting, settings, other, base_url, remark ... can carry a
# credential that no value pattern recognises.
CHANNEL_PRINTABLE_FIELDS = frozenset(("name", "models", "group", "test_model", "type", "status", "priority",
                                      "weight", "auto_ban", "tag"))
# Channel fields that hold JSON (headers, request overrides, settings): their member
# names are checked, not only their values.
CHANNEL_JSON_FIELDS = ("header_override", "param_override", "setting", "settings", "other", "other_info",
                       "model_mapping", "status_code_mapping")
# A member name (header or parameter) that carries a credential. `max_tokens`,
# `budget_tokens` and the like do not match (a trailing `token` must stand alone).
CHANNEL_SECRET_NAME_RE = re.compile(
    r"(authorization|cookie|secret|password|passwd|credential|private[_-]?key|api[_-]?key|access[_-]?key|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|id[_-]?token|bearer|signature|"
    r"^cf-access-|(^|[_.-])token$|(^|[_.-])key$|^key$|^token$)", re.IGNORECASE)
# Values that carry a credential without a recognisable prefix (a backstop: member
# names are the main signal). A whole value that is an HTTP auth scheme plus a
# credential; a long hex string (e.g. a Cloudflare Access client secret); a long
# run without separators that mixes upper case, lower case and digits (random
# tokens; model names and URL paths are split by - . / and are rarely mixed case).
AUTH_SCHEME_VALUE_RE = re.compile(r"(?i)^\s*(?:bearer|basic|token|digest)\s+[A-Za-z0-9._~+/=-]{8,}\s*$")
OPAQUE_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9+_=])(?=[A-Za-z0-9+_]*[A-Z])(?=[A-Za-z0-9+_]*[a-z])(?=[A-Za-z0-9+_]*[0-9])"
    r"[A-Za-z0-9+_]{24,}={0,2}(?![A-Za-z0-9+_=])")
# Fields that never hold free text a value pattern could misread (model lists, names).
_CHANNEL_NAME_FIELDS = frozenset(("name", "models", "group", "test_model", "tag", "type", "priority", "weight",
                                  "auto_ban", "status"))
ABILITY_COLUMNS = ("group", "model", "channel_id", "enabled", "priority", "weight", "tag")
# Tables an export must never touch (unit-tested against every SQL string).
FORBIDDEN_TABLES = (
    "users", "tokens", "user_access_tokens", "two_fas", "two_fa_backup_codes", "passkey_credentials",
    "user_sessions", "login_encryption_keys", "auth_flows", "external_identity_claims", "user_oauth_bindings",
    "logs", "quota_data", "audit_logs", "top_ups", "redemptions", "tasks", "midjourneys", "checkins",
    "subscription_orders", "user_subscriptions", "subscription_pre_consume_records", "perf_metrics",
    "system_tasks", "system_instances",
)


class ContentError(Exception):
    """A read, a transform or a check could not be done safely (exit 2)."""


# ------------------------------------------------------------------ Go JSON

def go_float(value):
    """encoding/json's float64 text (ES6 number formatting, Go cutoffs)."""
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        raise ContentError("non-finite number")
    if f == 0:
        return "-0" if math.copysign(1.0, f) < 0 else "0"
    neg = f < 0
    mantissa, _, exp = repr(abs(f)).lower().partition("e")
    whole, _, frac = mantissa.partition(".")
    raw = whole + frac
    stripped = raw.lstrip("0")
    point = len(whole) + (int(exp) if exp else 0) - (len(raw) - len(stripped))
    digits = stripped.rstrip("0") or "0"
    if abs(f) < 1e-6 or abs(f) >= 1e21:
        e10 = point - 1
        text = digits[0] + ("." + digits[1:] if len(digits) > 1 else "") + "e" + ("-" if e10 < 0 else "+") + "%02d" % abs(e10)
        if text[-4] == "e" and text[-3] == "-" and text[-2] == "0":  # e-07 -> e-7
            text = text[:-2] + text[-1]
    elif point <= 0:
        text = "0." + "0" * -point + digits
    elif point >= len(digits):
        text = digits + "0" * (point - len(digits))
    else:
        text = digits[:point] + "." + digits[point:]
    return ("-" if neg else "") + text


_GO_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def go_string(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch in _GO_ESCAPES:
            out.append(_GO_ESCAPES[ch])
        elif o < 0x20 or ch in "<>&" or o in (0x2028, 0x2029):
            out.append("\\u%04x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def go_json(value):
    """json.Marshal of what Go decodes into map[string]any (numbers are float64)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return go_float(value)
    if isinstance(value, str):
        return go_string(value)
    if isinstance(value, dict):
        return "{" + ",".join(go_string(k) + ":" + go_json(value[k])
                              for k in sorted(value, key=lambda k: k.encode("utf-8"))) + "}"
    if isinstance(value, list):
        return "[" + ",".join(go_json(v) for v in value) + "]"
    raise ContentError(f"unsupported JSON value {type(value).__name__}")


def parse_object(text, what):
    """A JSON object; duplicate members and NaN/Infinity are errors."""
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ContentError(f"{what}: duplicate member {k!r}")
            out[k] = v
        return out

    def no_constant(name):
        raise ContentError(f"{what}: {name} is not JSON")
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=no_constant)
    except ValueError as e:
        raise ContentError(f"{what}: not JSON ({e.__class__.__name__})") from None
    if not isinstance(value, dict):
        raise ContentError(f"{what}: not a JSON object")
    return value


def canonical_map(text, key):
    """Parse a stored pricing map and prove the server would store the same bytes."""
    value = parse_object(text, key)
    if go_json(value) != text:
        raise ContentError(f"{key}: stored text is not Go-canonical JSON; refusing to transform it")
    return value


def divide_map(text, key, divisor):
    """{model: value / divisor} in float64, serialized as rc.44 stores it."""
    value = canonical_map(text, key)
    out = {}
    for name, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
            raise ContentError(f"{key}[{name}]: not a non-negative number")
        out[name] = float(number) / divisor
    return go_json(out)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value):
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


# ------------------------------------------------------------ option keys

def option_key_class(key, profile=None):
    """'copy', 'server', 'derived', 'instance', 'never', 'secret' or 'invalid'."""
    if not isinstance(key, str) or not OPTION_KEY_RE.fullmatch(key):
        return "invalid"
    if key in SERVER_KEYS:
        return "server"
    if key in DERIVED_KEYS:
        return "derived"
    if key in INSTANCE_KEYS:
        return "instance"
    if key in NEVER_COPY:
        return "never"
    allowed = set((profile or {}).get("secret_like_allowed", ()))
    if SECRET_KEY_RE.search(key) and key not in allowed:
        return "secret"
    return "copy"


# ------------------------------------------------------------------- SQL

READ_ONLY_PREAMBLE = "SET default_transaction_read_only = on;\n"


def sql_literal(text):
    if "\x00" in text:
        raise ContentError("NUL in SQL literal")
    return "'" + text.replace("'", "''") + "'"


# The tool sends exactly two read-only queries, and the guard accepts nothing
# else: the text must equal one of the templates below byte for byte, and the
# only part that may vary is the list of option keys in the content query
# (`WHERE key IN (...)`), each a plain literal of an option key name
# (OPTION_KEY_RE: letters, digits and _.:-; no quote, backslash or space can
# appear). So `key` is read only where the templates read it: the options
# table's key column, and, for the target only, sha256() and length() of
# channels.key (never the key itself). The templates themselves are pinned
# by sha256 (SQL_TEMPLATE_SHA256): an edit of the SQL, of CHANNEL_COLUMNS or
# of the ability columns refuses every read until the new text has been
# reviewed and its pin updated.
_KEY_LIST_RE = re.compile(r"'[A-Za-z0-9_.:-]{1,128}'(?:,'[A-Za-z0-9_.:-]{1,128}')*|NULL")
_KEYS_SLOT = "\x00"          # where the key list goes; NUL never appears in a statement the guard accepts
SQL_TEMPLATE_SHA256 = {
    "option-keys": "96fa314b47ac6e74fcf47ef25dee92be2ffc54fe78a7441f1c7403e7554141e2",
    "content": "6b5a9a047fdd620838ec58f34eaad23066d24735e3c8b22a8e403d997e7588ee",
    "content+key-hashes": "9628f4f1202abbf2aa8f984b7c316957e603e956aefca806efe20f3f081df9b3",
}


def _option_keys_text():
    return READ_ONLY_PREAMBLE + (
        "SELECT jsonb_build_object("
        "'version', (SELECT version FROM setups ORDER BY id LIMIT 1), "
        "'keys', (SELECT coalesce(jsonb_agg(key ORDER BY key COLLATE \"C\"), '[]'::jsonb) FROM options)"
        ")::text;")


def _content_text(keys, key_hashes):
    channel_fields = ", ".join(f"'{api}', {col}" for api, col in CHANNEL_COLUMNS)
    if key_hashes:
        channel_fields += ", 'key_sha256_16', left(encode(sha256(convert_to(key, 'UTF8')), 'hex'), 16), 'key_length', length(key)"
    ability_fields = "'group', \"group\", 'model', model, 'channel_id', channel_id, 'enabled', enabled, " \
                     "'priority', priority, 'weight', weight, 'tag', tag"
    return READ_ONLY_PREAMBLE + (
        "SELECT jsonb_build_object("
        "'version', (SELECT version FROM setups ORDER BY id LIMIT 1), "
        "'options', (SELECT coalesce(jsonb_agg(jsonb_build_array(key, value, "
        "encode(sha256(convert_to(value, 'UTF8')), 'hex')) ORDER BY key COLLATE \"C\"), '[]'::jsonb) "
        f"FROM options WHERE key IN ({keys})), "
        f"'channels', (SELECT coalesce(jsonb_agg(jsonb_build_object({channel_fields}) ORDER BY id), '[]'::jsonb) "
        "FROM channels), "
        f"'abilities', (SELECT coalesce(jsonb_agg(jsonb_build_object({ability_fields}) "
        "ORDER BY \"group\" COLLATE \"C\", model COLLATE \"C\", channel_id), '[]'::jsonb) FROM abilities), "
        "'seq', (SELECT jsonb_build_object('last_value', last_value, 'is_called', is_called) FROM channels_id_seq)"
        ")::text;")


def sql_templates():
    """{name: (head, tail)}: the option-keys query has no variable part (tail None); a content
    query is head + key list + tail."""
    out = {"option-keys": (_option_keys_text(), None)}
    for name, key_hashes in (("content", False), ("content+key-hashes", True)):
        head, tail = _content_text(_KEYS_SLOT, key_hashes).split(_KEYS_SLOT)
        out[name] = (head, tail)
    return out


def template_sha256(head, tail):
    return sha256_text(head if tail is None else head + "{KEYS}" + tail)


def _check_key_literals(text, secret_like_allowed):
    """The key list of a content query names no secret-looking option (option_key_class 'secret',
    unless the profile's secret_like_allowed lists it) and no invalid one: their values are never read."""
    if text == "NULL":
        return
    profile = {"secret_like_allowed": list(secret_like_allowed or ())}
    for literal in text.split(","):
        name = literal[1:-1]
        if option_key_class(name, profile) in ("secret", "invalid"):
            raise ContentError(f"the content query asks for the value of {name!r}, a secret-looking option the "
                               "profile does not allow (secret_like_allowed): refused")


def check_read_only_sql(sql, secret_like_allowed=()):
    """Accept only the tool's own two queries (see SQL_TEMPLATE_SHA256), with option-key literals
    as the only variable part, none of them a secret-looking option name (unless secret_like_allowed
    lists it). Everything else is refused, harmless or not."""
    if not isinstance(sql, str) or "\x00" in sql:
        raise ContentError("SQL must be text without NUL")
    templates = sql_templates()
    for name, (head, tail) in templates.items():
        if template_sha256(head, tail) != SQL_TEMPLATE_SHA256[name]:
            raise ContentError(f"the {name} SQL template changed: review it (read-only, no channel key, no "
                               "customer table), then update apporo_newapi_content.SQL_TEMPLATE_SHA256")
    for name, (head, tail) in templates.items():
        if tail is None:
            if sql == head:
                return sql
        elif len(sql) > len(head) + len(tail) and sql.startswith(head) and sql.endswith(tail) and \
                _KEY_LIST_RE.fullmatch(sql[len(head):len(sql) - len(tail)]):
            _check_key_literals(sql[len(head):len(sql) - len(tail)], secret_like_allowed)
            return sql
    raise ContentError("SQL is not one of this tool's two read-only queries (only the list of option key names "
                       "may vary): refused")


def check_pinned_sql(sql, sha256_hex):
    """A fixed SQL text reviewed once (test harnesses): the preamble, one statement, the pinned sha256."""
    if not isinstance(sql, str) or not sql.startswith(READ_ONLY_PREAMBLE):
        raise ContentError("SQL must start with SET default_transaction_read_only = on")
    if sha256_text(sql) != sha256_hex:
        raise ContentError("SQL text differs from its pinned sha256: review it, then update the pin")
    return sql


def sql_option_keys():
    return check_read_only_sql(_option_keys_text())


def sql_content(option_keys, key_hashes=False, secret_like_allowed=()):
    """The content query for these option keys (each a valid option key name, none secret-looking
    unless secret_like_allowed lists it)."""
    keys = ",".join(sql_literal(k) for k in option_keys) or "NULL"
    return check_read_only_sql(_content_text(keys, key_hashes), secret_like_allowed)


class Psql:
    """psql in a Postgres pod through `kubectl exec -i` (stdin: one read-only SELECT)."""

    def __init__(self, kubectl, namespace, pod="newapi-postgres-0", container="postgres", user="newapi",
                 db="newapi", run=subprocess.run, timeout=120):
        self.kubectl, self.namespace, self.pod = list(kubectl), namespace, pod
        self.container, self.user, self.db, self.run, self.timeout = container, user, db, run, timeout

    def argv(self):
        return self.kubectl + ["-n", self.namespace, "exec", "-i", self.pod, "-c", self.container, "--",
                               "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                               "-U", self.user, "-d", self.db, "-f", "-"]

    def query_json(self, sql, secret_like_allowed=()):
        check_read_only_sql(sql, secret_like_allowed)
        try:
            r = self.run(self.argv(), input=sql.encode("utf-8"), capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ContentError(f"{self.namespace}/{self.pod}: psql did not run ({e.__class__.__name__})") from None
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise ContentError(f"{self.namespace}/{self.pod}: psql exit {r.returncode}: "
                               + (err[-1][:200] if err else "no message"))
        lines = [ln for ln in (r.stdout or b"").decode("utf-8").splitlines() if ln.strip() and ln.strip() != "SET"]
        if len(lines) != 1:
            raise ContentError(f"{self.namespace}/{self.pod}: expected one JSON row, got {len(lines)} lines")
        try:
            return json.loads(lines[0])
        except ValueError:
            raise ContentError(f"{self.namespace}/{self.pod}: the answer is not JSON") from None


def read_snapshot(psql, profile=None, key_hashes=False):
    """One instance's content (two read-only queries). Secret-looking keys are not read."""
    head = psql.query_json(sql_option_keys())
    keys = head.get("keys") or []
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ContentError("invalid option key list")
    readable, skipped = [], {}
    for k in keys:
        cls = option_key_class(k, profile)
        if cls in ("secret", "invalid"):
            skipped[k if cls == "secret" else repr(k)[:80]] = cls
        else:
            readable.append(k)
    allowed = tuple((profile or {}).get("secret_like_allowed", ()))
    body = psql.query_json(sql_content(readable, key_hashes, allowed), allowed)
    options, hashes = {}, {}
    for row in body.get("options") or []:
        if not (isinstance(row, list) and len(row) == 3 and all(isinstance(x, str) for x in row)):
            raise ContentError("invalid option row")
        k, v, h = row
        if k in options:
            raise ContentError(f"options: duplicate rows for {k}")
        if sha256_text(v) != h:
            raise ContentError(f"options: {k} changed in transport (sha256 mismatch)")
        if SECRET_VALUE_RE.search(v):
            skipped[k] = "secret-value"
            continue
        options[k], hashes[k] = v, h
    channels, unsafe = {}, {}
    for ch in body.get("channels") or []:
        if not isinstance(ch, dict) or type(ch.get("id")) is not int or ch["id"] < 1:
            raise ContentError("invalid channel row")
        if "key" in ch:
            raise ContentError("a channel key reached the export")
        bad = channel_unsafe_fields(ch)
        if bad:
            # A credential inside base_url, header_override, ...: never copied, printed or exported.
            unsafe[ch["id"]] = bad
            ch = {f: ("[redacted]" if f in bad else v) for f, v in ch.items()}
        channels[ch["id"]] = ch
    abilities = body.get("abilities") or []
    for a in abilities:
        if not isinstance(a, dict) or set(a) != set(ABILITY_COLUMNS):
            raise ContentError("invalid ability row")
    # setups.version: shown only (rc.44 never updates it after /api/setup); apporo_sync adds running_version.
    return {"setup_version": body.get("version") or head.get("version"), "options": options, "option_sha256": hashes,
            "skipped_keys": skipped, "channels": channels, "unsafe_channels": unsafe, "abilities": abilities,
            "seq": body.get("seq") or {}}


# --------------------------------------------------------------- profile

def load_profile(path):
    with open(path) as f:
        p = json.load(f)
    validate_profile(p)
    return p


def validate_profile(p):
    def need(cond, msg):
        if not cond:
            raise ContentError("profile: " + msg)
    need(isinstance(p, dict) and p.get("schema") == 1, "schema must be 1")
    need(p.get("newapi_version") == NEWAPI_VERSION, f"newapi_version must be {NEWAPI_VERSION}")
    for side in ("source", "target"):
        s = p.get(side)
        need(isinstance(s, dict) and isinstance(s.get("namespace"), str) and s["namespace"], f"{side}.namespace")
    tgt = p["target"]
    need(tgt["namespace"] not in ("new-api", "new-api-stg"), "the target can never be WOOW's new-api or new-api-stg")
    need(tgt["namespace"] != p["source"]["namespace"], "source and target are the same namespace")
    need(isinstance(tgt.get("server_address"), str) and tgt["server_address"].startswith("https://"),
         "target.server_address must be an https origin")
    div = p.get("price_divisor")
    need(type(div) in (int, float) and div > 0 and not isinstance(div, bool), "price_divisor must be > 0")
    need(set(p.get("divided_maps", ())) <= set(RATIO_MAPS), "divided_maps must be ratio maps")
    chans = p.get("channels")
    need(isinstance(chans, dict) and chans, "channels must map source ids")
    slots = (p.get("key_secret") or {}).get("slots") or {}
    targets = set()
    for sid, c in chans.items():
        need(sid.isdigit() and int(sid) > 0, f"channel id {sid!r}")
        need(isinstance(c, dict) and c.get("key_slot") in slots, f"channel {sid}: key_slot must be one of {sorted(slots)}")
        tid = c.get("target_id", int(sid))
        need(type(tid) is int and tid > 0 and tid not in targets, f"channel {sid}: target_id")
        targets.add(tid)
        for f in c.get("set", {}):
            need(f in CHANNEL_COPY_FIELDS, f"channel {sid}: cannot set {f}")
    for sid in p.get("excluded_channels", {}):
        need(sid not in chans, f"channel {sid} is both mapped and excluded")
    for k in (p.get("target_options") or {}):
        need(option_key_class(k, p) in ("copy", "server"), f"target_options: {k} cannot be set")
    labels = p.get("group_labels")
    need(labels is None or (isinstance(labels, dict) and all(isinstance(v, str) for v in labels.values())),
         "group_labels must map group -> label")
    # The AI Models Terms promise at least 7 days' notice of a price increase.
    days = p.get("price_increase_notice_days", MIN_NOTICE_DAYS)
    need(type(days) is int and days >= MIN_NOTICE_DAYS,
         f"price_increase_notice_days must be an integer >= {MIN_NOTICE_DAYS} (AI Models Terms, Section 3)")
    # A model taken off keeps its price protection this long; shorter would let a
    # brief delisting skip the notice.
    hist = p.get("served_history_days", DEFAULT_HISTORY_DAYS)
    need(type(hist) is int and MIN_HISTORY_DAYS <= hist <= 3650,
         f"served_history_days must be an integer from {MIN_HISTORY_DAYS} to 3650")
    need(type(p.get("divide_tool_prices", False)) is bool, "divide_tool_prices must be true or false")
    scalars = p.get("divided_scalar_prices", [])
    need(isinstance(scalars, list) and set(scalars) <= set(SCALAR_PRICE_DEFAULTS),
         f"divided_scalar_prices must be a list of {sorted(SCALAR_PRICE_DEFAULTS)}")
    return p


MIN_NOTICE_DAYS = 7
MIN_HISTORY_DAYS = 30
DEFAULT_HISTORY_DAYS = 30

# rc.44 setting/billing_setting/builtin_billing.go: models billed by a built-in
# expression in real US$ per 1M tokens when no billing_mode entry, ModelRatio or
# ModelPrice names them (GetBillingMode). The 30:1 rule never reaches these.
RC44_BUILTIN_EXPR_MODELS = frozenset(("gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-6-astra"))


def _option_object(options, key):
    try:
        value = json.loads(options.get(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def billed_by_expression(options, model):
    """True when rc.44 bills `model` with a billing expression (real US$ prices), not ModelRatio/ModelPrice."""
    mode = _option_object(options, "billing_setting.billing_mode").get(model)
    if mode is not None:
        return mode == "tiered_expr"
    if model in RC44_BUILTIN_EXPR_MODELS:
        return (model not in _option_object(options, "ModelRatio")
                and model not in _option_object(options, "ModelPrice"))
    return False


def billing_entries(options):
    """{billing map: number of entries} for the billing_setting.* maps that are not empty."""
    return {k: len(_option_object(options, k)) for k in BILLING_MAPS if _option_object(options, k)}


# ------------------------------------------------------- tool call prices

# rc.44 setting/operation_setting/tools.go: tool calls (web_search, file_search,
# image_generation, ...) are billed per call at
#   price / 1000 * GroupRatio * QuotaPerUnit   quota   (service/text_quota.go)
# with `price` an absolute amount per 1,000 calls, never a ModelRatio multiple.
# The effective price of a key is WOOW's operator value (option
# tool_price_setting.prices, a JSON object) or else this built-in seed
# (seedHardcodedToolPrices, "US$ or the vendor's list currency per 1K calls").
TOOL_PRICE_KEY = "tool_price_setting.prices"
RC44_TOOL_PRICE_SEED = {
    "web_search": 10.0, "web_search_preview": 10.0, "file_search": 2.5, "google_search": 14.0,
    "image_generation": 150.0,
    "web_search_preview:gpt-4o*": 25.0, "web_search_preview:gpt-4.1*": 25.0,
    "web_search_preview:gpt-4o-mini*": 25.0, "web_search_preview:gpt-4.1-mini*": 25.0,
    "google_search:gemini-2.5*": 0.0, "google_search:gemini-2.0*": 0.0, "google_search:gemini-1.5*": 0.0,
    "google_search_grounded_prompt:gemini-2.5*": 35.0, "google_search_grounded_prompt:gemini-2.0*": 35.0,
    "google_search_grounded_prompt:gemini-1.5*": 35.0,
    "search_std": 10.0, "search_pro": 30.0, "search_pro_sogou": 50.0, "search_pro_quark": 50.0,
    "search_strategy_turbo": 3.0, "search_strategy_max": 4.0, "search_strategy_agent": 4.0,
    "search_strategy_agent_max": 4.0, "bing_web_search": 14.0, "web_search:grok*": 5.0,
    "x_search_posts": 5.0, "x_search_profiles": 10.0,
}
# The New API versions this tool supports: the image each one runs as (the digest
# chart/values.yaml newapi.image pins) and its built-in prices (the tool price seed
# here, the violation fee default in VIOLATION_FEE_DEFAULTS). A site's version is
# the one it RUNS, read from the image digest its Deployments pin
# (apporo_sync.running_version); setups.version is not it (rc.44 writes that row
# once, at /api/setup). A new version may change these prices or any rc.44 rule
# this module relies on: re-check them (docs/APPORO-SYNC.md 〈映像〉 and 〈限制〉;
# tests/apporo_sync/rc44_price_tables.py <source> <version>), then add the version
# to all three tables. A site running an image that is not here stops the sync.
SEED_IMAGES = {"v1.0.0-rc.44": "sha256:67bf271602177ff31bbb4b34f20d5f33003d3108514bd23412cff66498c05a84"}
TOOL_PRICE_SEEDS = {"v1.0.0-rc.44": RC44_TOOL_PRICE_SEED}
# sha256 of go_json(RC44_TOOL_PRICE_SEED): an edit of the table must be a deliberate one.
RC44_TOOL_PRICE_SEED_SHA256 = "69dc49fddbea1f79c82a85b62043a46191ff9028f26e187b6956d378579a8493"


def version_of_digest(digest):
    """The supported New API version whose image has this digest, or None."""
    found = [version for version, d in SEED_IMAGES.items() if d == digest]
    return found[0] if len(found) == 1 else None


def supported_version(version):
    """True when every built-in table of this version is known (SEED_IMAGES and the price tables)."""
    return version in SEED_IMAGES and version in TOOL_PRICE_SEEDS and version in VIOLATION_FEE_DEFAULTS


class _Pairs(list):
    """A JSON object as its member list (keeps duplicate members, like the raw text)."""


def _no_constant(name):
    raise ValueError(name)


def tool_price_overrides(text):
    """rc.44 LoadToolPricesFromJSONString(text): ({name: price} that rc.44 loads, [names it ignores]).
    No row, or text that is not one JSON object, loads nothing (the seed applies everywhere); an
    entry that is not a finite number >= 0 is ignored on its own; a repeated member: the last wins."""
    if text is None:
        return {}, []
    try:
        value = json.loads(text, object_pairs_hook=_Pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return {}, ["(the value is not JSON)"]
    if not isinstance(value, _Pairs):
        return {}, ["(the value is not a JSON object)"]
    raw = {}
    for name, v in value:
        raw[name] = v
    prices, ignored = {}, []
    for name, v in raw.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            ignored.append(name)
            continue
        try:
            f = float(v)
        except OverflowError:
            ignored.append(name)
            continue
        if math.isnan(f) or math.isinf(f) or f < 0:
            ignored.append(name)
            continue
        prices[name] = f
    return prices, sorted(ignored)


def tool_price_seed(version):
    seed = TOOL_PRICE_SEEDS.get(version)
    if seed is None:
        raise ContentError(f"no built-in tool price table for New API {version!r}: read its "
                           "setting/operation_setting/tools.go and add it to TOOL_PRICE_SEEDS")
    return seed


def effective_tool_prices(version, text):
    """(seed + the operator prices rc.44 loads, ignored entry names): every key rc.44 can bill."""
    merged = dict(tool_price_seed(version))
    over, ignored = tool_price_overrides(text)
    merged.update(over)
    return merged, ignored


def tool_price_index(merged):
    """rc.44 RebuildToolPriceIndex: {tool: (default (price, key) or None, [(prefix bytes, price, key)])}
    with the prefixes longest first, then bytewise. Also returns the keys that share a prefix
    (rc.44's order between them is not defined)."""
    out, seen, clashes = {}, {}, []
    for key, price in merged.items():
        tool, colon, part = key.partition(":")
        entry = out.setdefault(tool, [None, []])
        if not colon:
            entry[0] = (price, key)
            continue
        prefix = part[:-1] if part.endswith("*") else part
        pb = prefix.encode("utf-8")
        other = seen.get((tool, pb))
        if other is not None and merged[other] != price:
            clashes.append(sorted((other, key)))
        seen[(tool, pb)] = key
        entry[1].append((pb, price, key))
    for entry in out.values():
        entry[1].sort(key=lambda e: (-len(e[0]), e[0], e[2]))
    return out, clashes


def tool_price(index, tool, model):
    """rc.44 GetToolPriceForModel: (price per 1,000 calls, the key that set it, or None)."""
    entry = index.get(tool)
    if entry is None:
        return 0.0, None
    if model:
        mb = model.encode("utf-8")
        for pb, price, key in entry[1]:
            if mb.startswith(pb):
                return price, key
    if entry[0] is not None:
        return entry[0]
    return 0.0, None


def tool_probes(*indexes):
    """Model names that reach every distinct price of these indexes: each key's prefix (it is
    governed by the longest key it starts with, in every index) and "" (the defaults)."""
    names = {""}
    for index in indexes:
        for _, (_, prefixes) in index.items():
            names.update(pb.decode("utf-8") for pb, _, _ in prefixes)
    return names


def divided_tool_prices(version, text, divisor):
    """(Apporo's tool_price_setting.prices text, ignored WOOW entries, prefix clashes): every key
    WOOW's rc.44 can bill (seed + WOOW's operator prices) at price / divisor, as rc.44 itself
    serializes this map (encoding/json of map[string]float64: keys sorted, Go floats). Writing
    every key, not only WOOW's overrides, is what makes each lookup WOOW's / divisor."""
    merged, ignored = effective_tool_prices(version, text)
    out = {}
    for key, price in merged.items():
        q = price / divisor
        if price > 0 and not q > 0:
            raise ContentError(f"{TOOL_PRICE_KEY}[{key}]: {price} / {divisor} underflows to 0")
        out[key] = q
    _, clashes = tool_price_index(merged)
    return go_json(out), ignored, clashes


# ------------------------------------------------- the violation fee (rc.44)

# rc.44 setting/model_setting/grok.go + service/violation_fee.go: an upstream error
# carrying a content-safety marker ("Content violates usage guidelines", "Failed
# check: SAFETY_CHECK_TYPE"), from any channel, is charged
#   amount * QuotaPerUnit * GroupRatio   quota
# with `amount` an absolute US$ value (default 0.05, enabled by default). Stored as
# plain text like rc.44's own export (strconv.FormatFloat 'f', -1).
VIOLATION_FEE_KEY = "grok.violation_deduction_amount"
VIOLATION_SWITCH_KEY = "grok.violation_deduction_enabled"
RC44_VIOLATION_FEE = 0.05
# The built-in default of each supported version (see SEED_IMAGES): (enabled, US$ per flagged request).
VIOLATION_FEE_DEFAULTS = {"v1.0.0-rc.44": (True, RC44_VIOLATION_FEE)}
# The absolute-price options the profile may divide (divided_scalar_prices) and their switches.
SCALAR_PRICE_DEFAULTS = {VIOLATION_FEE_KEY: RC44_VIOLATION_FEE}
PRICE_SWITCHES = {VIOLATION_SWITCH_KEY: True}
_GO_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_GO_OTHER_FLOAT_RE = re.compile(r"(?i)[+-]?(?:inf|infinity|nan|0x[0-9a-f_.p+-]*)")


def go_parse_float(text):
    """strconv.ParseFloat for rc.44 config text: (float, None); (None, None) when rc.44 skips the
    row (keeps its default); (None, why) for forms Go accepts that this tool does not model."""
    if _GO_DECIMAL_RE.fullmatch(text):
        try:
            v = float(text)
        except (OverflowError, ValueError):
            return None, None
        return (None, None) if math.isinf(v) or math.isnan(v) else (v, None)   # ErrRange: rc.44 skips it
    if _GO_OTHER_FLOAT_RE.fullmatch(text):
        return None, "infinite, NaN or hexadecimal"
    return None, None


def go_parse_bool(text):
    """strconv.ParseBool (None when rc.44 skips the row)."""
    if text in ("1", "t", "T", "TRUE", "true", "True"):
        return True
    if text in ("0", "f", "F", "FALSE", "false", "False"):
        return False
    return None


def violation_defaults(version):
    """(enabled, US$) that the New API version a site runs uses when its option rows are absent."""
    found = VIOLATION_FEE_DEFAULTS.get(version)
    if found is None:
        raise ContentError(f"no built-in violation fee default for New API {version!r}: read its "
                           "setting/model_setting/grok.go and add it to VIOLATION_FEE_DEFAULTS")
    return found


def scalar_price(options, key, version):
    """(the US$ amount this New API version uses for this option, or None, and why not)."""
    if key != VIOLATION_FEE_KEY:
        raise ContentError(f"{key}: not an absolute price this tool knows")
    default = violation_defaults(version)[1]
    text = options.get(key)
    if text is None:
        return default, None
    v, why = go_parse_float(text)
    if why:
        return None, why
    return (default if v is None else v), None


def violation_fee(options, version):
    """(US$ the New API version charges per flagged request, 0 when switched off, or None, and why not)."""
    on_default = violation_defaults(version)[0]
    text = options.get(VIOLATION_SWITCH_KEY)
    enabled = on_default if text is None else go_parse_bool(text)
    if enabled is None:
        enabled = on_default                               # rc.44 skips an unreadable switch
    amount, why = scalar_price(options, VIOLATION_FEE_KEY, version)
    if amount is None:
        return None, why
    return (amount if enabled and amount > 0 else 0.0), None


def go_format_f(value):
    """strconv.FormatFloat(value, 'f', -1, 64): the shortest digits, never an exponent."""
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        raise ContentError("non-finite number")
    if f == 0:
        return "-0" if math.copysign(1.0, f) < 0 else "0"
    mantissa, _, exp = repr(abs(f)).lower().partition("e")
    whole, _, frac = mantissa.partition(".")
    raw = whole + frac
    stripped = raw.lstrip("0")
    point = len(whole) + (int(exp) if exp else 0) - (len(raw) - len(stripped))
    digits = stripped.rstrip("0") or "0"
    if point <= 0:
        text = "0." + "0" * -point + digits
    elif point >= len(digits):
        text = digits + "0" * (point - len(digits))
    else:
        text = digits[:point] + "." + digits[point:]
    return ("-" if f < 0 else "") + text


# rc.44 setting/operation_setting/tools.go GetGeminiInputAudioPricePerMillionTokens:
# audio input of models whose (billing) name starts with one of these is billed at
# fixed US$ prices per 1M tokens that no option can change.
GEMINI_AUDIO_PREFIXES = ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-robotics-er-1.5")


def fixed_audio_price(model):
    return model.startswith(GEMINI_AUDIO_PREFIXES)


# ------------------------------------------------------------- transform

def transform(src, profile):
    """Desired target content from a source snapshot. Returns (desired, notes).

    desired = {"options": {key: stored string}, "channels": {target id: fields},
               "channel_slots": {target id: key slot}, "source_ids": {target id: source id}}
    notes   = [(kind, subject, text)] for keys or channels left out.
    """
    notes = []
    opts = {}
    divisor = profile["price_divisor"]
    divided = set(profile.get("divided_maps", ()))
    for key, value in src["options"].items():
        cls = option_key_class(key, profile)
        if cls != "copy":
            continue
        if key in PRICING_KEYS:
            canonical_map(value, key)
            if key in divided and divisor != 1:
                value = divide_map(value, key, divisor)
        elif key == "UserUsableGroups" and profile.get("group_labels") is not None:
            groups = parse_object(value, key)
            labels = profile["group_labels"]
            missing = [g for g in groups if g not in labels]
            if missing:
                notes.append(("label-missing", key, f"no target label for group(s) {missing}"))
            value = json.dumps({g: labels.get(g, groups[g]) for g in groups}, ensure_ascii=False)
        opts[key] = value
    if profile.get("divide_tool_prices"):
        # Absolute prices per 1,000 tool calls: WOOW's effective price of every key / divisor.
        # The seed is the one of the version WOOW RUNS (src["running_version"]), never setups.version.
        text, ignored, clashes = divided_tool_prices(src.get("running_version"), src["options"].get(TOOL_PRICE_KEY),
                                                     divisor)
        opts[TOOL_PRICE_KEY] = text
        if ignored:
            notes.append(("tool-price-ignored", TOOL_PRICE_KEY,
                          f"WOOW's rc.44 ignores these entries (not a number >= 0), so Apporo leaves them out: "
                          f"{ignored[:10]}"))
        for pair in clashes:
            notes.append(("tool-price-ambiguous", TOOL_PRICE_KEY,
                          f"keys {pair} name the same model prefix at different prices: rc.44's choice between "
                          "them is not defined, so Apporo may not charge exactly WOOW's / 30; fix it at WOOW"))
    for key in profile.get("divided_scalar_prices", ()):
        amount, why = scalar_price(src["options"], key, src.get("running_version"))
        if amount is None:
            opts.pop(key, None)
            notes.append(("scalar-price-unreadable", key, f"WOOW's value cannot be read ({why}): not copied"))
            continue
        opts[key] = go_format_f(amount / divisor)
    for key, value in (profile.get("target_options") or {}).items():
        opts[key] = value
    opts["ServerAddress"] = profile["target"]["server_address"]
    for key, why in src.get("skipped_keys", {}).items():
        notes.append(("option-excluded", key, why))

    chans, slots, source_ids = {}, {}, {}
    for sid_text, conf in profile["channels"].items():
        sid = int(sid_text)
        tid = conf.get("target_id", sid)
        ch = src["channels"].get(sid)
        if ch is None:
            notes.append(("source-channel-missing", f"channel {sid}", "mapped in the profile but absent at the source"))
            continue
        if sid in (src.get("unsafe_channels") or {}):
            notes.append(("source-channel-unsafe", f"channel {sid}",
                          f"field(s) {src['unsafe_channels'][sid]} look like they hold a credential: not copied"))
            continue
        want = {f: ch.get(f) for f in CHANNEL_COPY_FIELDS}
        want.update(conf.get("set", {}))
        chans[tid], slots[tid], source_ids[tid] = want, conf["key_slot"], sid
    excluded = {int(k) for k in profile.get("excluded_channels", {})}
    for sid in sorted(src["channels"]):
        if str(sid) not in profile["channels"] and sid not in excluded:
            notes.append(("source-channel-new", f"channel {sid}", "not in the profile: not copied"))
    return {"options": opts, "channels": chans, "channel_slots": slots, "source_ids": source_ids}, notes


def expected_abilities(channels, status=None):
    """rc.44 AddAbilities/UpdateAbilities: one row per model x group of each channel."""
    rows = []
    for cid, ch in channels.items():
        enabled = (status or {}).get(cid, 1) == 1
        seen = set()
        for model in (ch.get("models") or "").split(","):
            for group in (ch.get("group") or "").split(","):
                if (group, model) in seen:
                    continue
                seen.add((group, model))
                rows.append({"group": group, "model": model, "channel_id": cid, "enabled": enabled,
                             "priority": ch.get("priority") if ch.get("priority") is not None else 0,
                             "weight": ch.get("weight") if ch.get("weight") is not None else 0,
                             "tag": ch.get("tag")})
    return sorted(rows, key=lambda r: (r["group"].encode(), r["model"].encode(), r["channel_id"]))


# ----------------------------------------------------------- fingerprints

def options_fingerprint(options, exclude=SERVER_KEYS + DERIVED_KEYS + INSTANCE_KEYS):
    """sha256 of key=value lines in byte order; equals the research SQL
    (string_agg(key||'='||value, E'\\n' ORDER BY key)) on rc.44's musl collation."""
    rows = sorted(((k, v) for k, v in options.items() if k not in exclude), key=lambda kv: kv[0].encode())
    return len(rows), sha256_text("\n".join(k + "=" + v for k, v in rows))


def _sql_bool(v):
    return "t" if v else "f"


def abilities_fingerprint(rows, channel_ids=None):
    """sha256 of concat_ws('|', group, model, channel_id, enabled, priority, weight, coalesce(tag,''))."""
    picked = [r for r in rows if channel_ids is None or r["channel_id"] in channel_ids]
    picked.sort(key=lambda r: (r["group"].encode(), r["model"].encode(), r["channel_id"]))
    lines = ["|".join((r["group"], r["model"], str(r["channel_id"]), _sql_bool(r["enabled"]),
                       str(r["priority"] if r["priority"] is not None else ""),
                       str(r["weight"] if r["weight"] is not None else ""), r["tag"] or "")) for r in picked]
    return len(lines), sha256_text("\n".join(lines))


def channel_fingerprint(ch, fields=CHANNEL_COPY_FIELDS):
    """sha256 over the copied fields (null and '' are different)."""
    return digest({f: ch.get(f) for f in fields})


def secret_like(value):
    return bool(isinstance(value, str) and SECRET_VALUE_RE.search(value))


_SECRET_WORD_RE = re.compile(r"(?i)(authorization|cookie|secret|password|passwd|bearer|api[_-]?key|cf-access-|"
                             r"private[_-]?key|access[_-]?token)")
# "Name: value" or "Name=value" lines inside a string (headers written as text).
_HEADER_LINE_RE = re.compile(r"(?m)^\s*([A-Za-z0-9_.-]{1,80})\s*[:=]\s*\S")


def _value_unsafe(text):
    if SECRET_VALUE_RE.search(text) or OPAQUE_SECRET_RE.search(text) or AUTH_SCHEME_VALUE_RE.match(text):
        return True
    return any(CHANNEL_SECRET_NAME_RE.search(m.group(1)) for m in _HEADER_LINE_RE.finditer(text))


class _TooDeep(Exception):
    pass


def _json_names_and_strings(value, names, strings, depth=0):
    if depth > 20:
        raise _TooDeep()
    if isinstance(value, dict):
        for k, v in value.items():
            names.append(str(k))
            _json_names_and_strings(v, names, strings, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _json_names_and_strings(v, names, strings, depth + 1)
    elif isinstance(value, str):
        strings.append(value)


def channel_field_unsafe(field, value):
    """True when a channel field may hold a credential (it is then never copied or printed)."""
    if not isinstance(value, str) or not value:
        return False
    if SECRET_VALUE_RE.search(value):
        return True
    if field in _CHANNEL_NAME_FIELDS:
        return False
    if field in CHANNEL_JSON_FIELDS:
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            names, strings = [], []
            try:
                _json_names_and_strings(parsed, names, strings)
            except _TooDeep:
                return True                     # cannot be checked: treat as unsafe
            if any(CHANNEL_SECRET_NAME_RE.search(n) for n in names):
                return True
            return any(_value_unsafe(s) for s in strings)
        # not JSON (or a bare JSON string/number): any credential-looking word or value makes it unsafe
        return _value_unsafe(value) or bool(_SECRET_WORD_RE.search(value))
    if field == "base_url":
        try:
            u = urllib.parse.urlsplit(value)
        except ValueError:
            return True
        if u.username or u.password:
            return True
        for name, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
            if CHANNEL_SECRET_NAME_RE.search(name) or OPAQUE_SECRET_RE.search(v):
                return True
        return bool(OPAQUE_SECRET_RE.search(u.path))
    return bool(OPAQUE_SECRET_RE.search(value))


def channel_unsafe_fields(ch):
    return sorted(f for f, v in ch.items() if f in CHANNEL_COPY_FIELDS and channel_field_unsafe(f, v))


def redact(text):
    return SECRET_VALUE_RE.sub("[redacted]", text) if isinstance(text, str) else text


def write_private_new(path, text):
    """Create a 0600 file; never overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
