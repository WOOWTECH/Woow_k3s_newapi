#!/usr/bin/env python3
"""Apporo New API content replay and daily sync (WOOW new-api -> new-api-apporo).

The Apporo New API (namespace new-api-apporo, https://newapi.apporo.ai) carries
the same content as WOOW production (namespace new-api), with these
differences (deploy/apporo-sync/profile.json, docs/APPORO-SYNC.md):

  - ModelRatio and ModelPrice divided by 30 (float64, stored exactly as rc.44
    stores them); every other price map is copied unchanged;
  - channel 2 (WOOW's internal ChatGPT subscription test route) is not copied;
    channels 1, 3, 4 keep their ids; their keys are Apporo's own LiteLLM keys
    from the Secret new-api-apporo/newapi-upstream-keys (never printed);
  - ServerAddress, the English UserUsableGroups labels and the currency
    symbol "$" are Apporo's own; instance rows (passkey.*,
    LegacyAccessTokenRetireAt) are never copied.

Reads are read-only SQL through `kubectl exec ... psql` (scripts/newapi_content.py:
explicit column lists, no channel key, no user/token/log table). Writes go only
to the target, through the New API admin API over a `kubectl port-forward`,
as root: a PAT from the Secret newapi-sync-pat (key token), or before root has
2FA the password in newapi-root-credentials.

Commands:
  export   [--side source|target] [--out FILE]   read-only; fingerprints, optional 0600 snapshot
  plan     [--bootstrap] [--json]                read-only; what apply would do, classified
  apply                                          daily rules: auto items, matured holds
  replay   --yes-bootstrap                       fresh target after /api/setup: copy everything
  verify   [--strict] [--json]                   read-only SQL fingerprints of the target
  daily    [--apply]                             images + apply + alerts (the timer runs this)
  notice-sent NOTICE_ID [--at ISO-8601]          the owner sent a price-increase notice
  holds                                          list holds and notices
  check-images [--live]                          deploy/woow-k3s.yaml vs deploy/apporo-k3s.yaml
  mint-sync-pat                                  bootstrap only: root PAT into newapi-sync-pat

Daily classification (docs/APPORO-SYNC.md):
  auto   new models, price decreases, non-price option changes, model-list
         changes of the paid channels, removed models (plus an alert),
         Apporo-owned settings, channel creation and key rotation;
  hold   anything that raises the effective price of a model the target
         serves: drafted English notice; applied only when the owner has
         marked the notice sent (notice-sent) and 7 days have passed;
  alert  changes that need a platform or values change, or would make the
         platform's pricing validation fail: never applied automatically.

Exit status: 0 ok, 1 a write or check failed, 2 usage, target or connection error.
"""
import argparse
import base64
import contextlib
import datetime
import decimal
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import newapi_admin as na
import newapi_content as nc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_PROFILE = os.path.join(REPO, "deploy", "apporo-sync", "profile.json")
DEFAULT_STATE_DIR = os.path.expanduser(os.environ.get("APPORO_SYNC_STATE_DIR", "~/.local/state/apporo-newapi-sync"))
WOOW_ORIGINS = frozenset(na.TARGET_URLS.values())
FRESH_ORIGINS = frozenset(("http://localhost:3000", ""))
PAT_SCOPES = ("channel:sensitive_write", "channel:write", "option:write")
PAT_DAYS = 180
PAT_WARN_DAYS = 14
TPE = datetime.timezone(datetime.timedelta(hours=8))

# Options a change of which needs the Apporo platform (or its values) to change
# in step: docs/OPERATIONS.md 〈PaaS 依賴的設定不變量〉, the /api/status
# contract (C22) and the pricing validator inputs. Never applied by the sync.
PLATFORM_COUPLED = frozenset((
    "UserUsableGroups", "GroupGroupRatio", "group_ratio_setting.group_special_usable_group",
    "quota_setting.enable_free_model_pre_consume", "quota_setting.trust_quota_usd",
    "ModelRequestRateLimitEnabled", "ModelRequestRateLimitDurationMinutes", "ModelRequestRateLimitCount",
    "ModelRequestRateLimitGroup", "ModelRequestRateLimitSuccessCount",
    "RegisterEnabled", "QuotaForNewUser", "QuotaForInviter", "QuotaForInvitee",
    "payment_setting.compliance_confirmed", "checkin_setting.enabled", "LogConsumeEnabled", "DataExportEnabled",
    "SelfUseModeEnabled", "HeaderNavModules", "ExposeRatioEnabled", "general_setting.quota_display_type",
    "general_setting.custom_currency_exchange_rate", "QuotaPerUnit", "PasswordLoginEnabled",
    "TurnstileCheckEnabled", "DisplayInCurrencyEnabled", "DemoSiteEnabled", "AutoGroups", "DefaultUseAutoGroup",
))
PLATFORM_COUPLED_GROUPS = frozenset(("default", "free"))  # GroupRatio entries the pricing validator pins
# Customer-visible text and branding: needs an English Apporo version.
CUSTOMER_TEXT_RE = re.compile(r"^(SystemName|Logo|Footer|About|HomePageContent|Notice|ChatLink2?|Chats|TopUpLink|"
                              r"console_setting\..*|legal\..*|general_setting\.docs_link|.*Announcement.*)$")
# Needs Apporo's own configuration (OAuth apps, mail, captcha, payments, workers).
NEEDS_CONFIG_RE = re.compile(r"(OAuth|^oidc\.|^discord\.|^telegram\.|WeChat|Telegram|LinuxDO|GitHub|SMTP|Turnstile|"
                             r"EmailVerification|EmailDomain|EmailAlias|PasswordRegister|^passkey\.|Stripe|Epay|Waffo|"
                             r"Creem|^Pay|TopUp|Worker|CustomCallbackAddress|TaskPublicAddress|^payment_setting\.)")
# Prices whose direction cannot be read from the value: a person decides.
OPAQUE_PRICE_RE = re.compile(r"^(tool_price_setting\..*|task_pricing_setting\..*|Price|USDExchangeRate|PreConsumedQuota|"
                             r"QuotaRemindThreshold)$")
# Security-relevant switches (docs/OPERATIONS.md: JS task plugins can read channel keys).
SECURITY_KEYS = frozenset(("TaskPluginEnabled", "TaskEnabled", "MjForwardUrlEnabled"))
# Channel fields the sync copies by itself; any other field change is an alert.
CHANNEL_AUTO_FIELDS = frozenset(("name", "test_model", "remark"))
# rc.44 Channel fields that are Go *pointers: PUT can set them to "" (not to null).
# The others are plain strings: GORM Updates() skips "" for them.
CHANNEL_POINTER_FIELDS = frozenset(("openai_organization", "test_model", "base_url", "model_mapping",
                                    "status_code_mapping", "priority", "weight", "auto_ban", "tag", "setting",
                                    "param_override", "header_override", "remark"))


# Test seams (scripts/tests/test_apporo_sync.py replaces them with in-process fakes).
PSQL_FACTORY = None        # (side, profile) -> object with query_json(sql)
SECRET_READER = None       # (namespace, name, key) -> str
ANNOTATION_READER = None   # (namespace, name, annotation) -> str | None


class SyncError(Exception):
    """Usage, target, connection or safety error (exit 2)."""


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(text):
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise SyncError(f"not an ISO-8601 time: {text!r}") from None
    if dt.tzinfo is None:
        raise SyncError(f"{text!r}: give a time zone, e.g. 2026-11-18T09:00:00+08:00")
    return dt.astimezone(datetime.timezone.utc)


class Secret:
    """A credential kept only in memory; repr/str never show it."""

    def __init__(self, value):
        self._v = value

    def reveal(self):
        return self._v

    def sha16(self):
        return hashlib.sha256(self._v.encode("utf-8")).hexdigest()[:16]

    def __repr__(self):
        return "Secret(***)"

    __str__ = __repr__


# ------------------------------------------------------------- kubernetes

def kubectl_argv(a):
    return shlex.split(a.kubectl or os.environ.get("APPORO_SYNC_KUBECTL") or "kubectl")


