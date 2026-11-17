#!/usr/bin/env python3
"""Apporo New API content replay and daily sync (WOOW new-api -> new-api-apporo).

The Apporo New API (namespace new-api-apporo, https://newapi.apporo.ai) carries
the same content as WOOW production (namespace new-api), with these
differences (deploy/apporo-sync/profile.json, docs/APPORO-SYNC.md):

  - ModelRatio and ModelPrice divided by 30 (float64, stored exactly as rc.44
    stores them); the other ratio maps are copied unchanged. Billing
    expressions (billing_setting.*) are real US$ prices that the 30:1 rule
    cannot reach: replay refuses them and the daily sync only alerts;
  - channel 2 (WOOW's internal ChatGPT subscription test route) is not copied;
    channels 1, 3, 4 keep their ids; their keys are Apporo's own LiteLLM keys
    from the Secret new-api-apporo/newapi-upstream-keys (never printed);
  - ServerAddress, the English UserUsableGroups labels and the currency
    symbol "$" are Apporo's own; instance rows (passkey.*,
    LegacyAccessTokenRetireAt) are never copied.

Reads are read-only SQL through `kubectl exec ... psql` (scripts/newapi_content.py:
whitelisted statement shape, no channel key, no user/token/log table). Writes go
only to the target, through the New API admin API over a `kubectl port-forward`,
as root: a PAT from the Secret newapi-sync-pat (key token), or before root has
2FA the password in newapi-root-credentials.

Commands:
  export   [--side source|target] [--out FILE]   read-only; fingerprints, optional 0600 snapshot
  plan     [--bootstrap] [--json]                read-only; what apply would do, classified
  apply                                          daily rules: auto items, matured holds
  replay   --yes-bootstrap                       fresh target after /api/setup: copy everything
  verify   [--strict] [--json]                   read-only SQL fingerprints of the target
  daily    [--apply]                             images + apply + alerts (the timer runs this)
  notice-sent NOTICE_ID --effective YYYY-MM-DD [--at ISO-8601]
                                                 the owner sent a price-increase notice
  holds                                          list holds and notices
  check-images [--live]                          deploy/woow-k3s.yaml vs deploy/apporo-k3s.yaml
  mint-sync-pat                                  bootstrap only: root PAT into newapi-sync-pat
  failure-alert [--reason TEXT]                  record a failed unit run as an alert (systemd OnFailure)

Daily classification (docs/APPORO-SYNC.md). A model is "offered" when any
channel of the target routes it, enabled or not (rc.44 disables and re-enables
channels by itself); the state directory remembers the models offered in the
last served_history_days days.
  auto   new models, price decreases, non-price option changes, model-list
         changes of the paid channels, removed models (plus an alert),
         Apporo-owned settings, channel creation and key rotation, any price
         change of a model not offered now, after this run or recently, and
         the first price of a model offered for the first time;
  hold   anything that raises the effective price of a model offered now or
         recently that is still offered after this run: drafted English
         notice; applied only from the effective date the owner records with
         notice-sent (at least 7 days after sending);
  alert  changes that need a platform or values change, billing expressions,
         changes that would make the platform's pricing validation fail, and
         (severity info, "kept") increases or price removals of a model that
         is taken off now or was recently: kept until the window ends, so a
         re-listing cannot skip the notice.

Writes run in an order that never leaves an offered model without its price:
options and new or changed prices, then channels, then removed price entries.

Exit status: 0 ok, 1 a write or check failed, 2 usage, target or connection error.
"""
import argparse
import base64
import contextlib
import datetime
import decimal
import fcntl
import hashlib
import http.client
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
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
# "Anywhere on Earth": a notice's effective date has begun for every customer
# once it is 00:00 at UTC-12. Increases apply from then, never earlier.
AOE = datetime.timezone(datetime.timedelta(hours=-12))
BACKDATE_LIMIT = datetime.timedelta(hours=24)

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
# Prices whose direction cannot be read from the value (or that are absolute
# vendor prices the 30:1 rule never reaches): a person decides.
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
SECRET_WRITER = None       # (namespace, name, manifest) -> (ok, message)


class SyncError(Exception):
    """Usage, target, connection or safety error (exit 2)."""


class Terminated(SyncError):
    """SIGTERM (systemd stop or TimeoutStartSec): finally blocks and the failure alert still run."""


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


def parse_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        raise SyncError(f"not a date (YYYY-MM-DD): {text!r}") from None


def effective_start(day):
    """The moment a notice's effective date has begun everywhere (00:00 at UTC-12)."""
    return datetime.datetime.combine(day, datetime.time(0), AOE).astimezone(datetime.timezone.utc)


def earliest_effective(sent, days):
    """The first effective date allowed for a notice sent at `sent` (at least `days` days later, everywhere)."""
    target = sent + datetime.timedelta(days=days)
    day = target.astimezone(AOE).date()
    if effective_start(day) < target:
        day += datetime.timedelta(days=1)
    return day


def notice_days(profile):
    return max(nc.MIN_NOTICE_DAYS, profile.get("price_increase_notice_days", nc.MIN_NOTICE_DAYS))


def history_days(profile):
    return profile.get("served_history_days", nc.DEFAULT_HISTORY_DAYS)


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
    """The annotation's value ('' when unset), or None when the Secret could not be read."""
    if ANNOTATION_READER is not None:
        return ANNOTATION_READER(namespace, name, annotation)
    path = "{.metadata.annotations." + annotation.replace(".", "\\.") + "}"
    try:
        r = run(kube + ["-n", namespace, "get", "secret", name, "-o", "jsonpath=" + path], capture_output=True,
                timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout or b"").decode("utf-8", "replace").strip() if r.returncode == 0 else None


def write_secret(kube, namespace, name, manifest):
    """Create or replace a Secret from a manifest on stdin: (ok, message). Never prints the manifest."""
    if SECRET_WRITER is not None:
        return SECRET_WRITER(namespace, name, manifest)
    try:
        exists = subprocess.run(kube + ["-n", namespace, "get", "secret", name], capture_output=True,
                                timeout=60).returncode == 0
        verb = "replace" if exists else "create"
        r = subprocess.run(kube + [verb, "-f", "-"], input=json.dumps(manifest).encode(), capture_output=True,
                           timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"kubectl did not finish ({e.__class__.__name__})"
    if r.returncode != 0:
        return False, f"kubectl {verb} exit {r.returncode}"
    return True, verb + "d"


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
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, na.Fatal):
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
        for cid, ch in snap["channels"].items():   # snapshots older than the current checks
            bad = nc.channel_unsafe_fields(ch)
            if bad and cid not in snap["unsafe_channels"]:
                snap["unsafe_channels"][cid] = bad
                snap["channels"][cid] = {f: ("[redacted]" if f in bad else v) for f, v in ch.items()}
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


