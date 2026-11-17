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
    compare with the Apporo Secret without reading the key.
  - abilities, setups.version and channels_id_seq.
  Never: users, tokens, user_access_tokens, two_fas, passkeys, sessions,
  logs, quota_data, audit_logs or any other customer or identity table.

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
# Values that look like credentials (newapi_admin.SECRET_VALUE_RE plus PEM and URL user info).
SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{20,}|nap_[A-Za-z0-9_-]{16,})"
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
_WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|copy|vacuum|"
                          r"reindex|cluster|lock|call|do|comment|security|refresh|import|nextval|setval|"
                          r"pg_terminate_backend|pg_cancel_backend|set\s+session\s+authorization)\b", re.IGNORECASE)


def sql_literal(text):
    if "\x00" in text:
        raise ContentError("NUL in SQL literal")
    return "'" + text.replace("'", "''") + "'"


def check_read_only_sql(sql):
    """The only SQL this module sends: the read-only preamble, then one SELECT."""
    if not sql.startswith(READ_ONLY_PREAMBLE):
        raise ContentError("SQL must start with SET default_transaction_read_only = on")
    body = sql[len(READ_ONLY_PREAMBLE):]
    # Literals (option keys) may contain anything; check the statement shape without them.
    bare = re.sub(r"'(?:[^']|'')*'", "''", body)
    if not bare.lstrip().upper().startswith("SELECT ") or bare.count(";") != 1 or not bare.rstrip().endswith(";"):
        raise ContentError("SQL must be exactly one SELECT statement")
    found = _WRITE_WORDS.search(bare)
    if found:
        raise ContentError(f"SQL contains {found.group(0)!r}")
    lowered = bare.lower()
    for table in FORBIDDEN_TABLES:
        if re.search(r"\b(from|join)\s+" + re.escape(table) + r"\b", lowered):
            raise ContentError(f"SQL reads forbidden table {table}")
    # The bare identifier `key` may appear only in these places: the options
    # table's key column, and channels.key inside sha256()/length() (target
    # key fingerprint). Anything else could return a channel key.
    rest = lowered
    for allowed in _KEY_CONTEXTS:
        rest = rest.replace(allowed, "")
    if re.search(r"\bkey\b", rest):
        raise ContentError("SQL uses the column `key` outside the allowed read-only contexts")
    return sql


_KEY_CONTEXTS = (
    "jsonb_agg(key order by key collate",      # option key names
    "jsonb_build_array(key, value,",           # option rows
    "order by key collate",
    "where key in (",
    "sha256(convert_to(key, ''))",             # channel key fingerprint (literal stripped)
    "length(key)",
)


def sql_option_keys():
    return check_read_only_sql(READ_ONLY_PREAMBLE + (
        "SELECT jsonb_build_object("
        "'version', (SELECT version FROM setups ORDER BY id LIMIT 1), "
        "'keys', (SELECT coalesce(jsonb_agg(key ORDER BY key COLLATE \"C\"), '[]'::jsonb) FROM options)"
        ")::text;"))


def sql_content(option_keys, key_hashes=False):
    keys = ",".join(sql_literal(k) for k in option_keys) or "NULL"
    channel_fields = ", ".join(f"'{api}', {col}" for api, col in CHANNEL_COLUMNS)
    if key_hashes:
        channel_fields += ", 'key_sha256_16', left(encode(sha256(convert_to(key, 'UTF8')), 'hex'), 16), 'key_length', length(key)"
    ability_fields = "'group', \"group\", 'model', model, 'channel_id', channel_id, 'enabled', enabled, " \
                     "'priority', priority, 'weight', weight, 'tag', tag"
    return check_read_only_sql(READ_ONLY_PREAMBLE + (
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
        ")::text;"))


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

    def query_json(self, sql):
        check_read_only_sql(sql)
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
    body = psql.query_json(sql_content(readable, key_hashes))
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
        bad = sorted(f for f, v in ch.items() if f in CHANNEL_COPY_FIELDS and secret_like(v))
        if bad:
            # A credential inside base_url, header_override, ...: never copied or printed.
            unsafe[ch["id"]] = bad
            ch = {f: ("[redacted]" if f in bad else v) for f, v in ch.items()}
        channels[ch["id"]] = ch
    abilities = body.get("abilities") or []
    for a in abilities:
        if not isinstance(a, dict) or set(a) != set(ABILITY_COLUMNS):
            raise ContentError("invalid ability row")
    return {"version": body.get("version") or head.get("version"), "options": options, "option_sha256": hashes,
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
    return p


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


def redact(text):
    return SECRET_VALUE_RE.sub("[redacted]", text) if isinstance(text, str) else text


def write_private_new(path, text):
    """Create a 0600 file; never overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