def read_secret(kube, namespace, name, key, run=subprocess.run):
    if SECRET_READER is not None:
        value = SECRET_READER(namespace, name, key)
        if value is None:
            raise SyncError(f"secret {namespace}/{name} has no key {key}")
        return Secret(value)
    path = "{.data." + key.replace(".", "\\.") + "}"
    try:
        r = run(kube + ["-n", namespace, "get", "secret", name, "-o", "jsonpath=" + path],
                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SyncError(f"secret {namespace}/{name}: kubectl did not run ({e.__class__.__name__})") from None
    if r.returncode != 0:
        raise SyncError(f"secret {namespace}/{name}: not readable (kubectl exit {r.returncode})")
    raw = (r.stdout or b"").strip()
    if not raw:
        raise SyncError(f"secret {namespace}/{name} has no key {key}")
    try:
        return Secret(base64.b64decode(raw, validate=True).decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        raise SyncError(f"secret {namespace}/{name}: key {key} is not base64 text") from None


def read_annotation(kube, namespace, name, annotation, run=subprocess.run):
    if ANNOTATION_READER is not None:
        return ANNOTATION_READER(namespace, name, annotation)
    path = "{.metadata.annotations." + annotation.replace(".", "\\.") + "}"
    r = run(kube + ["-n", namespace, "get", "secret", name, "-o", "jsonpath=" + path], capture_output=True, timeout=60)
    return (r.stdout or b"").decode("utf-8").strip() if r.returncode == 0 else None


def free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForward:
    """kubectl port-forward to svc/<service>; ready only once /api/status answers JSON."""

    def __init__(self, kube, namespace, service="newapi", port=3000, timeout=45):
        self.kube, self.namespace, self.service, self.port, self.timeout = kube, namespace, service, port, timeout
        self.proc = None

    def __enter__(self):
        local = free_port()
        self.proc = subprocess.Popen(self.kube + ["-n", self.namespace, "port-forward", "--address", "127.0.0.1",
                                                  f"svc/{self.service}", f"{local}:{self.port}"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        base = f"http://127.0.0.1:{local}"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
                raise SyncError(f"port-forward to {self.namespace}/svc/{self.service} ended: "
                                + (err[-1][:200] if err else "no message"))
            try:
                req = urllib.request.Request(base + "/api/status", headers={"User-Agent": na.UA})
                with na._OPENER.open(req, timeout=5) as resp:
                    if json.loads(resp.read() or b"{}").get("success") is not None:
                        return base
            except (OSError, ValueError, urllib.error.URLError, na.Fatal):
                pass
            time.sleep(0.5)
        self.__exit__(None, None, None)
        raise SyncError(f"port-forward to {self.namespace}/svc/{self.service}: /api/status did not answer")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


# --------------------------------------------------------------- reading

def psql_for(a, profile, side):
    if PSQL_FACTORY is not None:
        return PSQL_FACTORY(side, profile)
    s = profile[side]
    return nc.Psql(kubectl_argv(a), s["namespace"], s.get("postgres_pod", "newapi-postgres-0"),
                   s.get("container", "postgres"), s.get("user", "newapi"), s.get("db", "newapi"))


def read_source(a, profile):
    if getattr(a, "source_snapshot", None):
        with open(a.source_snapshot) as f:
            snap = json.load(f)
        snap.pop("_meta", None)
        snap["channels"] = {int(k): v for k, v in snap["channels"].items()}
        snap["unsafe_channels"] = {int(k): v for k, v in (snap.get("unsafe_channels") or {}).items()}
        return snap
    return nc.read_snapshot(psql_for(a, profile, "source"), profile)


def read_target(a, profile):
    return nc.read_snapshot(psql_for(a, profile, "target"), profile, key_hashes=True)


def check_versions(src, tgt):
    for name, snap in (("source", src), ("target", tgt)):
        if snap is not None and snap.get("version") != nc.NEWAPI_VERSION:
            raise SyncError(f"{name} runs {snap.get('version')!r}; this tool is for {nc.NEWAPI_VERSION} "
                            "(rc.44 storage rules): re-check docs/APPORO-SYNC.md before an upgrade")


# ------------------------------------------------------------ the target

def channel_keys(a, profile):
    ks = profile["key_secret"]
    out = {}
    for slot, key_name in ks["slots"].items():
        sec = read_secret(kubectl_argv(a), profile["target"]["namespace"], ks["name"], key_name)
        if not nc.UPSTREAM_KEY_RE.fullmatch(sec.reveal()):
            raise SyncError(f"{ks['name']}/{key_name} does not look like a LiteLLM key (sk-...)")
        out[slot] = sec
    return out


def make_client(a, profile, base):
    ns = profile["target"]["namespace"]
    auth = profile.get("auth", {})
    if a.token_file:
        return na.Client(base, token=na.read_token_file(a.token_file))
    if a.auth == "pat":
        tok = read_secret(kubectl_argv(a), ns, auth.get("pat_secret", "newapi-sync-pat"), "token")
        if not na.PAT_RE.fullmatch(tok.reveal()):
            raise SyncError("the sync PAT Secret does not hold one nap_... token")
        return na.Client(base, token=tok.reveal())
    if a.auth == "password":
        name = auth.get("password_secret", "newapi-root-credentials")
        user = read_secret(kubectl_argv(a), ns, name, "username").reveal()
        pw = read_secret(kubectl_argv(a), ns, name, "password")
        return na.Client(base, creds={"username": user, "password": pw.reveal()})
    raise SyncError("--auth must be pat or password (or pass --token-file)")


def guard_target(client, profile, tgt, bootstrap):
    """Write only to the profile's target: rc.44, its own ServerAddress (or a fresh one)."""
    st = client.status()
    sa = (st.get("server_address") or "").rstrip("/")
    want = profile["target"]["server_address"].rstrip("/")
    if st.get("version") != nc.NEWAPI_VERSION:
        raise SyncError(f"target runs {st.get('version')!r}, not {nc.NEWAPI_VERSION}")
    if sa in WOOW_ORIGINS or want in WOOW_ORIGINS:
        raise SyncError(f"refusing to write to a WOOW site ({sa or want})")
    if sa != want and not (bootstrap and sa in FRESH_ORIGINS):
        raise SyncError(f"target ServerAddress is {sa!r}, the profile expects {want!r}"
                        + ("" if bootstrap else " (a fresh install needs `replay`)"))
    db_sa = (tgt["options"].get("ServerAddress") or "http://localhost:3000").rstrip("/")
    if db_sa != (sa or "http://localhost:3000"):
        raise SyncError("the API and the database disagree on ServerAddress: "
                        "port-forward and psql reach different instances")
    return sa


def api(client, method, path, body=None, headers=None):
    """One admin API call; True on success. Upstream messages are redacted, keys never echoed."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(client.base + path, data=data, method=method)
    req.add_header("User-Agent", na.UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + client.bearer())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with na._OPENER.open(req, timeout=60) as resp:
            answer = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            answer = json.loads(e.read()[:2000] or b"{}")
        except ValueError:
            answer = {"message": f"HTTP {e.code}"}
        answer.setdefault("success", False)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{method} {path}: {e.__class__.__name__} (outcome unknown)", {}
    ok = isinstance(answer, dict) and answer.get("success") is True
    msg = "" if ok else nc.redact(str(answer.get("message") or answer.get("code") or "rejected"))[:200]
    return ok, msg, answer if isinstance(answer, dict) else {}


# ---------------------------------------------------------- option classes

def option_category(key, profile):
    """auto | platform | security | text | config | opaque-price | target (Apporo-owned)."""
    if key in (profile.get("target_options") or {}) or key in nc.SERVER_KEYS:
        return "target"
    if key in PLATFORM_COUPLED:
        return "platform"
    if key in SECURITY_KEYS:
        return "security"
    if CUSTOMER_TEXT_RE.match(key):
        return "text"
    if NEEDS_CONFIG_RE.search(key):
        return "config"
    if OPAQUE_PRICE_RE.match(key):
        return "opaque-price"
    return "auto"


# -------------------------------------------------------------- the plan

def item(kind, cls=None, reason="", **kw):
    it = {"kind": kind, "class": cls, "reason": reason, "apply": False, "hold_id": None, "alert": False}
    it.update(kw)
    it["subject"] = subject_of(it)
    return it


def subject_of(it):
    k = it["kind"]
    if k in ("opt",):
        return f"option {it['key']}"
    if k == "map":
        return f"{it['key']}[{it['entry']}]"
    if k.startswith("chan"):
        return f"channel {it['channel']}" + (f" {it.get('field')}" if it.get("field") else "") + \
            (f" model {it['model']}" if it.get("model") else "")
    return it.get("subject_text") or k


def build_items(src, tgt, desired, notes, profile, keys=None, bootstrap=False):
    """Differences desired vs target as items (unclassified)."""
    items = []
    for kind, subj, text in notes:
        items.append(item("alert", "alert", text, subject_text=f"{kind}: {subj}", note=kind))
    want, have = desired["options"], tgt["options"]
    for key in sorted(set(want) | set(have), key=lambda k: k.encode()):
        cls = nc.option_key_class(key, profile)
        if cls in ("derived", "instance", "never", "invalid"):
            continue
        if key not in want:
            if cls == "copy" and key not in BOOT_KEYS:
                items.append(item("alert", "alert", "the target has this option row, the source does not "
                                  "(rc.44 has no API to delete it)", subject_text=f"option-extra: {key}", key=key))
            continue
        d, t = want[key], have.get(key)
        if d == t:
            continue
        if key in nc.PRICING_KEYS and t is not None and not bootstrap:
            items += map_items(key, t, d, go=True)
        elif key in nc.PRICING_KEYS and t is None and not bootstrap:
            items.append(item("alert", "alert", "the target has no row for this price map: run replay",
                              subject_text=f"pricing-row-missing: {key}"))
        elif key in nc.GROUP_PRICE_MAPS and t is not None and not bootstrap and _is_object(t) and _is_object(d):
            items += map_items(key, t, d, go=False)
        else:
            items.append(item("opt", key=key, old=t, new=d))
    managed = set(desired["channels"])
    for cid in sorted(managed):
        d, t = desired["channels"][cid], tgt["channels"].get(cid)
        slot = desired["channel_slots"][cid]
        if t is None:
            items.append(item("chan-create", channel=cid, source_id=desired["source_ids"][cid], slot=slot))
            continue
        for f in nc.CHANNEL_COPY_FIELDS:
            if f == "models":
                # the free-channel rule looks at both groups: a group change itself is alert-only
                both = ",".join(g for g in (t.get("group"), d.get("group")) if g)
                items += model_items(cid, t.get("models"), d.get("models"), both)
            elif d.get(f) != t.get(f):
                items.append(item("chan-field", channel=cid, field=f, old=t.get(f), new=d.get(f)))
        if keys is not None and slot in keys and t.get("key_sha256_16") != keys[slot].sha16():
            items.append(item("chan-rekey", channel=cid, slot=slot))
        if t.get("status") != 1:
            items.append(item("alert", "alert", f"status {t.get('status')}: disabled on the target "
                              "(auto-disabled by upstream errors, or by hand); the sync does not change status",
                              subject_text=f"channel-disabled: channel {cid}"))
    for cid in sorted(set(tgt["channels"]) - managed):
        items.append(item("alert", "alert", "a channel the profile does not manage; left alone",
                          subject_text=f"channel-unmanaged: channel {cid} ({tgt['channels'][cid].get('name')})"))
    return items


BOOT_KEYS = frozenset(("theme.frontend", "SelfUseModeEnabled", "DemoSiteEnabled"))


def _is_object(text):
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def map_items(key, old_text, new_text, go):
    old = nc.canonical_map(old_text, key) if go else nc.parse_object(old_text, key)
    new = nc.canonical_map(new_text, key) if go else nc.parse_object(new_text, key)
    out = []
    for entry in sorted(set(old) | set(new), key=lambda e: e.encode()):
        o, n = old.get(entry), new.get(entry)
        if o != n or (o is None) != (n is None):
            out.append(item("map", key=key, entry=entry, old=o, new=n))
    if not out and old_text != new_text:
        out.append(item("opt", key=key, old=old_text, new=new_text, reason="same entries, different text"))
    return out


def _models(text):
    return [m for m in (text or "").split(",")] if text else []


def model_items(cid, old_text, new_text, group):
    old, new = _models(old_text), _models(new_text)
    out = []
    for m in new:
        if m not in old:
            out.append(item("chan-model-add", channel=cid, model=m, group=group))
    for m in old:
        if m not in new:
            out.append(item("chan-model-remove", channel=cid, model=m, group=group))
    if not out and old_text != new_text:
        out.append(item("chan-field", channel=cid, field="models", old=old_text, new=new_text,
                        reason="same models, different order"))
    return out


# --------------------------------------------------------- classification

def served_models(tgt):
    out = {}
    for a in tgt["abilities"]:
        if a["enabled"]:
            out.setdefault(a["model"], set()).add(a["group"])
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def classify(items, tgt, profile, bootstrap=False, desired=None):
    """Set class auto/hold/alert and reason on every item (bootstrap: everything auto)."""
    for it in items:
        if it["kind"] == "alert":
            it["class"] = "alert"
            continue
        if bootstrap:
            it["class"], it["reason"] = "auto", it["reason"] or "bootstrap replay"
            continue
        k = it["kind"]
        if k == "opt":
            classify_option(it, profile)
        elif k == "map":
            continue  # second pass: needs what the channel changes leave served
        elif k == "chan-create":
            it["class"], it["reason"] = "auto", "mapped channel missing on the target"
        elif k == "chan-field":
            if it["field"] in _owned_fields(it["channel"], profile):
                it["class"], it["reason"] = "auto", "Apporo's own channel setting (profile)"
            elif it["field"] in CHANNEL_AUTO_FIELDS:
                it["class"], it["reason"] = "auto", "channel metadata"
            elif it["field"] == "models":
                it["class"], it["reason"] = "auto", it["reason"] or "model order"
            else:
                it["class"], it["reason"] = "alert", f"channel {it['field']} changed: routing, upstream or " \
                                                      "values may need a change (never automatic)"
        elif k in ("chan-model-add", "chan-model-remove"):
            groups = set((it.get("group") or "").split(","))
            if groups & set(profile.get("free_groups", ("free",))):
                it["class"], it["reason"] = "alert", "free-trial channel: proxy.freeModels and the platform's " \
                                                     "FREE_MODELS must change with it"
            elif k == "chan-model-add":
                it["class"], it["reason"] = "auto", "new model route"
            else:
                it["class"], it["reason"], it["alert"] = "auto", "model removed at the source", True
        elif k == "chan-rekey":
            it["class"], it["reason"] = "auto", "channel key differs from the Apporo key Secret"
    if bootstrap:
        return items
    # A price matters to customers only for a model served before and after this run.
    before = served_models(tgt)
    routes = [it for it in items if it["class"] == "auto" and it["kind"] in ("chan-create", "chan-model-add",
                                                                             "chan-model-remove", "chan-field")]
    after = served_models(simulate(tgt, routes, profile, desired))
    in_use = {m: before[m] & after[m] for m in before if m in after}
    for it in items:
        if it["kind"] == "map":
            classify_map(it, in_use, {g for gs in in_use.values() for g in gs})
    platform_gate(items, tgt, profile, desired)
    return items


def classify_option(it, profile):
    key = it["key"]
    cat = option_category(key, profile)
    if it.get("reason") == "same entries, different text":
        it["class"] = "auto"
    elif cat == "target":
        it["class"], it["reason"] = "auto", "Apporo's own setting (profile)"
    elif key == "UserUsableGroups" and _same_object_keys(it.get("old"), it.get("new")):
        it["class"], it["reason"] = "auto", "group labels only (the profile's English labels)"
    elif cat == "platform":
        it["class"], it["reason"] = "alert", "platform-coupled option: the Apporo platform must change with it"
    elif cat == "security":
        it["class"], it["reason"] = "alert", "security-relevant switch: decide by hand"
    elif cat == "text":
        it["class"], it["reason"] = "alert", "customer-visible text: needs an English Apporo version"
    elif cat == "config":
        it["class"], it["reason"] = "alert", "needs Apporo's own configuration (accounts, secrets or payments)"
    elif cat == "opaque-price":
        it["class"], it["reason"] = "alert", "a price whose direction cannot be read: decide by hand"
    elif key in nc.PRICING_KEYS or key in nc.GROUP_PRICE_MAPS:
        it["class"], it["reason"] = "alert", "price option that cannot be compared entry by entry: decide by hand"
    else:
        it["class"], it["reason"] = "auto", "non-price option"


def _same_object_keys(a, b):
    try:
        x, y = json.loads(a), json.loads(b)
    except (TypeError, ValueError):
        return False
    return isinstance(x, dict) and isinstance(y, dict) and list(x) == list(y)


def _owned_fields(cid, profile):
    for sid, conf in profile["channels"].items():
        if conf.get("target_id", int(sid)) == cid:
            return set(conf.get("set", {}))
    return set()


def classify_map(it, served, served_groups):
    key, entry, o, n = it["key"], it["entry"], it["old"], it["new"]
    if key in ("GroupRatio",) and entry in PLATFORM_COUPLED_GROUPS:
        it["class"], it["reason"] = "alert", "GroupRatio default/free: the platform's pricing profile pins them"
        return
    if key == "GroupGroupRatio":
        it["class"], it["reason"] = "alert", "GroupGroupRatio must stay {} (platform invariant)"
        return
    if key == "TopupGroupRatio":
        it["class"], it["reason"] = "auto", "top-up ratio (New API payments are off)"
        return
    if key == "GroupRatio":
        in_use = entry in served_groups
    elif key == "billing_setting.plugin_billing_expr":
        in_use = any(m in entry for m in served)       # entries are <plugin><separator><model>
    else:
        in_use = entry in served
    if not in_use:
        it["class"], it["reason"] = "auto", "not served on the target before and after this run: " \
                                            "no customer price changes"
        return
    if key in nc.BILLING_MAPS:
        it["class"], it["reason"] = "hold", "billing expression or mode of a served model: direction unknown"
        return
    if key in ("ModelRatio", "ModelPrice") and n is not None and _num(n) and n == 0 and (o is None or o != 0):
        it["class"], it["reason"] = "alert", "the price drops to 0: free for every key at any balance; confirm by hand"
        return
    if key == "ModelRatio" and n is None:
        it["class"], it["reason"] = "alert", "the served model loses its price: rc.44 would refuse it"
        return
    if key == "ModelRatio" and o is None:
        it["class"], it["reason"] = "auto", "a served model gets its first ratio (it was unpriced)"
        return
    if key == "ModelPrice" and (o is None) != (n is None):
        it["class"], it["reason"] = "hold", "switches between per-call and per-token billing: direction unknown"
        return
    if key == "CompletionRatio" and (o is None) != (n is None):
        it["class"], it["reason"] = "hold", "completion ratio falls back to rc.44's built-in rule: direction unknown"
        return
    old_v = nc.ABSENT_DEFAULTS.get(key) if o is None else o
    new_v = nc.ABSENT_DEFAULTS.get(key) if n is None else n
    if not (_num(old_v) and _num(new_v)):
        it["class"], it["reason"] = "hold", "not comparable: direction unknown"
    elif new_v > old_v:
        it["class"], it["reason"] = "hold", "price increase"
    else:
        it["class"], it["reason"] = "auto", "price decrease" if new_v < old_v else "same effective value"


# --------------------------------------------- the platform's pricing gate

def _dec(value):
    if isinstance(value, bool) or type(value) not in (int, float, str, decimal.Decimal) or len(str(value)) > 64:
        raise ValueError("pricing_number")
    d = decimal.Decimal(str(value))
    if not d.is_finite() or d < 0 or len(d.as_tuple().digits) > 24 or (d and not -18 <= d.adjusted() <= 12):
        raise ValueError("pricing_number")
    return d


def pricing_payload(state, profile):
    """What GET /api/pricing answers the platform's service account (group default), from DB state."""
    opts = state["options"]

    def obj(k):
        try:
            return json.loads(opts[k]) if k in opts else {}
        except ValueError:
            return {}
    usable = dict(obj("UserUsableGroups"))
    usable.setdefault("default", "User group")
    group_ratio = {g: r for g, r in obj("GroupRatio").items() if g in usable}
    ratio, price, comp = obj("ModelRatio"), obj("ModelPrice"), obj("CompletionRatio")
    cache, create = obj("CacheRatio"), obj("CreateCacheRatio")
    mode, expr = obj("billing_setting.billing_mode"), obj("billing_setting.billing_expr")
    groups = {}
    for a in state["abilities"]:
        if a["enabled"]:
            groups.setdefault(a["model"], []).append(a["group"])
    rows = []
    for model, gs in groups.items():
        gs = sorted(set(gs), key=gs.index)
        if "all" not in gs and not any(g in usable for g in gs):
            continue
        row = {"model_name": model, "enable_groups": gs}
        if model in price:
            row.update(quota_type=1, model_price=price[model], model_ratio=0, completion_ratio=0)
        else:
            row.update(quota_type=0, model_price=0, model_ratio=ratio.get(model, 37.5),
                       completion_ratio=comp.get(model, 1))
        if model in cache:
            row["cache_ratio"] = cache[model]
        if model in create:
            row["create_cache_ratio"] = create[model]
        if mode.get(model) == "tiered_expr" and str(expr.get(model, "")).strip():
            row["billing_mode"] = "tiered_expr"
        rows.append(row)
    return {"group_ratio": group_ratio, "usable_group": usable, "data": rows, "vendors": []}


MODEL_NAME_RE = re.compile(r"[A-Za-z0-9_.:/+-]{1,200}")


def platform_check(payload, platform):
    """Port of woow_paas_platform src/services/llm_pricing.py (18.0.2.0.16) normalize_pricing():
    None when the platform would publish this catalogue, else its PricingInvalid label.
    `apporo_sync.py ... --platform-pricing-module` runs the real module instead."""
    allowed = frozenset(platform.get("allowed_groups", ("default", "free")))
    free_models = frozenset(platform.get("free_models", ()))
    pilot = frozenset(platform.get("paid_pilot_models", ()))
    groups = payload["group_ratio"]
    try:
        if platform.get("pricing_profile"):
            return _paid_pilot_check(payload, groups, allowed, free_models, pilot)
        return _ga_check(payload, groups, allowed, free_models)
    except _Invalid as e:
        return e.label


class _Invalid(Exception):
    def __init__(self, label):
        super().__init__(label)
        self.label = label


def _n(value):
    try:
        return _dec(value)
    except (ValueError, decimal.InvalidOperation):
        raise _Invalid("pricing_number") from None


def _row_prices(row, ratio):
    """model_price(): the populated price fields (points; the constant 32 cannot change a sign)."""
    if row.get("billing_mode") == "tiered_expr":
        return "tiered", None
    if row.get("billing_mode") not in (None, "", "ratio", "fixed"):
        raise _Invalid("pricing_mode")
    if type(row.get("quota_type")) is not int:
        raise _Invalid("pricing_mode")
    if row["quota_type"] == 1:
        return "fixed", [_n(row.get("model_price")) * ratio]
    if row["quota_type"] != 0:
        raise _Invalid("pricing_mode")
    inp = 2 * _n(row.get("model_ratio")) * ratio
    out = [inp, inp * _n(row.get("completion_ratio"))]
    if row.get("cache_ratio") is not None:
        out.append(inp * _n(row["cache_ratio"]))
    out.append(inp * _n(row["create_cache_ratio"] if row.get("create_cache_ratio") is not None else "1.25"))
    return "ratio", out


def _paid_pilot_check(payload, groups, allowed, free_models, pilot):
    if any(type(g) is not str or g not in allowed for g in groups) or "default" not in groups:
        raise _Invalid("pricing_groups")
    if _n(groups["default"]) != 1:
        raise _Invalid("pricing_ratio")
    if "free" in groups and _n(groups["free"]) != 0:
        raise _Invalid("pricing_ratio")
    seen, paid = set(), 0
    for row in payload["data"]:
        enabled, name = row.get("enable_groups"), row.get("model_name")
        if type(enabled) is not list or any(type(g) is not str for g in enabled):
            raise _Invalid("pricing_shape")
        if type(name) is not str or not MODEL_NAME_RE.fullmatch(name):
            raise _Invalid("pricing_model")
        retained = (name in pilot and name.startswith("chatgpt/")
                    and set(enabled) == {"default", "internal"} and len(enabled) == 2)
        if not retained and any(g not in allowed for g in enabled):
            raise _Invalid("pricing_groups")
        if name in seen:
            raise _Invalid("pricing_duplicate")
        seen.add(name)
        if name in free_models:
            if enabled == ["free"] and "free" not in groups:
                raise _Invalid("pricing_groups")
            continue
        if name not in pilot:
            continue
        if enabled != ["default"] and not retained:
            raise _Invalid("pricing_groups")
        mode, prices = _row_prices(row, decimal.Decimal(1))
        if mode not in ("ratio", "fixed") or prices is None:
            raise _Invalid("pricing_mode")
        if not prices or any(p <= 0 for p in prices):
            raise _Invalid("pricing_zero")
        paid += 1
    if paid == 0:
        raise _Invalid("pricing_scope")
    return None


def _ga_check(payload, groups, allowed, free_models):
    if not set(groups) <= allowed:
        raise _Invalid("pricing_groups")
    if "default" in groups and _n(groups["default"]) != decimal.Decimal("1.3"):
        raise _Invalid("pricing_ratio")
    if "free" in groups and _n(groups["free"]) != 0:
        raise _Invalid("pricing_ratio")
    seen = set()
    for row in payload["data"]:
        got = {g for g in row.get("enable_groups") or [] if isinstance(g, str)} & allowed
        if not got:
            continue
        if got == allowed:
            raise _Invalid("pricing_groups")
        name = row.get("model_name")
        if not isinstance(name, str) or not MODEL_NAME_RE.fullmatch(name):
            raise _Invalid("pricing_model")
        group = "free" if name in free_models and "free" in got else "default"
        if group not in got or group not in groups:
            raise _Invalid("pricing_groups")
        if name in free_models and group != "free":
            raise _Invalid("pricing_groups")
        if group == "default" and name.endswith(":free"):
            continue
        if group == "default":
            mode, prices = _row_prices(row, _n(groups[group]))
            if prices and prices[0] == 0:
                continue
        if name in seen:
            raise _Invalid("pricing_duplicate")
        seen.add(name)
    return None


def load_platform_module(path):
    """The real llm_pricing.py (pure Python: decimal, re), e.g. the Apporo image's vendored copy."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("woow_llm_pricing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def platform_verdict(payload, profile):
    platform = profile.get("platform") or {}
    mod = profile.get("_platform_module")
    if mod is None:
        return platform_check(payload, platform)
    try:
        mod.normalize_pricing(payload, profile=platform.get("pricing_profile", ""))
        return None
    except mod.PricingInvalid as e:
        return str(e)


def platform_gate(items, tgt, profile, desired=None):
    """Turn an auto/hold item into an alert when it alone (or all autos together) would make
    the platform's pricing validation fail where it passes today."""
    if not profile.get("platform"):
        return
    base = platform_verdict(pricing_payload(tgt, profile), profile)
    if base is not None:
        items.append(item("alert", "alert", f"the platform's pricing validation already fails on the target ({base}); "
                          "changes are not held back by this check until it passes",
                          subject_text="platform-pricing: target"))
        return
    relevant = [it for it in items if it["class"] in ("auto", "hold") and it["kind"] in (
        "opt", "map", "chan-create", "chan-model-add", "chan-model-remove", "chan-field")]
    for it in relevant:
        res = platform_verdict(pricing_payload(simulate(tgt, [it], profile, desired), profile), profile)
        if res is not None:
            it["class"], it["reason"] = "alert", f"would make the platform's pricing validation fail ({res})"
    combined_gate([it for it in relevant if it["class"] == "auto"], tgt, profile, desired)


def combined_gate(chosen, tgt, profile, desired=None):
    """All chosen items together must keep the platform's validation passing."""
    if not profile.get("platform") or not chosen:
        return True
    if platform_verdict(pricing_payload(tgt, profile), profile) is not None:
        return True
    res = platform_verdict(pricing_payload(simulate(tgt, chosen, profile, desired), profile), profile)
    if res is None:
        return True
    for it in chosen:
        if it["kind"] not in ("chan-rekey",):
            it["class"], it["apply"] = "alert", False
            it["reason"] = f"together the changes fail the platform's pricing validation ({res})"
    return False


# -------------------------------------------------------- effective state

def simulate(tgt, chosen, profile, desired=None):
    """The target state after the chosen items (options, channels, abilities)."""
    opts = dict(tgt["options"])
    chans = {cid: dict(ch) for cid, ch in tgt["channels"].items()}
    by_key = {}
    for it in chosen:
        if it["kind"] == "map":
            by_key.setdefault(it["key"], []).append(it)
        elif it["kind"] == "opt":
            opts[it["key"]] = it["new"]
    for key, its in by_key.items():
        go = key in nc.PRICING_KEYS
        cur = nc.canonical_map(opts[key], key) if go else nc.parse_object(opts[key], key)
        for it in its:
            if it["new"] is None:
                cur.pop(it["entry"], None)
            else:
                cur[it["entry"]] = it["new"]
        if go:
            opts[key] = nc.go_json(cur)
        else:
            want = desired["options"].get(key) if desired else None
            same = want is not None and json.loads(want) == cur
            opts[key] = want if same else json.dumps(cur, ensure_ascii=False)
    model_edits = {}
    for it in chosen:
        if it["kind"] == "chan-create" and desired:
            ch = dict(desired["channels"][it["channel"]], id=it["channel"], status=1)
            chans[it["channel"]] = ch
        elif it["kind"] == "chan-create" and "fields" in it:
            chans[it["channel"]] = dict(it["fields"], id=it["channel"], status=1)
        elif it["kind"] == "chan-field" and it["channel"] in chans:
            chans[it["channel"]][it["field"]] = it["new"]
        elif it["kind"] in ("chan-model-add", "chan-model-remove"):
            model_edits.setdefault(it["channel"], []).append(it)
    for cid, its in model_edits.items():
        if cid not in chans:
            continue
        cur = _models(chans[cid].get("models"))
        want_order = _models((desired or {}).get("channels", {}).get(cid, {}).get("models")) if desired else []
        adds = {it["model"] for it in its if it["kind"] == "chan-model-add"}
        removes = {it["model"] for it in its if it["kind"] == "chan-model-remove"}
        if want_order and set(cur) - removes | adds == set(want_order):
            result = want_order
        else:
            result = [m for m in cur if m not in removes] + [m for m in (want_order or sorted(adds))
                                                              if m in adds and m not in cur]
        chans[cid]["models"] = ",".join(result)
    abilities = nc.expected_abilities(chans, {cid: ch.get("status", 1) for cid, ch in chans.items()})
    return {"options": opts, "channels": chans, "abilities": abilities}


# -------------------------------------------------------------- holds

_OPEN_STATES = []


class State:
    """holds.json in the state directory (0700 dir, 0600 file), with a run lock.
    read_only=True (plan): no lock, no directory created, never saved."""

    def __init__(self, path, read_only=False):
        self.dir, self.read_only = path, read_only
        self.file = os.path.join(path, "holds.json")
        _OPEN_STATES.append(self)
        if not read_only:
            os.makedirs(path, mode=0o700, exist_ok=True)
            self.lock = open(os.path.join(path, ".lock"), "w")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SyncError(f"another apporo_sync run holds {path}/.lock") from None
        if os.path.exists(self.file):
            with open(self.file) as f:
                self.data = json.load(f)
            if self.data.get("schema") != 1:
                raise SyncError(f"{self.file}: unknown schema")
        else:
            self.data = {"schema": 1, "holds": {}, "notices": {}, "alerts": {}, "runs": []}

    def close(self):
        lock = getattr(self, "lock", None)
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            self.lock = None

    def save(self):
        if self.read_only:
            return
        tmp = self.file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.file)

    def open_holds(self):
        return {h["id"]: h for h in self.data["holds"].values() if h["state"] in ("pending-notice", "notified")}


def hold_subject(it):
    return [it["kind"], it.get("key"), it.get("entry")]


def process_holds(items, state, now, days, persist):
    """Match hold items with open holds; mark matured ones applicable; open new holds."""
    open_holds = state.open_holds() if state else {}
    used, new = set(), []
    for it in items:
        if it["class"] != "hold":
            continue
        match = next((h for h in open_holds.values() if h["subject"] == hold_subject(it)), None)
        if match and match["old"] == it["old"] and match["new"] == it["new"]:
            used.add(match["id"])
            it["hold_id"] = match["id"]
            due = match.get("eligible_at")
            if match["state"] == "notified" and due and parse_iso(due) <= now:
                it["apply"] = True
                it["reason"] += f"; notice sent {match['notice_sent_at']}, matured {due}"
            else:
                it["reason"] += "; held: " + ("waiting for notice-sent" if match["state"] == "pending-notice"
                                               else f"applies after {due}")
            continue
        if match:
            used.add(match["id"])
            if persist:
                match.update(state="superseded", closed_at=iso(now), close_reason="the desired value changed again")
        day = now.astimezone(TPE).strftime("%Y%m%d")
        hid = f"H-{day}-" + nc.digest([hold_subject(it), it["old"], it["new"]])[:8]
        it["hold_id"] = hid
        it["reason"] += "; new hold: draft notice, then notice-sent"
        new.append(it)
        if persist:
            state.data["holds"][hid] = {"id": hid, "subject": hold_subject(it), "old": it["old"], "new": it["new"],
                                        "reason": it["reason"], "detected_at": iso(now), "state": "pending-notice",
                                        "notice_id": None, "notice_sent_at": None, "eligible_at": None,
                                        "applied_at": None, "closed_at": None, "close_reason": None,
                                        "notice_days": days}
    if persist:
        for hid, h in open_holds.items():
            if hid not in used:
                h.update(state="cancelled", closed_at=iso(now),
                         close_reason="no longer a pending change (source reverted, or the target already has it)")
    return new


# ------------------------------------------------------------- notices

def usd(d):
    q = d.quantize(decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def model_prices(state, model, profile, group="default"):
    """Points (= US$ at Apporo) per 1M tokens, or per call, as the platform shows them."""
    opts = state["options"]

    def obj(k):
        try:
            return json.loads(opts.get(k) or "{}")
        except ValueError:
            return {}
    pts = decimal.Decimal(str(profile.get("points_per_usd", 32)))
    gr = obj("GroupRatio").get(group)
    if gr is None:
        return None
    gr = decimal.Decimal(str(gr))
    price = obj("ModelPrice").get(model)
    if price is not None:
        return {"per call": decimal.Decimal(str(price)) * gr * pts}
    ratio = obj("ModelRatio").get(model)
    if ratio is None:
        return None
    inp = 2 * decimal.Decimal(str(ratio)) * gr * pts
    out = {"input": inp, "output": inp * decimal.Decimal(str(obj("CompletionRatio").get(model, 1)))}
    cache = obj("CacheRatio").get(model)
    if cache is not None:
        out["cache read"] = inp * decimal.Decimal(str(cache))
    out["cache write"] = inp * decimal.Decimal(str(obj("CreateCacheRatio").get(model, 1.25)))
    return out


def draft_notice(nid, holds_items, tgt, profile, now, days):
    """English customer notice for the held increases: old vs new US$ per 1M tokens."""
    final = simulate(tgt, holds_items, profile)
    served = served_models(tgt)
    models = set()
    for it in holds_items:
        if it["kind"] == "map" and it["key"] == "GroupRatio":
            models |= {m for m, gs in served.items() if it["entry"] in gs}
        elif it["kind"] == "map":
            models.add(it["entry"])
    rows = []
    for m in sorted(models, key=str.encode):
        if "default" not in served.get(m, set()):
            continue
        before, after = model_prices(tgt, m, profile), model_prices(final, m, profile)
        if not before or not after:
            continue
        for comp in after:
            b, a = before.get(comp), after[comp]
            if b is None or a != b:
                change = "" if not b else f"{(a - b) / b * 100:+.1f}%"
                rows.append(f"| `{m}` | {comp} | {usd(b) if b is not None else 'n/a'} | {usd(a)} | {change} |")
    earliest = (now + datetime.timedelta(days=days)).astimezone(TPE).strftime("%B %d, %Y")
    lines = [
        f"<!-- {nid}: DRAFT for the owner. Fill the placeholders, send it by email to every customer with an",
        "     AI account, then run: apporo_sync.py notice-sent " + nid + " --at <send time>. The increase is",
        f"     applied by the daily sync no earlier than {days} days after that time. -->",
        "",
        "Subject: Price change for Apporo AI Models, effective {EFFECTIVE_DATE}",
        "",
        "Hello,",
        "",
        f"We are writing to let you know that the prices of the AI models listed below will increase on "
        f"{{EFFECTIVE_DATE}}, at least {days} days after this email, as described in Section 3 of the AI Models "
        "Terms of Service. Prices are in points per million tokens (or per call), and 1 point = US$1. "
        "Prices of other models do not change.",
        "",
        "| Model | Price | Current (US$) | New (US$) | Change |",
        "|---|---|---:|---:|---:|",
        *(rows or ["| (no served model in the default group is affected) | | | | |"]),
        "",
        "Usage before {EFFECTIVE_DATE} is charged at the current prices. AI credit is not price-protected: from "
        "{EFFECTIVE_DATE}, usage is charged at the new prices. If you do not accept this increase, you can contact "
        "support before {EFFECTIVE_DATE} and ask us to move your unused AI credit back to your points wallet and to "
        "stop your use of the affected paid models (Section 4, item 5 of the AI Models Terms).",
        "",
        "Questions: {SUPPORT_CONTACT}",
        "",
        "APPORO UNION INC.",
        "",
        f"<!-- earliest {{EFFECTIVE_DATE}} if sent now: {earliest} (Asia/Taipei). Holds: "
        + ", ".join(it["hold_id"] for it in holds_items) + " -->",
    ]
    return "\n".join(lines) + "\n"


def open_notice(state, new_holds, tgt, profile, now, days):
    if not new_holds:
        return None
    day = now.astimezone(TPE).strftime("%Y%m%d")
    n = 1 + sum(1 for nid in state.data["notices"] if nid.startswith(f"N-{day}-"))
    nid = f"N-{day}-{n}"
    path = os.path.join(state.dir, "notices", nid + ".md")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    nc.write_private_new(path, draft_notice(nid, new_holds, tgt, profile, now, days))
    state.data["notices"][nid] = {"id": nid, "created_at": iso(now), "hold_ids": [it["hold_id"] for it in new_holds],
                                  "draft": path, "sent_at": None}
    for it in new_holds:
        state.data["holds"][it["hold_id"]]["notice_id"] = nid
    return nid


# ------------------------------------------------------------- alerts

class AlertSink:
    """Where alerts go. File + stdout is the default; e-mail is not decided yet
    (implement emit() in another sink and select it in make_sink)."""

    def emit(self, alerts, run):
        raise NotImplementedError


class FileStdoutSink(AlertSink):
    def __init__(self, directory, out=None):
        self.directory, self.out = directory, out or sys.stdout

    def emit(self, alerts, run):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        path = os.path.join(self.directory, run["started_at"][:10].replace("-", "") + ".jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            for al in alerts:
                f.write(json.dumps(dict(al, run=run["id"]), ensure_ascii=False, sort_keys=True) + "\n")
        for al in alerts:
            print(f"ALERT [{al['severity']}] {al['title']}" + (" (new)" if al.get("new") else ""), file=self.out)
            for line in al.get("details", []):
                print("      " + line, file=self.out)
        print(f"alerts: {len(alerts)} -> {path}", file=self.out)
        return path


def make_sink(a, state_dir):
    kind = getattr(a, "alert_sink", None) or os.environ.get("APPORO_SYNC_ALERT_SINK") or "file"
    if kind != "file":
        raise SyncError(f"alert sink {kind!r} is not implemented (only 'file': e-mail transport is not decided)")
    return FileStdoutSink(os.path.join(state_dir, "alerts"))


def collect_alerts(items, state, now, extra=()):
    """Alert records with first-seen tracking (an e-mail sink can send only new ones)."""
    out = []
    for it in items:
        if it["class"] == "alert" or it["alert"] or (it["class"] == "hold" and not it["apply"]):
            sev = "action" if it["class"] in ("alert", "hold") or it["alert"] else "info"
            title = ("HOLD " if it["class"] == "hold" else "") + it["subject"]
            details = [it["reason"]]
            if it["kind"] in ("opt", "map", "chan-field"):
                details.append(f"target {short(it.get('old'))} -> source {short(it.get('new'))}")
            if it.get("hold_id"):
                details.append(f"hold {it['hold_id']}")
            out.append({"severity": sev, "title": title, "details": details, "class": it["class"],
                        "hold_id": it.get("hold_id")})
    out += list(extra)
    seen = state.data.setdefault("alerts", {}) if state else {}
    for al in out:
        fp = nc.digest([al["title"], al.get("hold_id") or al["details"][:1]])[:16]
        rec = seen.get(fp)
        al["fingerprint"], al["new"] = fp, rec is None
        if state:
            seen[fp] = {"title": al["title"], "first_seen": rec["first_seen"] if rec else iso(now),
                        "last_seen": iso(now), "count": (rec["count"] if rec else 0) + 1}
    return out


def short(v, n=90):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = nc.redact(s)
    return s if len(s) <= n else s[:n] + f"... ({len(s)} chars)"


# ------------------------------------------------------------- writes

def option_write_order(keys):
    first = ["general_setting.custom_currency_exchange_rate", "general_setting.quota_display_type",
             "ModelRequestRateLimitSuccessCount", "ModelRequestRateLimitGroup", "ModelRequestRateLimitDurationMinutes",
             "ModelRequestRateLimitCount", "ModelRequestRateLimitEnabled"]
    plain = sorted((k for k in keys if k not in nc.PRICING_KEYS and k not in first and k != "ServerAddress"),
                   key=str.encode)
    return ([k for k in first if k in keys] + plain + [k for k in nc.PRICING_WRITE_ORDER if k in keys]
            + (["ServerAddress"] if "ServerAddress" in keys else []))


class Journal:
    def __init__(self, state_dir, command, now):
        d = os.path.join(state_dir, "journal")
        os.makedirs(d, mode=0o700, exist_ok=True)
        for n in range(1000):
            self.path = os.path.join(d, now.strftime("%Y%m%dT%H%M%SZ") + f"-{command}-{os.getpid()}-{n}.jsonl")
            try:
                self.f = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
                break
            except FileExistsError:
                continue
        else:
            raise SyncError("cannot create a journal file")

    def write(self, **rec):
        rec["at"] = iso(now_utc())
        self.f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def execute(client, a, profile, tgt, desired, chosen, keys, journal, out=None):
    """Write the chosen items: options (prices first), then channels. Stops at the first failure."""
    out = out or sys.stdout
    final = simulate(tgt, chosen, profile, desired)
    opt_keys = [k for k in final["options"] if final["options"][k] != tgt["options"].get(k)]
    for key in option_write_order(opt_keys):
        value = final["options"][key]
        journal.write(step="option", key=key, sha256=nc.sha256_text(value), length=len(value))
        ok, msg, _ = api(client, "PUT", "/api/option/", {"key": key, "value": value})
        journal.write(step="option-result", key=key, ok=ok, message=msg)
        print(f"{'ok  ' if ok else 'FAIL'} option {key} ({len(value)} chars)" + ("" if ok else f": {msg}"), file=out)
        if not ok:
            return False
    creates = sorted(it["channel"] for it in chosen if it["kind"] == "chan-create")
    for cid in creates:
        if not create_channel(client, a, profile, cid, desired, keys, journal, out):
            return False
    for cid in sorted(set(final["channels"]) - set(creates)):
        before, after = tgt["channels"].get(cid), final["channels"][cid]
        if before is None:
            continue
        body = {f: after[f] for f in nc.CHANNEL_COPY_FIELDS if after.get(f) != before.get(f)}
        rekey = any(it["kind"] == "chan-rekey" and it["channel"] == cid for it in chosen)
        if not body and not rekey:
            continue
        unsettable = [f for f, v in body.items() if v is None or (v == "" and f not in CHANNEL_POINTER_FIELDS)]
        body = {f: v for f, v in body.items() if f not in unsettable}
        if unsettable:
            print(f"WARN channel {cid}: rc.44 PUT cannot clear {unsettable} (null/empty): left as they are", file=out)
        journal.write(step="channel-update", channel=cid, fields=sorted(body), rekey=rekey)
        if not body and not rekey:
            continue
        req = dict(body, id=cid)
        if rekey:
            req["key"] = keys[desired["channel_slots"][cid]].reveal()
        ok, msg, _ = api(client, "PUT", "/api/channel/", req)
        req = None
        journal.write(step="channel-update-result", channel=cid, ok=ok, message=msg)
        print(f"{'ok  ' if ok else 'FAIL'} channel {cid} update {sorted(body)}" + (" + key" if rekey else "")
              + ("" if ok else f": {msg}"), file=out)
        if not ok:
            return False
    return True


PLACEHOLDER = {"name": "apporo-sync id placeholder (deleted at once)", "type": 62, "key": "placeholder-not-a-key",
               "base_url": "http://127.0.0.1:9", "models": "apporo-sync-id-placeholder",
               "group": "apporo-sync-id-placeholder", "status": 2}


def create_channel(client, a, profile, cid, desired, keys, journal, out):
    """Create managed channel `cid` with exactly that id (so abilities equal WOOW's)."""
    psql = psql_for(a, profile, "target")
    snap = nc.read_snapshot(psql, profile, key_hashes=True)
    if cid in snap["channels"]:
        print(f"FAIL channel {cid}: the id is taken by {snap['channels'][cid].get('name')!r}", file=out)
        return False
    seq = snap["seq"]
    next_id = seq["last_value"] + 1 if seq.get("is_called") else seq["last_value"]
    explicit = next_id > cid
    while not explicit and next_id < cid:
        journal.write(step="placeholder", expected_id=next_id)
        ok, msg, _ = api(client, "POST", "/api/channel/", {"mode": "single", "channel": PLACEHOLDER})
        if not ok:
            print(f"FAIL placeholder for id {next_id}: {msg}", file=out)
            return False
        snap = nc.read_snapshot(psql, profile, key_hashes=True)
        mine = [i for i, ch in snap["channels"].items() if ch.get("name") == PLACEHOLDER["name"]]
        for pid in mine:
            ok, msg, _ = api(client, "DELETE", f"/api/channel/{pid}")
            journal.write(step="placeholder-deleted", id=pid, ok=ok, message=msg)
            if not ok:
                print(f"FAIL deleting placeholder {pid}: {msg}", file=out)
                return False
        print(f"ok   id gap {mine}: placeholder created and deleted", file=out)
        snap = nc.read_snapshot(psql, profile, key_hashes=True)
        seq = snap["seq"]
        next_id = seq["last_value"] + 1 if seq.get("is_called") else seq["last_value"]
    fields = {f: v for f, v in desired["channels"][cid].items() if v is not None}
    body = dict(fields, key=keys[desired["channel_slots"][cid]].reveal())
    if explicit:
        body["id"] = cid
    journal.write(step="channel-create", channel=cid, explicit_id=explicit, fields=sorted(fields))
    ok, msg, _ = api(client, "POST", "/api/channel/", {"mode": "single", "channel": body})
    body = None
    journal.write(step="channel-create-result", channel=cid, ok=ok, message=msg)
    if not ok:
        print(f"FAIL channel {cid} create: {msg}", file=out)
        return False
    snap = nc.read_snapshot(psql, profile, key_hashes=True)
    got = snap["channels"].get(cid)
    if not got or got.get("name") != desired["channels"][cid].get("name"):
        print(f"FAIL channel {cid}: created, but not under id {cid}; reconcile by hand", file=out)
        return False
    print(f"ok   channel {cid} created ({len(_models(fields.get('models')))} models, group {fields.get('group')})",
          file=out)
    return True


# ------------------------------------------------------------- verify

def verify_report(src, tgt, desired, profile, keys, expected_items=()):
    """Checks of the target against the desired content. `expected_items`: held or
    alert-only differences that are allowed to remain (daily mode)."""
    rows = []
    allowed_opts = {it["key"] for it in expected_items if it.get("key") and it["kind"] in ("opt", "map", "alert")}
    allowed_chan = {(it["channel"], it.get("field") or "models") for it in expected_items
                    if it["kind"].startswith("chan") and it["kind"] != "chan-create"}
    allowed_chan |= {(it["channel"], "key") for it in expected_items if it["kind"] == "chan-rekey"}
    want, have, hashes = desired["options"], tgt["options"], tgt["option_sha256"]
    for key in sorted(set(want) | set(have), key=str.encode):
        cls = nc.option_key_class(key, profile)
        if cls in ("derived", "instance", "never", "invalid") or (key not in want and key in BOOT_KEYS):
            continue
        exp = nc.sha256_text(want[key]) if key in want else None
        got = hashes.get(key)
        ok = exp == got
        rows.append(("option", key, ok or key in allowed_opts, None if ok else
                     ("expected difference (held/alert)" if key in allowed_opts else f"sha256 {got} != {exp}")))
    sa = profile["target"]["server_address"]
    origins = (have.get("passkey.origins") or "").split(",")
    rows.append(("derived", "passkey.origins", sa in origins, None if sa in origins else f"{origins}"))
    rows.append(("derived", "passkey.rp_id", have.get("passkey.rp_id", "") == "", None))
    managed = set(desired["channels"])
    for cid in sorted(managed):
        d, t = desired["channels"][cid], tgt["channels"].get(cid)
        if t is None:
            rows.append(("channel", f"{cid}", False, "missing"))
            continue
        for f in nc.CHANNEL_COPY_FIELDS:
            ok = d.get(f) == t.get(f)
            exp_ok = ok or (cid, f) in allowed_chan
            rows.append(("channel", f"{cid}.{f}", exp_ok, None if ok else
                         ("expected difference" if exp_ok else f"{short(t.get(f), 60)} != {short(d.get(f), 60)}")))
        if keys is not None:
            ok = t.get("key_sha256_16") == keys[desired["channel_slots"][cid]].sha16()
            rows.append(("channel", f"{cid}.key", ok or (cid, "key") in allowed_chan,
                         None if ok else "key differs from the Apporo key Secret"))
        rows.append(("channel", f"{cid}.status", t.get("status") == 1, None if t.get("status") == 1
                     else f"status {t.get('status')}"))
    exp_ab = nc.expected_abilities({cid: tgt["channels"][cid] for cid in managed if cid in tgt["channels"]},
                                   {cid: tgt["channels"][cid].get("status") for cid in managed if cid in tgt["channels"]})
    got_ab = sorted((r for r in tgt["abilities"] if r["channel_id"] in managed),
                    key=lambda r: (r["group"].encode(), r["model"].encode(), r["channel_id"]))
    rows.append(("abilities", "rows derived from the target channels", exp_ab == got_ab,
                 None if exp_ab == got_ab else f"{len(got_ab)} rows, expected {len(exp_ab)}"))
    src_ids = {desired["source_ids"][cid]: cid for cid in managed}
    src_ab = [dict(r, channel_id=src_ids[r["channel_id"]]) for r in src["abilities"] if r["channel_id"] in src_ids]
    def route(rs):
        return sorted((r["group"], r["model"], r["channel_id"], r["priority"], r["weight"], r["tag"] or "") for r in rs)
    model_diff = any(f == "models" for _, f in allowed_chan)
    same = route(src_ab) == route(got_ab)
    rows.append(("abilities", "routes equal the source's for the mapped channels (ids kept)", same or model_diff,
                 None if same else ("expected difference (held/alert model change)" if model_diff else "differ")))
    full_same = nc.abilities_fingerprint(src_ab) == nc.abilities_fingerprint(got_ab)
    rows.append(("abilities", "rows byte-identical to the source's mapped rows (incl. enabled)",
                 full_same or model_diff or any(src["channels"].get(s, {}).get("status") != 1 for s in src_ids),
                 None if full_same else "differ (a channel status differs, or an expected model difference)"))
    fps = {
        "options_expected": nc.options_fingerprint(want),
        "options_target": nc.options_fingerprint(have),
        "abilities_target": nc.abilities_fingerprint(got_ab),
        "abilities_source_mapped": nc.abilities_fingerprint(src_ab),
        "channels_expected": {cid: nc.channel_fingerprint(desired["channels"][cid])[:16] for cid in sorted(managed)},
        "channels_target": {cid: nc.channel_fingerprint(tgt["channels"][cid])[:16]
                            for cid in sorted(managed) if cid in tgt["channels"]},
    }
    return rows, fps


# ------------------------------------------------------------- images

IMAGE_LEAF = re.compile(r"^(\s*)([A-Za-z0-9_]+):\s*(.*?)\s*$")


def yaml_images(path):
    """{path: value} of every image repository/tag/digest leaf (indent-based; no YAML library)."""
    out, stack = {}, []
    with open(path) as f:
        for raw in f:
            line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
            if not line.strip() or line.lstrip().startswith("-"):
                continue
            m = IMAGE_LEAF.match(line)
            if not m:
                continue
            indent, key, value = len(m.group(1)), m.group(2), m.group(3)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if value == "":
                stack.append((indent, key))
                continue
            dotted = ".".join([k for _, k in stack] + [key])
            if key in ("repository", "tag", "digest") and stack and stack[-1][1] == "image":
                out[dotted] = value.strip("'\"")
    return out


def image_alerts(repo, kube=None, live=False, profile=None):
    out = []
    woow, app = os.path.join(repo, "deploy", "woow-k3s.yaml"), os.path.join(repo, "deploy", "apporo-k3s.yaml")
    if not os.path.exists(app):
        out.append({"severity": "info", "title": "images: deploy/apporo-k3s.yaml is not in this checkout",
                    "details": ["update the checkout after the Apporo values PR is merged"]})
    else:
        w, p = yaml_images(woow), yaml_images(app)
        for k in sorted(set(w) | set(p)):
            if w.get(k) != p.get(k):
                out.append({"severity": "action", "title": f"images: {k} differs (WOOW {w.get(k)} / Apporo {p.get(k)})",
                            "details": ["open a PR that moves deploy/apporo-k3s.yaml to WOOW's pin (after WOOW prod "
                                        "runs it); the owner merges it, then helm upgrade"]})
    if live and kube and profile:
        def images(ns):
            r = subprocess.run(kube + ["-n", ns, "get", "deploy,statefulset,cronjob", "-o", "json"],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                return None
            got = {}
            for obj in json.loads(r.stdout or b"{}").get("items", []):
                spec = obj["spec"].get("jobTemplate", {}).get("spec", obj["spec"])
                pod = spec.get("template", {}).get("spec", {})
                for c in pod.get("containers", []) + pod.get("initContainers", []):
                    got[f"{obj['kind']}/{obj['metadata']['name']}/{c['name']}"] = c.get("image")
            return got
        w, p = images(profile["source"]["namespace"]), images(profile["target"]["namespace"])
        if w is None or p is None:
            out.append({"severity": "info", "title": "images: live comparison unavailable (kubectl get failed)",
                        "details": []})
        else:
            for k in sorted(set(w) & set(p)):
                if w[k] != p[k]:
                    out.append({"severity": "action", "title": f"images (live): {k} differs",
                                "details": [f"WOOW {w[k]}", f"Apporo {p[k]}"]})
    return out


# ------------------------------------------------------------- commands

def load(a):
    profile = nc.load_profile(a.profile)
    if getattr(a, "platform_pricing_module", None):
        profile["_platform_module"] = load_platform_module(a.platform_pricing_module)
    return profile


def plan_all(a, profile, bootstrap, keys=None):
    src = read_source(a, profile)
    tgt = read_target(a, profile)
    check_versions(src, tgt)
    desired, notes = nc.transform(src, profile)
    items = build_items(src, tgt, desired, notes, profile, keys, bootstrap)
    classify(items, tgt, profile, bootstrap, desired)
    return src, tgt, desired, items


def target_base(a, profile):
    """A context manager yielding the target origin: a fresh port-forward, or --target-base."""
    if a.target_base:
        return contextlib.nullcontext(a.target_base)
    t = profile["target"]
    return PortForward(kubectl_argv(a), t["namespace"], t.get("service", "newapi"), t.get("port", 3000))


def chosen_items(items):
    return [it for it in items if it["class"] == "auto" or (it["class"] == "hold" and it["apply"])]


def print_items(items, out=None):
    out = out or sys.stdout
    order = {"auto": 0, "hold": 1, "alert": 2}
    for it in sorted(items, key=lambda i: (order.get(i["class"], 3), i["subject"])):
        tag = {"auto": "AUTO ", "hold": "HOLD ", "alert": "ALERT"}.get(it["class"], "?    ")
        if it["class"] == "hold" and it["apply"]:
            tag = "HOLD+"
        extra = ""
        if it["kind"] in ("opt", "map", "chan-field"):
            extra = f"  {short(it.get('old'), 60)} -> {short(it.get('new'), 60)}"
        print(f"{tag} {it['subject']}: {it['reason']}{extra}", file=out)
    counts = {c: sum(1 for i in items if i["class"] == c) for c in ("auto", "hold", "alert")}
    print(f"plan: {counts['auto']} auto, {counts['hold']} hold ({sum(1 for i in items if i['apply'])} matured), "
          f"{counts['alert']} alert", file=out)


def cmd_export(a):
    profile = load(a)
    side = a.side
    snap = read_source(a, profile) if side == "source" else read_target(a, profile)
    n, fp = nc.options_fingerprint(snap["options"])
    ab = nc.abilities_fingerprint(snap["abilities"])
    print(f"{side} {profile[side]['namespace']}: version {snap['version']}, {len(snap['options'])} options "
          f"(portable {n}, sha256 {fp}), {len(snap['channels'])} channels, abilities {ab[0]} sha256 {ab[1]}")
    for k in ("ModelRatio", "ModelPrice", "CompletionRatio", "CacheRatio"):
        if k in snap["options"]:
            print(f"  {k}: {len(json.loads(snap['options'][k]))} entries, sha256 {snap['option_sha256'][k]}")
    for cid, ch in sorted(snap["channels"].items()):
        print(f"  channel {cid}: group {ch.get('group')}, {len(_models(ch.get('models')))} models, status "
              f"{ch.get('status')}, config {nc.channel_fingerprint(ch)[:12]}")
    if snap["skipped_keys"]:
        print(f"  not read (secret-looking or invalid): {sorted(snap['skipped_keys'])}")
    if a.out:
        real = os.path.realpath(a.out)
        if real.startswith(REPO + os.sep):
            raise SyncError("keep snapshots outside the repository")
        out = dict(snap, channels={str(k): v for k, v in snap["channels"].items()},
                   _meta={"side": side, "namespace": profile[side]["namespace"], "taken_at": iso(now_utc())})
        out["channels"] = {k: {f: x for f, x in v.items() if f not in ("key_sha256_16", "key_length")}
                           for k, v in out["channels"].items()}
        nc.write_private_new(a.out, json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(f"snapshot -> {a.out} (0600, no secrets)")
    return 0


def cmd_plan(a):
    profile = load(a)
    keys = channel_keys(a, profile) if a.check_keys else None
    src, tgt, desired, items = plan_all(a, profile, a.bootstrap, keys)
    state = None if a.bootstrap else State(a.state_dir, read_only=True)
    process_holds(items, state, now_utc(), profile.get("price_increase_notice_days", 7), persist=False)
    if not a.bootstrap:
        combined_gate(chosen_items(items), tgt, profile, desired)
    if a.json:
        print(json.dumps({"items": items}, ensure_ascii=False, indent=1, default=str))
    else:
        print_items(items)
    return 0


def run_apply(a, profile, bootstrap, out=None):
    out = out or sys.stdout
    now = now_utc()
    days = profile.get("price_increase_notice_days", 7)
    state = State(a.state_dir)
    try:
        keys = channel_keys(a, profile)
        src, tgt, desired, items = plan_all(a, profile, bootstrap, keys)
        if bootstrap and state.data.get("sync_started_at") and not a.force:
            raise SyncError("the daily sync already ran against this state: `replay` would skip the price-notice "
                            "rule. Use `apply`, or pass --force with a reason in --why")
        new_holds = process_holds(items, state, now, days, persist=not bootstrap)
        nid = open_notice(state, new_holds, tgt, profile, now, days) if not bootstrap else None
        if not bootstrap:
            combined_gate(chosen_items(items), tgt, profile, desired)
        chosen = chosen_items(items)
        print_items(items, out)
        ok = True
        if chosen:
            with target_base(a, profile) as base:
                client = make_client(a, profile, base)
                try:
                    guard_target(client, profile, tgt, bootstrap)
                    journal = Journal(a.state_dir, "replay" if bootstrap else "apply", now)
                    journal.write(step="start", items=len(chosen), why=getattr(a, "why", None))
                    try:
                        ok = execute(client, a, profile, tgt, desired, chosen, keys, journal, out)
                    finally:
                        journal.write(step="end", ok=ok)
                        journal.close()
                finally:
                    client.close()
            if ok:
                for it in chosen:
                    if it.get("hold_id") and it["hold_id"] in state.data["holds"]:
                        state.data["holds"][it["hold_id"]].update(state="applied", applied_at=iso(now_utc()))
        tgt2 = read_target(a, profile)
        items2 = build_items(src, tgt2, desired, [], profile, keys, bootstrap)
        classify(items2, tgt2, profile, bootstrap, desired)
        process_holds(items2, state, now, days, persist=False)
        leftover = chosen_items(items2) if ok else []
        for it in leftover:
            print(f"LEFT {it['subject']}: still differs after apply", file=out)
        if not bootstrap:
            state.data.setdefault("sync_started_at", iso(now))
        run = {"id": iso(now), "started_at": iso(now), "command": "replay" if bootstrap else "apply",
               "applied": len(chosen) if ok else None, "ok": ok and not leftover, "notice": nid}
        state.data["runs"] = (state.data.get("runs") or [])[-50:] + [run]
        state.save()
        return ok and not leftover, items, run, state, tgt2
    except BaseException:
        state.save()
        raise


def cmd_apply(a):
    profile = load(a)
    ok, items, run, state, _ = run_apply(a, profile, bootstrap=False)
    alerts = collect_alerts(items, state, now_utc())
    state.save()
    make_sink(a, a.state_dir).emit(alerts, run)
    return 0 if ok else 1


def cmd_replay(a):
    if not a.yes_bootstrap:
        raise SyncError("replay copies everything, price increases and platform-coupled settings included: only for a "
                        "fresh target before customers exist. Pass --yes-bootstrap")
    profile = load(a)
    ok, _, _, _, _ = run_apply(a, profile, bootstrap=True)
    print("replay: " + ("done; now run verify --strict" if ok else "FAILED; inspect the journal, then run it again"))
    return 0 if ok else 1


def cmd_verify(a):
    profile = load(a)
    keys = channel_keys(a, profile) if a.check_keys else None
    src = read_source(a, profile)
    tgt = read_target(a, profile)
    check_versions(src, tgt)
    desired, notes = nc.transform(src, profile)
    expected = []
    if not a.strict:
        items = build_items(src, tgt, desired, notes, profile, keys)
        classify(items, tgt, profile, False, desired)
        expected = [it for it in items if it["class"] in ("alert", "hold") and not it["apply"]]
    rows, fps = verify_report(src, tgt, desired, profile, keys, expected)
    failed = [r for r in rows if not r[2]]
    if a.json:
        print(json.dumps({"rows": rows, "fingerprints": fps, "failed": len(failed)}, ensure_ascii=False, indent=1))
    else:
        for section, name, ok, why in rows:
            if not ok or a.verbose or why:
                print(f"{'ok  ' if ok else 'FAIL'} {section} {name}" + (f": {why}" if why else ""))
        print(f"options   expected {fps['options_expected'][0]} rows sha256 {fps['options_expected'][1]}")
        print(f"options   target   {fps['options_target'][0]} rows sha256 {fps['options_target'][1]}")
        print(f"abilities target   {fps['abilities_target'][0]} rows sha256 {fps['abilities_target'][1]}")
        print(f"abilities source*  {fps['abilities_source_mapped'][0]} rows sha256 {fps['abilities_source_mapped'][1]}"
              "  (*source rows of the mapped channels)")
        print("channels  " + ", ".join(f"{cid}: {fps['channels_target'].get(cid, '-')}"
                                       f"{'' if fps['channels_target'].get(cid) == v else ' (expected ' + v + ')'}"
                                       for cid, v in fps["channels_expected"].items()))
        print(f"verify: {len(rows)} checks, {len(failed)} failed" + ("" if a.strict else
              f" ({len(expected)} held/alert differences allowed)"))
    return 1 if failed else 0


def cmd_daily(a):
    try:
        return _daily(a)
    except (SyncError, nc.ContentError, na.Fatal) as e:
        # A failed run is itself an alert (an e-mail sink must hear about it too).
        now = now_utc()
        run = {"id": iso(now), "started_at": iso(now), "command": "daily", "ok": False}
        try:
            make_sink(a, a.state_dir).emit([{"severity": "action", "title": "daily sync failed",
                                             "details": [str(e)[:300]], "new": True, "fingerprint": "daily-failed"}], run)
        except OSError:
            pass
        raise


def _daily(a):
    profile = load(a)
    now = now_utc()
    extra = image_alerts(REPO, kubectl_argv(a), a.live_images, profile)
    exp = read_annotation(kubectl_argv(a), profile["target"]["namespace"],
                          profile.get("auth", {}).get("pat_secret", "newapi-sync-pat"), "woowtech.io/expires-at")
    if exp and exp.isdigit():
        left = (int(exp) - now.timestamp()) / 86400
        if left < PAT_WARN_DAYS:
            extra.append({"severity": "action", "title": f"the sync PAT expires in {left:.0f} days",
                          "details": ["mint a new root PAT in the browser (scopes "
                                      + ", ".join(PAT_SCOPES) + ") and replace the Secret"]})
    if a.apply:
        ok, items, run, state, _ = run_apply(a, profile, bootstrap=False)
    else:
        state = State(a.state_dir)
        _, tgt, desired, items = plan_all(a, profile, False)
        new = process_holds(items, state, now, profile.get("price_increase_notice_days", 7), persist=True)
        combined_gate(chosen_items(items), tgt, profile, desired)
        nid = open_notice(state, new, tgt, profile, now, profile.get("price_increase_notice_days", 7))
        print_items(items)
        ok, run = True, {"id": iso(now), "started_at": iso(now), "command": "daily-report", "notice": nid, "ok": True}
        state.data["runs"] = (state.data.get("runs") or [])[-50:] + [run]
    alerts = collect_alerts(items, state, now, extra)
    if run.get("notice"):
        n = state.data["notices"][run["notice"]]
        alerts.append({"severity": "action", "title": f"price-increase notice {n['id']} drafted",
                       "details": [n["draft"], f"after sending: apporo_sync.py notice-sent {n['id']} --at <time>"],
                       "new": True, "fingerprint": n["id"]})
    state.save()
    make_sink(a, a.state_dir).emit(alerts, run)
    return 0 if ok else 1


def cmd_notice_sent(a):
    state = State(a.state_dir)
    n = state.data["notices"].get(a.notice_id)
    if not n:
        raise SyncError(f"no notice {a.notice_id}; see `holds`")
    at = parse_iso(a.at) if a.at else now_utc()
    if at > now_utc() + datetime.timedelta(minutes=5):
        raise SyncError("--at is in the future")
    if at < parse_iso(n["created_at"]):
        raise SyncError("--at is before the notice was drafted")
    days = None
    marked = []
    for hid in n["hold_ids"]:
        h = state.data["holds"].get(hid)
        if not h or h["state"] != "pending-notice":
            continue
        days = h.get("notice_days", 7)
        h.update(state="notified", notice_sent_at=iso(at), eligible_at=iso(at + datetime.timedelta(days=days)))
        marked.append(hid)
    n["sent_at"] = iso(at)
    state.save()
    print(f"notice {a.notice_id} sent at {iso(at)}: {len(marked)} hold(s) apply from "
          + (iso(at + datetime.timedelta(days=days)) if marked else "-") + " on")
    return 0


def cmd_holds(a):
    state = State(a.state_dir)
    for h in sorted(state.data["holds"].values(), key=lambda h: h["detected_at"]):
        print(f"{h['id']} {h['state']:<15} {'/'.join(str(x) for x in h['subject'] if x)}: "
              f"{short(h['old'], 40)} -> {short(h['new'], 40)}  notice {h.get('notice_id')} "
              f"sent {h.get('notice_sent_at')} eligible {h.get('eligible_at')}")
    for n in state.data["notices"].values():
        print(f"{n['id']} sent {n['sent_at']} holds {n['hold_ids']} draft {n['draft']}")
    return 0


def cmd_check_images(a):
    profile = load(a)
    alerts = image_alerts(REPO, kubectl_argv(a), a.live, profile)
    for al in alerts:
        print(f"{al['severity']:<6} {al['title']}")
        for d in al["details"]:
            print("       " + d)
    if not alerts:
        print("images: WOOW and Apporo pins are equal")
    return 1 if any(al["severity"] == "action" for al in alerts) else 0


def cmd_mint_sync_pat(a):
    """Bootstrap only (root has no 2FA yet): root PAT for the sync, straight into a Secret."""
    profile = load(a)
    ns = profile["target"]["namespace"]
    name = profile.get("auth", {}).get("pat_secret", "newapi-sync-pat")
    expires = int(time.time()) + PAT_DAYS * 86400
    with PortForward(kubectl_argv(a), ns) as base:
        a.auth = "password"
        client = make_client(a, profile, base)
        try:
            tgt = read_target(a, profile)
            guard_target(client, profile, tgt, bootstrap=True)
            creds = read_secret(kubectl_argv(a), ns, profile.get("auth", {}).get("password_secret",
                                                                                 "newapi-root-credentials"), "password")
            ok, msg, ans = api(client, "POST", "/api/verify", {
                "method": "password", "scope": "access_token.generate",
                "context": {"scopes": sorted(PAT_SCOPES), "expires_at": expires}, "password": creds.reveal()})
            creds = None
            if not ok:
                raise SyncError(f"password proof refused ({msg}): root may already have 2FA; mint the PAT in the browser")
            proof = (ans.get("data") or {}).get("proof_token")
            ok, msg, ans = api(client, "POST", "/api/user/access_tokens",
                               {"name": "apporo-sync-" + now_utc().strftime("%Y%m%d"), "scopes": sorted(PAT_SCOPES),
                                "expires_at": expires}, headers={"X-Security-Proof": proof or ""})
            if not ok:
                raise SyncError(f"PAT not created: {msg}")
            token = Secret(ans["data"]["token"])
            hint = (ans["data"].get("item") or {}).get("token_hint", "")
            ans = None
        finally:
            client.close()
    manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/part-of": "woow-k3s-newapi"},
                             "annotations": {"woowtech.io/expires-at": str(expires), "woowtech.io/hint": hint,
                                             "woowtech.io/scopes": ",".join(sorted(PAT_SCOPES))}},
                "data": {"token": base64.b64encode(token.reveal().encode()).decode()}}
    kube = kubectl_argv(a)
    exists = subprocess.run(kube + ["-n", ns, "get", "secret", name], capture_output=True).returncode == 0
    r = subprocess.run(kube + ["replace" if exists else "create", "-f", "-"], input=json.dumps(manifest).encode(),
                       capture_output=True)
    manifest = token = None
    if r.returncode != 0:
        raise SyncError(f"kubectl {'replace' if exists else 'create'} secret {ns}/{name} failed")
    print(f"secret {ns}/{name} written (PAT hint {hint}, scopes {', '.join(sorted(PAT_SCOPES))}, expires "
          f"{datetime.datetime.fromtimestamp(expires, TPE):%Y-%m-%d})")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="holds, notices, alerts, journal (0700)")
    ap.add_argument("--kubectl", help="kubectl command line (default $APPORO_SYNC_KUBECTL or kubectl)")
    ap.add_argument("--source-snapshot", help="use an `export --out` file as the source instead of reading it")
    ap.add_argument("--target-base", help="target origin if a port-forward is already running (127.0.0.1 only)")
    ap.add_argument("--auth", choices=("pat", "password"), default="pat",
                    help="target credential: Secret newapi-sync-pat (default) or newapi-root-credentials (before 2FA)")
    ap.add_argument("--token-file", help="0600 file with a root PAT instead of the Secret")
    ap.add_argument("--platform-pricing-module", help="the platform's real llm_pricing.py for the pricing gate "
                    "(default: the built-in port of 18.0.2.0.16)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export", help="read-only content and fingerprints of one side")
    p.add_argument("--side", choices=("source", "target"), default="source")
    p.add_argument("--out", help="write a 0600 JSON snapshot (no secrets) outside the repo")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("plan", help="read-only: what apply would do, classified")
    p.add_argument("--bootstrap", action="store_true", help="the replay plan (no classification)")
    p.add_argument("--check-keys", action="store_true", help="also compare channel keys with the key Secret")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply", help="apply auto items and matured holds; second run is a no-op")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("replay", help="bootstrap a fresh target (after /api/setup, before DNS and customers)")
    p.add_argument("--yes-bootstrap", action="store_true")
    p.add_argument("--force", action="store_true", help="even after the daily sync started (needs --why)")
    p.add_argument("--why", help="journaled reason for --force")
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("verify", help="read-only SQL fingerprints of the target vs the transform")
    p.add_argument("--strict", action="store_true", help="no held or alert-only difference allowed")
    p.add_argument("--check-keys", action="store_true", help="also compare channel keys with the key Secret")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("daily", help="images + plan + holds + alerts (+ apply)")
    p.add_argument("--apply", action="store_true", help="write the target (the timer passes it)")
    p.add_argument("--live-images", action="store_true", help="also compare running images (kubectl get)")
    p.add_argument("--alert-sink", help="file (default); e-mail is not implemented yet")
    p.set_defaults(fn=cmd_daily)
    p = sub.add_parser("notice-sent", help="record that a drafted price notice was sent")
    p.add_argument("notice_id")
    p.add_argument("--at", help="send time, ISO-8601 with zone (default now)")
    p.set_defaults(fn=cmd_notice_sent)
    p = sub.add_parser("holds", help="list holds and notices")
    p.set_defaults(fn=cmd_holds)
    p = sub.add_parser("check-images", help="deploy/woow-k3s.yaml vs deploy/apporo-k3s.yaml image pins")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_check_images)
    p = sub.add_parser("mint-sync-pat", help="bootstrap: root PAT for the sync into the Secret (root without 2FA)")
    p.set_defaults(fn=cmd_mint_sync_pat)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.target_base:
        u = urllib.parse.urlsplit(a.target_base)
        if u.scheme != "http" or u.hostname not in na.LOOPBACK_HOSTS or u.path not in ("", "/"):
            print("error: --target-base must be http://127.0.0.1:<port> (a port-forward)", file=sys.stderr)
            return 2
        a.target_base = f"http://{u.netloc}"
    for attr in ("check_keys", "force", "why", "live_images", "alert_sink"):
        if not hasattr(a, attr):
            setattr(a, attr, None)
    try:
        return a.fn(a)
    except (SyncError, nc.ContentError, na.Fatal) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {getattr(e, 'filename', '') or ''} {e.strerror or e}", file=sys.stderr)
        return 2
    finally:
        while _OPEN_STATES:
            _OPEN_STATES.pop().close()


if __name__ == "__main__":
    sys.modules.setdefault("apporo_sync", sys.modules[__name__])
    sys.exit(main())