def refuse_woow_database(tgt, profile):
    """Defence in depth before any write: the target database must not carry a WOOW ServerAddress."""
    sa = (tgt["options"].get("ServerAddress") or "").rstrip("/")
    if sa in WOOW_ORIGINS or profile["target"]["server_address"].rstrip("/") in WOOW_ORIGINS:
        raise SyncError(f"refusing to write to a WOOW site ({sa or profile['target']['server_address']})")


def target_is_fresh(tgt, desired):
    """A target nobody has synced yet: no ServerAddress of its own and none of the managed channels."""
    sa = (tgt["options"].get("ServerAddress") or "").rstrip("/")
    return sa in FRESH_ORIGINS and not (set(desired["channels"]) & set(tgt["channels"]))


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
        except (ValueError, OSError, http.client.HTTPException):
            answer = {"message": f"HTTP {e.code}"}
        if not isinstance(answer, dict):
            answer = {"message": f"HTTP {e.code}"}
        answer.setdefault("success", False)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        # IncompleteRead, BadStatusLine, RemoteDisconnected: a port-forward that broke mid-answer.
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
                              "(auto-disabled by upstream errors, or by hand); the sync does not change status. "
                              "Its models still count as offered (price increases stay held)",
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

def routed_models(state):
    """{model: groups} of every ability row, enabled or not: a channel disabled by
    upstream errors (or by hand) still offers its models, and rc.44 re-enables it."""
    out = {}
    for a in state["abilities"]:
        out.setdefault(a["model"], set()).add(a["group"])
    return out


def merge_offered(*maps):
    out = {}
    for m in maps:
        for model, groups in (m or {}).items():
            out.setdefault(model, set()).update(groups)
    return out


def offered_history(state, now, days):
    """{model: groups} the target offered in the last `days` days (the state directory's record)."""
    if state is None:
        return {}
    cutoff = now - datetime.timedelta(days=days)
    out = {}
    for model, rec in (state.data.get("served") or {}).items():
        try:
            if parse_iso(rec["last_seen"]) >= cutoff:
                out[model] = set(rec.get("groups") or ())
        except (KeyError, TypeError, AttributeError, SyncError):
            continue
    return out


def record_offered(state, snap, now, days):
    """Remember the models `snap` offers (any channel status); forget those not seen for `days` days."""
    served = state.data.setdefault("served", {})
    for model, groups in routed_models(snap).items():
        rec = served.get(model) if isinstance(served.get(model), dict) else {"first_seen": iso(now), "groups": []}
        rec["groups"] = sorted(set(rec.get("groups") or ()) | groups)
        rec["last_seen"] = iso(now)
        served[model] = rec
    cutoff = now - datetime.timedelta(days=days)
    for model in list(served):
        try:
            keep = parse_iso(served[model]["last_seen"]) >= cutoff
        except (KeyError, TypeError, AttributeError, SyncError):
            keep = False
        if not keep:
            del served[model]


def history_alerts(state, tgt, desired):
    if (state.data.get("served") or state.data.get("runs")) or target_is_fresh(tgt, desired):
        return []
    return [{"severity": "info", "title": "no record of offered models in this state directory",
             "details": [f"{state.dir}: models taken off the target before today are not remembered, so re-listing "
                         "one of them at a higher price would not be held; the record starts with this run"]}]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def classify(items, tgt, profile, bootstrap=False, desired=None, history=None):
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
            continue  # second pass: needs what the channel changes leave offered
        elif k == "chan-create":
            it["class"], it["reason"] = "auto", "mapped channel missing on the target"
        elif k == "chan-field":
            if it["new"] is None or (it["new"] == "" and it["field"] not in CHANNEL_POINTER_FIELDS):
                it["class"], it["reason"] = "alert", "rc.44's PUT /api/channel/ cannot clear this field (null, or an " \
                                                     "empty plain string): change it by hand in the admin UI"
            elif it["field"] in _owned_fields(it["channel"], profile):
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
            left = ((desired or {}).get("channels", {}).get(it["channel"]) or {}).get("models")
            if groups & set(profile.get("free_groups", ("free",))):
                it["class"], it["reason"] = "alert", "free-trial channel: proxy.freeModels and the platform's " \
                                                     "FREE_MODELS must change with it"
            elif k == "chan-model-add":
                it["class"], it["reason"] = "auto", "new model route"
            elif not left:
                it["class"], it["reason"] = "alert", "the source channel has no models left: rc.44 cannot store an " \
                                                     "empty model list; disable or delete the channel by hand"
            else:
                it["class"], it["reason"], it["alert"] = "auto", "model removed at the source", True
        elif k == "chan-rekey":
            it["class"], it["reason"] = "auto", "channel key differs from the Apporo key Secret"
    if bootstrap:
        return items
    # Who can be charged a price: models routed now (any channel status), routed
    # after this run's route changes, or offered in the last served_history_days days.
    before = routed_models(tgt)
    routes = [it for it in items if it["class"] == "auto" and it["kind"] in ("chan-create", "chan-model-add",
                                                                             "chan-model-remove", "chan-field")]
    after = routed_models(simulate(tgt, routes, profile, desired))
    hist = history or {}
    ctx = {"before": before, "after": after, "hist": hist, "days": history_days(profile)}
    for name in ("before", "after", "hist"):
        ctx["groups_" + name] = {g for gs in ctx[name].values() for g in gs}
    for it in items:
        if it["kind"] == "map":
            classify_map(it, ctx)
    expression_gate(items, tgt, profile, desired)
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
        it["class"], it["reason"] = "alert", "a price whose direction cannot be read, or an absolute vendor price " \
                                             "the 30:1 rule never reaches: decide by hand"
    elif key in nc.BILLING_MAPS:
        it["class"], it["reason"] = "alert", "billing expressions are real US$ prices (never divided by 30): " \
                                             "decide by hand"
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


def _direction(key, o, n):
    """'decrease', 'same', 'increase' or 'unknown' for one price-map entry."""
    if key == "ModelPrice" and (o is None) != (n is None):
        return "unknown"        # per-call <-> per-token billing
    if key == "CompletionRatio" and (o is None) != (n is None):
        return "unknown"        # rc.44's built-in completion rule
    old_v = nc.ABSENT_DEFAULTS.get(key) if o is None else o
    new_v = nc.ABSENT_DEFAULTS.get(key) if n is None else n
    if not (_num(old_v) and _num(new_v)):
        return "unknown"
    return "increase" if new_v > old_v else "decrease" if new_v < old_v else "same"


def classify_map(it, ctx):
    key, entry, o, n = it["key"], it["entry"], it["old"], it["new"]
    if key in nc.BILLING_MAPS:
        it["class"], it["reason"] = "alert", "billing expressions are real US$ prices (rc.44: no ratio conversion), " \
                                             "so the 30:1 rule cannot be applied to them: decide by hand"
        return
    if key == "GroupRatio" and entry in PLATFORM_COUPLED_GROUPS:
        it["class"], it["reason"] = "alert", "GroupRatio default/free: the platform's pricing profile pins them"
        return
    if key == "GroupGroupRatio":
        it["class"], it["reason"] = "alert", "GroupGroupRatio must stay {} (platform invariant)"
        return
    if key == "TopupGroupRatio":
        it["class"], it["reason"] = "auto", "top-up ratio (New API payments are off)"
        return
    pre = "groups_" if key == "GroupRatio" else ""
    now_, after, hist = (entry in ctx[pre + "before"], entry in ctx[pre + "after"], entry in ctx[pre + "hist"])
    days = ctx["days"]
    if not (now_ or after or hist):
        it["class"], it["reason"] = "auto", f"not offered on the target (now, after this run or in the last {days} " \
                                            "days): no customer price changes"
        return
    if after and key in ("ModelRatio", "ModelPrice") and _num(n) and n == 0 and not (_num(o) and o == 0):
        it["class"], it["reason"] = "alert", "the price drops to 0: free for every key at any balance; confirm by hand"
        return
    if after and key == "ModelRatio" and n is None:
        it["class"], it["reason"] = "alert", "an offered model loses its price (ModelRatio): rc.44 refuses it (or " \
                                             "bills a built-in US$ expression): decide by hand"
        return
    direction = "first" if key == "ModelRatio" and o is None else _direction(key, o, n)
    if direction in ("decrease", "same"):
        it["class"], it["reason"] = "auto", "price decrease" if direction == "decrease" else "same effective value"
        return
    if direction == "first":
        it["class"], it["reason"] = "auto", "a model gets its first ratio (rc.44 refused it before)"
        return
    if not after:
        # Taken off now, or recently: keep the old value so that a re-listing within
        # the window compares against it (and needs a notice to go up).
        it["class"], it["severity"], it["deferred"] = "alert", "info", True
        it["reason"] = f"kept: the model is not offered after this run but was offered in the last {days} days; a " \
                       "re-listing must not skip the price notice (applied once the window has passed)"
        return
    if not (now_ or hist):
        it["class"], it["reason"] = "auto", "first listing on the target: no customer paid the old price"
        return
    it["class"] = "hold"
    it["reason"] = "price increase" if direction == "increase" else {
        "ModelPrice": "switches between per-call and per-token billing: direction unknown",
        "CompletionRatio": "completion ratio falls back to rc.44's built-in rule: direction unknown",
    }.get(key, "not comparable: direction unknown")


def expression_gate(items, tgt, profile, desired):
    """A model rc.44 bills with a billing expression (real US$ per 1M tokens; never
    divided by 30) must not be offered by the sync: alert, and do not route it."""
    chosen = [it for it in items if it["class"] == "auto"]
    final = simulate(tgt, chosen, profile, desired)
    new_routes = {}
    for it in chosen:
        if it["kind"] == "chan-model-add":
            new_routes.setdefault(it["model"], []).append(it)
        elif it["kind"] == "chan-create" and desired:
            for m in _models(desired["channels"][it["channel"]].get("models")):
                new_routes.setdefault(m, []).append(it)
    for m in sorted(routed_models(final), key=str.encode):
        here = nc.billed_by_expression(final["options"], m)
        # a new route also follows the source's billing: WOOW bills it by expression, Apporo cannot price it
        there = desired is not None and nc.billed_by_expression(desired["options"], m)
        reason = (f"{m} is billed by a billing expression (real US$ prices: an rc.44 built-in, or billing_mode "
                  "tiered_expr); the 30:1 rule cannot be applied: give it a ModelRatio/ModelPrice or route it by hand")
        if m in new_routes and (here or there):
            for it in new_routes[m]:
                it["class"], it["reason"] = "alert", reason
        elif here:
            items.append(item("alert", "alert", reason, subject_text=f"billing-expression: model {m}"))


def bootstrap_blockers(desired):
    """What replay must never copy: billing expressions and models billed by one (real US$ prices)."""
    out = []
    for key, n in nc.billing_entries(desired["options"]).items():
        out.append(item("alert", "alert", f"{n} billing expression entr{'y' if n == 1 else 'ies'} (real US$ prices, "
                        "never divided by 30): replay refuses to copy them", subject_text=f"billing-expression: {key}",
                        blocker=True))
    for m in sorted(routed_models({"abilities": nc.expected_abilities(desired["channels"])}), key=str.encode):
        if nc.billed_by_expression(desired["options"], m):
            out.append(item("alert", "alert", "billed by a billing expression (real US$ prices): replay refuses to "
                            "route it", subject_text=f"billing-expression: model {m}", blocker=True))
    return out


# --------------------------------------------- the platform's pricing gate

def _dec(value):
    if isinstance(value, bool) or type(value) not in (int, float, str, decimal.Decimal) or len(str(value)) > 64:
        raise ValueError("pricing_number")
    d = decimal.Decimal(str(value))
    if not d.is_finite() or d < 0 or len(d.as_tuple().digits) > 24 or (d and not -18 <= d.adjusted() <= 12):
        raise ValueError("pricing_number")
    return d


def pricing_payload(state, profile, include_disabled=False):
    """What GET /api/pricing answers the platform's service account (group default), from DB state.
    include_disabled: as if every channel were enabled (rc.44 re-enables channels by itself)."""
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
        if a["enabled"] or include_disabled:
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


def gate_verdict(state, profile):
    """The platform's verdict as if every channel were enabled (a disabled channel can come back any time)."""
    return platform_verdict(pricing_payload(state, profile, include_disabled=True), profile)


def platform_gate(items, tgt, profile, desired=None):
    """Turn an auto/hold item into an alert when it alone (or all autos together) would make
    the platform's pricing validation fail where it passes today."""
    if not profile.get("platform"):
        return
    base = gate_verdict(tgt, profile)
    if base is not None:
        items.append(item("alert", "alert", f"the platform's pricing validation already fails on the target ({base}); "
                          "changes are not held back by this check until it passes",
                          subject_text="platform-pricing: target"))
        return
    relevant = [it for it in items if it["class"] in ("auto", "hold") and it["kind"] in (
        "opt", "map", "chan-create", "chan-model-add", "chan-model-remove", "chan-field")]
    for it in relevant:
        res = gate_verdict(simulate(tgt, [it], profile, desired), profile)
        if res is not None:
            it["class"], it["reason"] = "alert", f"would make the platform's pricing validation fail ({res})"
    combined_gate([it for it in relevant if it["class"] == "auto"], tgt, profile, desired)


def combined_gate(chosen, tgt, profile, desired=None):
    """All chosen items together must keep the platform's validation passing."""
    if not profile.get("platform") or not chosen:
        return True
    if gate_verdict(tgt, profile) is not None:
        return True
    res = gate_verdict(simulate(tgt, chosen, profile, desired), profile)
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
    read_only=True (plan, verify): no lock, no directory created, never saved."""

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
            try:
                with open(self.file) as f:
                    self.data = json.load(f)
            except ValueError:
                raise SyncError(f"{self.file} is not JSON: restore it from the journal or a backup") from None
            if not isinstance(self.data, dict) or self.data.get("schema") != 1:
                raise SyncError(f"{self.file}: unknown schema")
        else:
            self.data = {"schema": 1, "holds": {}, "notices": {}, "alerts": {}, "runs": [], "served": {}}

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
    """Match hold items with open holds; mark matured ones applicable; open new holds.
    A kept (deferred) item keeps a matching hold alive but never opens or applies one."""
    open_holds = state.open_holds() if state else {}
    used, new = set(), []
    for it in items:
        deferred = it["class"] == "alert" and it.get("deferred")
        if it["class"] != "hold" and not deferred:
            continue
        match = next((h for h in open_holds.values() if h["subject"] == hold_subject(it)), None)
        if match and match["old"] == it["old"] and match["new"] == it["new"]:
            used.add(match["id"])
            it["hold_id"] = match["id"]
            if deferred:
                it["reason"] += f"; hold {match['id']} waits until the model is offered again"
                continue
            due = match.get("eligible_at")
            if match["state"] == "notified" and due and parse_iso(due) <= now:
                it["apply"] = True
                it["reason"] += f"; notice sent {match['notice_sent_at']}, effective {due}"
            else:
                it["reason"] += "; held: " + ("waiting for notice-sent" if match["state"] == "pending-notice"
                                               else f"applies from {due}")
            continue
        if match:
            used.add(match["id"])
            if persist:
                match.update(state="superseded", closed_at=iso(now), close_reason="the desired value changed again")
        if deferred:
            continue
        day = now.astimezone(TPE).strftime("%Y%m%d")
        hid = f"H-{day}-" + nc.digest([hold_subject(it), it["old"], it["new"]])[:8]
        it["hold_id"] = hid
        it["reason"] += "; new hold: draft notice, then notice-sent"
        new.append(it)
        if persist:
            state.data["holds"][hid] = {"id": hid, "subject": hold_subject(it), "old": it["old"], "new": it["new"],
                                        "reason": it["reason"], "detected_at": iso(now), "state": "pending-notice",
                                        "notice_id": None, "notice_sent_at": None, "eligible_at": None,
                                        "effective_date": None, "applied_at": None, "closed_at": None,
                                        "close_reason": None, "notice_days": days}
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


def draft_notice(nid, holds_items, base, profile, now, days, offered):
    """English customer notice for the held increases: US$ prices before and after, per model.
    `base` is the target after this run's automatic writes; `offered` maps models to groups
    (offered now, after this run or recently)."""
    final = simulate(base, holds_items, profile)
    models = set()
    for it in holds_items:
        if it["kind"] == "map" and it["key"] == "GroupRatio":
            models |= {m for m, gs in offered.items() if it["entry"] in gs}
        elif it["kind"] == "map":
            models.add(it["entry"])
    rows, by_hand = [], []
    for m in sorted(models, key=str.encode):
        if "default" not in offered.get(m, set()):
            continue
        if nc.billed_by_expression(base["options"], m) or nc.billed_by_expression(final["options"], m):
            by_hand.append(m)
            rows.append(f"| `{m}` | billing expression | (by hand) | (by hand) | |")
            continue
        before, after = model_prices(base, m, profile), model_prices(final, m, profile)
        if not before or not after:
            by_hand.append(m)
            rows.append(f"| `{m}` | (price not computable) | (by hand) | (by hand) | |")
            continue
        for comp in after:
            b, a = before.get(comp), after[comp]
            if b is None or a != b:
                change = "" if not b else f"{(a - b) / b * 100:+.1f}%"
                rows.append(f"| `{m}` | {comp} | {usd(b) if b is not None else 'n/a'} | {usd(a)} | {change} |")
    if not rows:
        subjects = ", ".join(it["subject"] for it in holds_items)
        rows = [f"| (BY HAND: the held change(s) {subjects} do not map to a per-model price in the default "
                "group; write this table yourself) | | | | |"]
    earliest = earliest_effective(now, days)
    lines = [
        f"<!-- {nid}: DRAFT for the owner. Fill the placeholders, send it by email to every customer with an",
        "     AI account, then run:",
        f"       apporo_sync.py notice-sent {nid} --effective <the EFFECTIVE_DATE you wrote, YYYY-MM-DD> --at <send time>",
        f"     notice-sent refuses an effective date earlier than {days} days after sending. The daily sync applies",
        "     the increase from the first run after the effective date has begun everywhere (00:00 UTC-12).",
        *([f"     BY HAND: {', '.join(by_hand)} (billing expression or no computable price)."] if by_hand else []),
        "-->",
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
        *rows,
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
        f"<!-- earliest {{EFFECTIVE_DATE}} if sent now: {earliest:%B} {earliest.day}, {earliest.year} "
        f"({earliest.isoformat()}). Holds: " + ", ".join(it["hold_id"] for it in holds_items) + " -->",
    ]
    return "\n".join(lines) + "\n"


def open_notice(state, new_holds, base, profile, now, days, offered):
    if not new_holds:
        return None
    day = now.astimezone(TPE).strftime("%Y%m%d")
    n = 1 + sum(1 for nid in state.data["notices"] if nid.startswith(f"N-{day}-"))
    nid = f"N-{day}-{n}"
    path = os.path.join(state.dir, "notices", nid + ".md")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    nc.write_private_new(path, draft_notice(nid, new_holds, base, profile, now, days, offered))
    state.data["notices"][nid] = {"id": nid, "created_at": iso(now), "hold_ids": [it["hold_id"] for it in new_holds],
                                  "draft": path, "sent_at": None, "effective_date": None}
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
            sev = it.get("severity") or ("action" if it["class"] in ("alert", "hold") or it["alert"] else "info")
            title = ("HOLD " if it["class"] == "hold" else "KEPT " if it.get("deferred") else "") + it["subject"]
            details = [it["reason"]]
            if it["kind"] in ("opt", "map", "chan-field"):
                details.append(f"target {show(it, 'old')} -> source {show(it, 'new')}")
            if it.get("hold_id"):
                details.append(f"hold {it['hold_id']}")
            out.append({"severity": sev, "title": title, "details": details, "class": it["class"],
                        "hold_id": it.get("hold_id")})
    out += list(extra)
    seen = state.data.setdefault("alerts", {}) if state else {}
    for al in out:
        al.setdefault("details", [])
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


def masked(v):
    """A channel value that may hold a credential: only its sha256 prefix and length."""
    if v is None:
        return "null"
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return f"sha256:{nc.sha256_text(s)[:12]} ({len(s)} chars)"


def show(it, which, n=90):
    """old/new of an item for people: channel fields outside the printable list are masked."""
    if it["kind"] == "chan-field" and it.get("field") not in nc.CHANNEL_PRINTABLE_FIELDS:
        return masked(it.get(which))
    return short(it.get(which), n)


def public_item(it):
    """An item for `plan --json` (masked like the text output)."""
    out = dict(it)
    if it["kind"] == "chan-field" and it.get("field") not in nc.CHANNEL_PRINTABLE_FIELDS:
        out["old"], out["new"] = masked(it.get("old")), masked(it.get("new"))
    return out


def apply_failure_alert(run):
    """An action alert for a run whose writes failed or left differences (None when it succeeded)."""
    if run.get("ok"):
        return None
    if not run.get("write_ok", True):
        title = "apply failed at " + str(run.get("failed") or "an unknown step")[:160]
    else:
        title = "apply left differences that it should have written"
    return {"severity": "action", "title": title, "details": [
        f"run {run['id']} ({run.get('command')})",
        "written: " + (", ".join(run.get("written") or []) or "nothing")[:800],
        "still differs: " + (", ".join(run.get("left") or []) or "nothing")[:800],
        "the journal in the state directory lists every write; the next run retries what is left"]}


def pat_expiry_alerts(a, profile, now):
    if a.token_file or a.auth != "pat":
        return []
    exp = read_annotation(kubectl_argv(a), profile["target"]["namespace"],
                          profile.get("auth", {}).get("pat_secret", "newapi-sync-pat"), "woowtech.io/expires-at")
    if not exp or not exp.isdigit():
        return [{"severity": "info", "title": "the sync PAT's expiry is unknown",
                 "details": ["the Secret has no woowtech.io/expires-at annotation (unix seconds), or it could not "
                             "be read: the 14-day warning cannot work"]}]
    left = (int(exp) - now.timestamp()) / 86400
    if left < PAT_WARN_DAYS:
        return [{"severity": "action", "title": f"the sync PAT expires in {left:.0f} days",
                 "details": ["mint a new root PAT in the browser (scopes " + ", ".join(PAT_SCOPES)
                             + ") and replace the Secret"]}]
    return []


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


def write_options(client, before, after, journal, out, result):
    for key in option_write_order([k for k in after if after[k] != before.get(k)]):
        value = after[key]
        journal.write(step="option", key=key, sha256=nc.sha256_text(value), length=len(value))
        ok, msg, _ = api(client, "PUT", "/api/option/", {"key": key, "value": value})
        journal.write(step="option-result", key=key, ok=ok, message=msg)
        print(f"{'ok  ' if ok else 'FAIL'} option {key} ({len(value)} chars)" + ("" if ok else f": {msg}"), file=out)
        if not ok:
            result["failed"] = f"option {key}: {msg}"
            return False
        result["written"].append(f"option {key}")
    return True


def execute(client, a, profile, tgt, desired, chosen, keys, journal, out=None):
    """Write the chosen items so that an offered model never lacks its price, whatever step fails:
    A) options, new and changed prices; B) channels (create, routes, keys); C) removed price entries.
    Stops at the first failure. Returns {"ok", "written": [steps], "failed": step or None}."""
    out = out or sys.stdout
    result = {"ok": False, "written": [], "failed": None}
    late = {id(it) for it in chosen if it["kind"] == "map" and it["new"] is None}
    phase_a = simulate(tgt, [it for it in chosen if id(it) not in late], profile, desired)
    final = simulate(tgt, chosen, profile, desired)
    if not write_options(client, tgt["options"], phase_a["options"], journal, out, result):
        return result
    creates = sorted(it["channel"] for it in chosen if it["kind"] == "chan-create")
    for cid in creates:
        if not create_channel(client, a, profile, cid, desired, keys, journal, out):
            result["failed"] = f"channel {cid} create"
            return result
        result["written"].append(f"channel {cid} create")
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
        step = f"channel {cid} update {sorted(body)}" + (" + key" if rekey else "")
        print(f"{'ok  ' if ok else 'FAIL'} {step}" + ("" if ok else f": {msg}"), file=out)
        if not ok:
            result["failed"] = f"{step}: {msg}"
            return result
        result["written"].append(step)
    if not write_options(client, phase_a["options"], final["options"], journal, out, result):
        return result
    result["ok"] = True
    return result


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
            if f in nc.CHANNEL_PRINTABLE_FIELDS:
                diff = f"{short(t.get(f), 60)} != {short(d.get(f), 60)}"
            else:
                diff = f"{masked(t.get(f))} != {masked(d.get(f))}"
            rows.append(("channel", f"{cid}.{f}", exp_ok, None if ok else
                         ("expected difference" if exp_ok else diff)))
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
            try:
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
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
                return None
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


def plan_all(a, profile, bootstrap, keys=None, history=None):
    src = read_source(a, profile)
    tgt = read_target(a, profile)
    check_versions(src, tgt)
    desired, notes = nc.transform(src, profile)
    items = build_items(src, tgt, desired, notes, profile, keys, bootstrap)
    classify(items, tgt, profile, bootstrap, desired, history)
    if bootstrap:
        items += bootstrap_blockers(desired)
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
        elif it.get("deferred"):
            tag = "KEPT "
        extra = ""
        if it["kind"] in ("opt", "map", "chan-field"):
            extra = f"  {show(it, 'old', 60)} -> {show(it, 'new', 60)}"
        print(f"{tag} {it['subject']}: {it['reason']}{extra}", file=out)
    counts = {c: sum(1 for i in items if i["class"] == c) for c in ("auto", "hold", "alert")}
    kept = sum(1 for i in items if i.get("deferred"))
    print(f"plan: {counts['auto']} auto, {counts['hold']} hold ({sum(1 for i in items if i['apply'])} matured), "
          f"{counts['alert']} alert" + (f" ({kept} kept)" if kept else ""), file=out)


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
    if snap.get("unsafe_channels"):
        print(f"  channels with credential-looking fields (redacted, never copied): "
              f"{ {k: v for k, v in sorted(snap['unsafe_channels'].items())} }")
    if snap["skipped_keys"]:
        print(f"  not read (secret-looking or invalid): {sorted(snap['skipped_keys'])}")
    if a.out:
        real, repo = os.path.realpath(a.out), os.path.realpath(REPO)
        if real == repo or real.startswith(repo + os.sep):
            raise SyncError("keep snapshots outside the repository (its main branch is mirrored to a public GitHub)")
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
    now = now_utc()
    state = None if a.bootstrap else State(a.state_dir, read_only=True)
    hist = offered_history(state, now, history_days(profile))
    src, tgt, desired, items = plan_all(a, profile, a.bootstrap, keys, hist)
    process_holds(items, state, now, notice_days(profile), persist=False)
    if not a.bootstrap:
        combined_gate(chosen_items(items), tgt, profile, desired)
    if a.json:
        print(json.dumps({"items": [public_item(it) for it in items]}, ensure_ascii=False, indent=1, default=str))
    else:
        print_items(items)
    return 0


def daily_view(src, tgt, desired, profile, keys, history):
    """The hold and alert items the daily rules would leave alone (what a forced replay overrides)."""
    items = build_items(src, tgt, desired, [], profile, keys, False)
    classify(items, tgt, profile, False, desired, history)
    return [it for it in items if it["class"] in ("hold", "alert")]


def check_bootstrap(a, state, src, tgt, desired, items, profile, keys, history, out):
    """Replay copies everything at once: only for a fresh target, or with --force and a reason."""
    blockers = [it for it in items if it.get("blocker")]
    if blockers:
        raise SyncError("replay refused: " + "; ".join(f"{it['subject']} ({it['reason']})" for it in blockers))
    fresh = target_is_fresh(tgt, desired)
    started = state.data.get("sync_started_at")
    if fresh and not started:
        return []
    if not a.force:
        why = ("the daily sync already ran against this state" if started else
               "the target is not a fresh install (its ServerAddress is set, or managed channels exist)")
        raise SyncError(f"{why}: `replay` would skip the price-notice rule. Use `apply` (the daily rules), or "
                        "`plan --bootstrap` to check a finished replay; to resume a failed bootstrap before any "
                        "customer exists, pass --force --why '<reason>'")
    overridden = daily_view(src, tgt, desired, profile, keys, history)
    for it in overridden:
        print(f"OVERRIDE {it['class']} {it['subject']}: {it['reason']}", file=out)
    return overridden


def run_apply(a, profile, bootstrap, out=None):
    """One write run (replay or the daily rules). Returns {"ok", "items", "run", "state", "tgt", "alerts"}."""
    out = out or sys.stdout
    now = now_utc()
    days, hdays = notice_days(profile), history_days(profile)
    state = State(a.state_dir)
    command = "replay" if bootstrap else "apply"
    run = {"id": iso(now), "started_at": iso(now), "command": command, "ok": False}
    try:
        keys = channel_keys(a, profile)
        history = offered_history(state, now, hdays)
        src, tgt, desired, items = plan_all(a, profile, bootstrap, keys, history)
        refuse_woow_database(tgt, profile)
        alerts = [] if bootstrap else history_alerts(state, tgt, desired)
        overridden = check_bootstrap(a, state, src, tgt, desired, items, profile, keys, history, out) \
            if bootstrap else []
        new_holds = process_holds(items, state, now, days, persist=not bootstrap)
        if not bootstrap:
            combined_gate(chosen_items(items), tgt, profile, desired)
        chosen = chosen_items(items)
        base = simulate(tgt, chosen, profile, desired)
        nid = None if bootstrap else open_notice(state, new_holds, base, profile, now, days,
                                                 merge_offered(routed_models(tgt), routed_models(base), history))
        print_items(items, out)
        res = {"ok": True, "written": [], "failed": None}
        if chosen:
            with target_base(a, profile) as tb:
                client = make_client(a, profile, tb)
                try:
                    guard_target(client, profile, tgt, bootstrap)
                    journal = Journal(a.state_dir, command, now)
                    journal.write(step="start", items=len(chosen), force=bool(a.force), why=a.why,
                                  overridden=[it["subject"] for it in overridden])
                    res, error = {"ok": False, "written": [], "failed": "interrupted"}, None
                    try:
                        res = execute(client, a, profile, tgt, desired, chosen, keys, journal, out)
                    except BaseException as e:
                        error = e.__class__.__name__
                        raise
                    finally:
                        journal.write(step="end", ok=res["ok"], error=error, failed=res["failed"],
                                      written=len(res["written"]))
                        journal.close()
                finally:
                    client.close()
            if res["ok"]:
                for it in chosen:
                    if it.get("hold_id") and it["hold_id"] in state.data["holds"]:
                        state.data["holds"][it["hold_id"]].update(state="applied", applied_at=iso(now_utc()))
        tgt2 = read_target(a, profile)
        items2 = build_items(src, tgt2, desired, [], profile, keys, bootstrap)
        classify(items2, tgt2, profile, bootstrap, desired, history)
        process_holds(items2, state, now, days, persist=False)
        leftover = chosen_items(items2)
        for it in leftover:
            print(f"LEFT {it['subject']}: still differs after apply", file=out)
        record_offered(state, tgt, now, hdays)
        record_offered(state, tgt2, now, hdays)
        if not bootstrap:
            state.data.setdefault("sync_started_at", iso(now))
        run.update(ok=res["ok"] and not leftover, write_ok=res["ok"], failed=res["failed"],
                   applied=len(res["written"]), written=res["written"][:50],
                   left=[it["subject"] for it in leftover][:50], notice=nid)
        state.data["runs"] = (state.data.get("runs") or [])[-50:] + [run]
        state.save()
        return {"ok": run["ok"], "items": items, "run": run, "state": state, "tgt": tgt2, "alerts": alerts}
    except BaseException as e:
        run.update(ok=False, error=e.__class__.__name__)
        state.data["runs"] = (state.data.get("runs") or [])[-50:] + [run]
        state.save()
        raise


def cmd_apply(a):
    profile = load(a)
    res = run_apply(a, profile, bootstrap=False)
    extra = list(res["alerts"])
    fail = apply_failure_alert(res["run"])
    if fail:
        extra.append(fail)
    alerts = collect_alerts(res["items"], res["state"], now_utc(), extra)
    res["state"].save()
    make_sink(a, a.state_dir).emit(alerts, res["run"])
    return 0 if res["ok"] else 1


def cmd_replay(a):
    if not a.yes_bootstrap:
        raise SyncError("replay copies everything, price increases and platform-coupled settings included: only for a "
                        "fresh target before customers exist. Pass --yes-bootstrap")
    profile = load(a)
    res = run_apply(a, profile, bootstrap=True)
    print("replay: " + ("done; now run verify --strict, then plan --bootstrap (must be 0 auto)" if res["ok"] else
                        "FAILED; inspect the journal, then resume with replay --force --why '<reason>'"))
    return 0 if res["ok"] else 1


def cmd_verify(a):
    profile = load(a)
    keys = channel_keys(a, profile) if a.check_keys else None
    src = read_source(a, profile)
    tgt = read_target(a, profile)
    check_versions(src, tgt)
    desired, notes = nc.transform(src, profile)
    expected = []
    if not a.strict:
        state = State(a.state_dir, read_only=True)
        items = build_items(src, tgt, desired, notes, profile, keys)
        classify(items, tgt, profile, False, desired, offered_history(state, now_utc(), history_days(profile)))
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
    except Exception as e:
        # Any failed run is itself an alert (an e-mail sink must hear about it too),
        # whatever the exception: IncompleteRead, TimeoutExpired, a broken state file, SIGTERM ...
        now = now_utc()
        run = {"id": iso(now), "started_at": iso(now), "command": "daily", "ok": False}
        try:
            make_sink(a, a.state_dir).emit([{
                "severity": "action", "title": "daily sync failed",
                "details": [f"{e.__class__.__name__}: {nc.redact(str(e))[:300]}",
                            "writes done before the failure are in the journal of the state directory"],
                "new": True, "fingerprint": "daily-failed-" + iso(now)}], run)
        except Exception:  # noqa: BLE001 - the original error matters more
            pass
        raise


def _daily(a):
    profile = load(a)
    now = now_utc()
    extra = image_alerts(REPO, kubectl_argv(a), a.live_images, profile)
    extra += pat_expiry_alerts(a, profile, now)
    if a.apply:
        res = run_apply(a, profile, bootstrap=False)
        items, run, state, ok = res["items"], res["run"], res["state"], res["ok"]
        extra += res["alerts"]
        fail = apply_failure_alert(run)
        if fail:
            extra.append(fail)
    else:
        state = State(a.state_dir)
        days, hdays = notice_days(profile), history_days(profile)
        history = offered_history(state, now, hdays)
        _, tgt, desired, items = plan_all(a, profile, False, None, history)
        extra += history_alerts(state, tgt, desired)
        new = process_holds(items, state, now, days, persist=True)
        combined_gate(chosen_items(items), tgt, profile, desired)
        base = simulate(tgt, chosen_items(items), profile, desired)
        nid = open_notice(state, new, base, profile, now, days,
                          merge_offered(routed_models(tgt), routed_models(base), history))
        print_items(items)
        record_offered(state, tgt, now, hdays)
        ok, run = True, {"id": iso(now), "started_at": iso(now), "command": "daily-report", "notice": nid, "ok": True}
        state.data["runs"] = (state.data.get("runs") or [])[-50:] + [run]
    alerts = collect_alerts(items, state, now, extra)
    if run.get("notice"):
        n = state.data["notices"][run["notice"]]
        alerts.append({"severity": "action", "title": f"price-increase notice {n['id']} drafted",
                       "details": [n["draft"], f"after sending: apporo_sync.py notice-sent {n['id']} --effective "
                                   "<YYYY-MM-DD> --at <send time>"],
                       "new": True, "fingerprint": n["id"]})
    state.save()
    make_sink(a, a.state_dir).emit(alerts, run)
    return 0 if ok else 1


def cmd_notice_sent(a):
    state = State(a.state_dir)
    n = state.data["notices"].get(a.notice_id)
    if not n:
        raise SyncError(f"no notice {a.notice_id}; see `holds`")
    if n.get("sent_at"):
        raise SyncError(f"notice {a.notice_id} was already recorded as sent at {n['sent_at']} "
                        f"(effective {n.get('effective_date')})")
    now = now_utc()
    at = parse_iso(a.at) if a.at else now
    if at > now + datetime.timedelta(minutes=5):
        raise SyncError("--at is in the future")
    if at < parse_iso(n["created_at"]):
        raise SyncError("--at is before the notice was drafted")
    backdated = now - at > BACKDATE_LIMIT
    if backdated and not a.backdated:
        raise SyncError(f"--at is more than {BACKDATE_LIMIT.total_seconds() / 3600:.0f} hours ago: pass --backdated "
                        "only if the email really went out then")
    effective = parse_date(a.effective)
    holds = [state.data["holds"][hid] for hid in n["hold_ids"]
             if hid in state.data["holds"] and state.data["holds"][hid]["state"] == "pending-notice"]
    days = max([nc.MIN_NOTICE_DAYS] + [int(h.get("notice_days") or nc.MIN_NOTICE_DAYS) for h in holds])
    start = effective_start(effective)
    if start < at + datetime.timedelta(days=days):
        raise SyncError(f"--effective {effective.isoformat()} is less than {days} days after sending "
                        f"({iso(at)}); the earliest effective date is {earliest_effective(at, days).isoformat()} "
                        "(AI Models Terms, Section 3). Send a corrected notice if the email named an earlier date")
    for h in holds:
        h.update(state="notified", notice_sent_at=iso(at), effective_date=effective.isoformat(), eligible_at=iso(start))
    n.update(sent_at=iso(at), effective_date=effective.isoformat(), recorded_at=iso(now), backdated=backdated)
    state.save()
    print(f"notice {a.notice_id} sent at {iso(at)}, effective {effective.isoformat()}: "
          + (f"{len(holds)} hold(s) apply from the first daily run after {iso(start)} (00:00 UTC-12)"
             if holds else "no hold was waiting for this notice (cancelled or superseded)"))
    return 0


def cmd_holds(a):
    state = State(a.state_dir)
    for h in sorted(state.data["holds"].values(), key=lambda h: h["detected_at"]):
        print(f"{h['id']} {h['state']:<15} {'/'.join(str(x) for x in h['subject'] if x)}: "
              f"{short(h['old'], 40)} -> {short(h['new'], 40)}  notice {h.get('notice_id')} "
              f"sent {h.get('notice_sent_at')} effective {h.get('effective_date')} from {h.get('eligible_at')}")
    for n in state.data["notices"].values():
        print(f"{n['id']} sent {n['sent_at']} effective {n.get('effective_date')} holds {n['hold_ids']} "
              f"draft {n['draft']}")
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


def revoke_pat(client, password, token_id):
    """Revoke one of the caller's PATs (proof by password); True when the server confirmed it."""
    if token_id is None:
        return False
    ok, _, ans = api(client, "POST", "/api/verify", {"method": "password", "scope": "access_token.revoke",
                                                     "context": {"token_id": token_id}, "password": password.reveal()})
    if not ok:
        return False
    proof = (ans.get("data") or {}).get("proof_token") or ""
    ok, _, _ = api(client, "DELETE", f"/api/user/access_tokens/{token_id}", headers={"X-Security-Proof": proof})
    return ok


def cmd_mint_sync_pat(a):
    """Bootstrap only (root has no 2FA yet): root PAT for the sync, straight into a Secret.
    If the Secret cannot be written, the new PAT is revoked at once (no orphan credential)."""
    profile = load(a)
    ns = profile["target"]["namespace"]
    name = profile.get("auth", {}).get("pat_secret", "newapi-sync-pat")
    expires = int(time.time()) + PAT_DAYS * 86400
    with target_base(a, profile) as base:
        a.auth = "password"
        client = make_client(a, profile, base)
        try:
            tgt = read_target(a, profile)
            guard_target(client, profile, tgt, bootstrap=True)
            password = read_secret(kubectl_argv(a), ns, profile.get("auth", {}).get("password_secret",
                                                                                    "newapi-root-credentials"),
                                   "password")
            ok, msg, ans = api(client, "POST", "/api/verify", {
                "method": "password", "scope": "access_token.generate",
                "context": {"scopes": sorted(PAT_SCOPES), "expires_at": expires}, "password": password.reveal()})
            if not ok:
                raise SyncError(f"password proof refused ({msg}): root may already have 2FA; mint the PAT in the browser")
            proof = (ans.get("data") or {}).get("proof_token")
            ok, msg, ans = api(client, "POST", "/api/user/access_tokens",
                               {"name": "apporo-sync-" + now_utc().strftime("%Y%m%d"), "scopes": sorted(PAT_SCOPES),
                                "expires_at": expires}, headers={"X-Security-Proof": proof or ""})
            if not ok:
                raise SyncError(f"PAT not created: {msg}")
            data = ans.get("data") or {}
            token = Secret(data.get("token") or "")
            meta = data.get("item") or {}
            hint, token_id = meta.get("token_hint", ""), meta.get("id")
            ans = data = None
            if not na.PAT_RE.fullmatch(token.reveal()):
                revoked = revoke_pat(client, password, token_id)
                raise SyncError("the server's answer did not hold one nap_... token; the PAT was "
                                + ("revoked" if revoked else "NOT revoked: revoke it by hand (root, access tokens)"))
            manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": name, "namespace": ns,
                                     "labels": {"app.kubernetes.io/part-of": "woow-k3s-newapi"},
                                     "annotations": {"woowtech.io/expires-at": str(expires), "woowtech.io/hint": hint,
                                                     "woowtech.io/scopes": ",".join(sorted(PAT_SCOPES))}},
                        "data": {"token": base64.b64encode(token.reveal().encode()).decode()}}
            try:
                ok, msg = write_secret(kubectl_argv(a), ns, name, manifest)
            finally:
                manifest = token = None
            if not ok:
                revoked = revoke_pat(client, password, token_id)
                raise SyncError(f"secret {ns}/{name} not written ({msg}); the new PAT (hint {hint}) was "
                                + ("revoked" if revoked else "NOT revoked: revoke it by hand (root, access tokens)"))
        finally:
            client.close()
    print(f"secret {ns}/{name} {msg} (PAT hint {hint}, scopes {', '.join(sorted(PAT_SCOPES))}, expires "
          f"{datetime.datetime.fromtimestamp(expires, TPE):%Y-%m-%d})")
    return 0


def cmd_failure_alert(a):
    """For systemd OnFailure= (a crash, SIGKILL or timeout the tool could not report itself)."""
    now = now_utc()
    run = {"id": iso(now), "started_at": iso(now), "command": "failure-alert", "ok": False}
    make_sink(a, a.state_dir).emit([{"severity": "action", "title": "daily sync unit failed",
                                     "details": [nc.redact(a.reason or "the systemd unit reported a failure")[:300],
                                                 "see journalctl --user -u apporo-newapi-sync.service"],
                                     "new": True, "fingerprint": "unit-failed-" + iso(now)}], run)
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
    p.add_argument("--force", action="store_true", help="also on a target that is not fresh (needs --why)")
    p.add_argument("--why", help="journaled reason for --force")
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("verify", help="read-only SQL fingerprints of the target vs the transform")
    p.add_argument("--strict", action="store_true", help="no held or alert-only difference allowed")
    p.add_argument("--check-keys", action="store_true", help="also compare channel keys with the key Secret")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("daily", help="images + plan + holds + alerts (+ apply)")
    p.add_argument("--apply", action="store_true", help="write the target (the timer passes it in apply mode)")
    p.add_argument("--live-images", action="store_true", help="also compare running images (kubectl get)")
    p.add_argument("--alert-sink", help="file (default); e-mail is not implemented yet")
    p.set_defaults(fn=cmd_daily)
    p = sub.add_parser("notice-sent", help="record that a drafted price notice was sent, and its effective date")
    p.add_argument("notice_id")
    p.add_argument("--effective", required=True,
                   help="the effective date written in the email (YYYY-MM-DD); at least 7 days after sending")
    p.add_argument("--at", help="send time, ISO-8601 with zone (default now)")
    p.add_argument("--backdated", action="store_true", help="--at is more than 24 hours ago, deliberately")
    p.set_defaults(fn=cmd_notice_sent)
    p = sub.add_parser("holds", help="list holds and notices")
    p.set_defaults(fn=cmd_holds)
    p = sub.add_parser("check-images", help="deploy/woow-k3s.yaml vs deploy/apporo-k3s.yaml image pins")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_check_images)
    p = sub.add_parser("mint-sync-pat", help="bootstrap: root PAT for the sync into the Secret (root without 2FA)")
    p.set_defaults(fn=cmd_mint_sync_pat)
    p = sub.add_parser("failure-alert", help="record a failed unit run as an alert (systemd OnFailure=)")
    p.add_argument("--reason", help="short text (no secrets)")
    p.add_argument("--alert-sink", help="file (default)")
    p.set_defaults(fn=cmd_failure_alert)
    return ap


def _on_sigterm(signum, frame):
    raise Terminated("terminated by SIGTERM (systemd stop or TimeoutStartSec)")


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.target_base:
        u = urllib.parse.urlsplit(a.target_base)
        if u.scheme != "http" or u.hostname not in na.LOOPBACK_HOSTS or u.path not in ("", "/"):
            print("error: --target-base must be http://127.0.0.1:<port> (a port-forward)", file=sys.stderr)
            return 2
        a.target_base = f"http://{u.netloc}"
    for attr in ("check_keys", "force", "why", "live_images", "alert_sink", "backdated"):
        if not hasattr(a, attr):
            setattr(a, attr, None)
    if a.force and not (a.why or "").strip():
        print("error: --force needs --why '<reason>' (it is journaled)", file=sys.stderr)
        return 2
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return a.fn(a)
    except (SyncError, nc.ContentError, na.Fatal) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {getattr(e, 'filename', '') or ''} {e.strerror or e}", file=sys.stderr)
        return 2
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        while _OPEN_STATES:
            _OPEN_STATES.pop().close()


if __name__ == "__main__":
    sys.modules.setdefault("apporo_sync", sys.modules[__name__])
    sys.exit(main())
