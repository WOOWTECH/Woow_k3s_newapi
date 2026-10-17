"""What the contract suite talks to, read from the environment.

Nothing has a default host: an unset target means its tests skip, so the suite
collects (and the offline self-checks run) on a machine with no cluster. Every
live run must say ``NEWAPI_CONTRACT_TARGET=stg``; conftest.py then checks that
the server really is new-api-stg (``/api/status`` ServerAddress, directly and
through the proxy) before anything is created, and stops the run (exit 3)
otherwise. Secrets only ever arrive as a path to a 0600 file or an env var the
shell exported; no secret is a default or a command-line argument, and every
one is held in :class:`redact.Secret`.

Environment (docs/OPERATIONS.md 〈升版〉 lists the full set for a gate run)
-----------------------------------------------------------------------
target and transport
  NEWAPI_CONTRACT_TARGET        must be ``stg`` (the only site the suite runs on)
  NEWAPI_CONTRACT_KUBECTL       kubectl binary. Turns on everything that needs the
                                cluster: the suite's own port-forwards, read-only
                                SQL, the Valkey CT budget, the run lock, and (with
                                the opt-ins below) restarts and kills. Every
                                command it builds carries ``-n new-api-stg``.
  NEWAPI_CONTRACT_BASE          new-api origin (plain http only to loopback). Unset
                                with KUBECTL set: the suite port-forwards
                                svc/newapi itself.
  NEWAPI_CONTRACT_PROXY_BASE    newapi-proxy origin. Unset with KUBECTL set and a
                                CRON key given: the suite port-forwards
                                svc/newapi-proxy itself (a port-forward bypasses
                                the NetworkPolicy; the proxy authenticates by HMAC).
  NEWAPI_CONTRACT_RELAY_BASE    origin serving /v1 (default: the new-api origin)

credentials (0600 files; env for the proxy keys)
  NEWAPI_CONTRACT_ADMIN_PAT_FILE    root PAT made for the run: user:read,
                                    user:write, log:read (never the proxy's AP/OP)
  NEWAPI_CONTRACT_SETTINGS_PAT_FILE root PAT with option:read+option:write, used
                                    only by the settings flips and verify-settings
  NEWAPI_CONTRACT_ADMIN_CREDS_FILE  stg root ``url=/username=/password=`` (stg root
                                    has no 2FA): the admin step-up proofs (hard
                                    delete of test users, C03 admin contexts, C27)
  WOOW_NEWAPI_PROXY_AUTH_CRON / _WEB  signing key lists ``kid:secret[,...]``

models and opt-ins
  NEWAPI_CONTRACT_FREE_MODEL    default qwen3-4b-cluster1
  NEWAPI_CONTRACT_PAID_MODEL    the cheapest priced model (F05/F06/F07/F08)
  NEWAPI_CONTRACT_SKIP_SLOW=1   skip the long waits (quick look; never a gate run)
  NEWAPI_CONTRACT_DISRUPTIVE=1  run what touches the shared stg: setting flips
                                (F02/F05/F09), Valkey restart (F11), proxy kill
                                (X09). Announced window only (OPERATIONS).
  NEWAPI_CONTRACT_CT_EXHAUST=1  C23's 429 run; alone: ``pytest -m ct_exhaust``
  NEWAPI_CONTRACT_UPSTREAM_DOWN=1  X06 connect run while newapi-master is scaled
                                to 0; alone: ``pytest -m upstream_down``
  NEWAPI_CONTRACT_F10_CONFIRM=1 + NEWAPI_CONTRACT_F10_MODEL (the model with the
                                highest output price) [+ _F10_MAX_USD (0.50),
                                _F10_MAX_TOKENS (512), _F10_CONCURRENCY (200)]
  NEWAPI_CONTRACT_EXPECT_PERGROUP=1  after an upgrade with 45fbb149f (F12)
  NEWAPI_CONTRACT_GATE=1        strict: exit non-zero when an id is skip,
                                partial or missing (alias _REQUIRE_ALL=1)
  NEWAPI_CONTRACT_REPORT        write the JSON report there
  NEWAPI_CONTRACT_RECORD_DIR    write live, de-identified response fixtures there
  NEWAPI_CONTRACT_USER_PREFIX   direct test users, default ``wct``
  NEWAPI_CONTRACT_PROXY_PREFIX  test users that log in through the proxy (must
                                fit the proxy's ``^ws[a-z]{2}\\d{10}$``), default
                                ``wsct``; names are ``<prefix>99<8 digits>``
  NEWAPI_CONTRACT_SVC_USERNAME  the proxy's service account, default wpsvc-stg
  NEWAPI_CONTRACT_MAX_CT_WAIT   longest single wait for the CT window, seconds
                                (default 1300)
"""
from __future__ import annotations

import ipaddress
import os
import re
import stat

from .redact import Secret

#: The only namespace any built command may name.
NAMESPACE = "new-api-stg"
#: The only site a live run may target (newapi_admin.TARGET_URLS["stg"]).
TARGET = "stg"

#: Settings removed in the NA-03 review: the suite builds those commands itself.
REMOVED_ENV = {
    "NEWAPI_CONTRACT_PSQL": "set NEWAPI_CONTRACT_KUBECTL; the suite builds a read-only psql with -n new-api-stg",
    "NEWAPI_CONTRACT_X09_KILL": "set NEWAPI_CONTRACT_KUBECTL and NEWAPI_CONTRACT_DISRUPTIVE=1",
    "NEWAPI_CONTRACT_VALKEY_CLI": "set NEWAPI_CONTRACT_KUBECTL",
    "NEWAPI_CONTRACT_VALKEY_RESTART": "set NEWAPI_CONTRACT_KUBECTL and NEWAPI_CONTRACT_DISRUPTIVE=1",
    "NEWAPI_CONTRACT_SETTINGS_ADMIN": "set NEWAPI_CONTRACT_SETTINGS_PAT_FILE and NEWAPI_CONTRACT_DISRUPTIVE=1",
    "NEWAPI_CONTRACT_UPSTREAM_SLOW": "X06 stalls new-api itself now (a row lock on its own test user)",
    "NEWAPI_CONTRACT_TENANT_UP_FILE": "X07/X09 use a suite-owned user now (NEWAPI_CONTRACT_PROXY_PREFIX)",
    "NEWAPI_CONTRACT_TENANT_USERNAME": "X07/X09 use a suite-owned user now (NEWAPI_CONTRACT_PROXY_PREFIX)",
    "NEWAPI_CONTRACT_TENANT_IP": "the suite's tenant IPs are fixed (config.SUITE_TENANT_IPS)",
}

#: The proxy's fixed synthetic addresses (DESIGN §1.4.2-4): AP .1, OP .3.
AP_IP, OP_IP = "198.18.0.1", "198.18.0.3"

PAT_RE = re.compile(r"nap_[A-Za-z0-9]{43}")
PREFIX_RE = re.compile(r"[a-z]{2,6}")
PROXY_PREFIX_RE = re.compile(r"ws[a-z]{2}")

#: Synthetic tenant IPs the suite uses through the proxy: block 31, account
#: ids 3800-3839 (198.18.0.0 + 256 + 31*4096 + id = 198.19.255.216-.255).
#: Inside the stg PROXY_TENANT_IP_RANGE; no sandbox gets near id 3800, so the
#: suite never shares a CT bucket with a sandbox tenant.
_SUITE_IP_FIRST = int(ipaddress.IPv4Address("198.18.0.0")) + 256 + 31 * 4096 + 3800
SUITE_TENANT_IPS = tuple(str(ipaddress.IPv4Address(_SUITE_IP_FIRST + k)) for k in range(40))


class ConfigError(Exception):
    """A setting is unusable (message never contains a secret)."""


def _origin(value):
    return value.strip().rstrip("/") if value else None


def _flag(env, name):
    return env.get(name) == "1"


def _require_private(path, what):
    try:
        st = os.stat(path)
    except OSError as e:
        raise ConfigError(f"{what} file {path}: {e.strerror}") from None
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{what} file {path}: mode {stat.S_IMODE(st.st_mode):04o}, must be 0600")


def load_pat(path, what="PAT"):
    """One nap_ PAT alone in a 0600 file, as a Secret (never echoed)."""
    path = os.path.expanduser(path)
    _require_private(path, what)
    with open(path) as f:
        token = f.read().strip()
    if not PAT_RE.fullmatch(token):
        raise ConfigError(f"{what} file {path}: expected exactly one nap_ access token (47 chars)")
    return Secret(token)


def load_creds(path):
    """0600 ``url=/username=/password=`` file; the password as a Secret."""
    path = os.path.expanduser(path)
    _require_private(path, "credentials")
    out = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    if not {"url", "username", "password"} <= set(out):
        raise ConfigError(f"credentials file {path}: needs url=, username= and password= lines")
    out["password"] = Secret(out["password"])
    return out


def _int_env(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}: not an integer") from None


def _float_env(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name}: not a number") from None


class Config:
    def __init__(self, environ=None):
        env = environ if environ is not None else os.environ
        self.removed = sorted(k for k in REMOVED_ENV if env.get(k))
        self.target = (env.get("NEWAPI_CONTRACT_TARGET") or "").strip() or None
        self.kubectl = (env.get("NEWAPI_CONTRACT_KUBECTL") or "").strip() or None
        self.base = _origin(env.get("NEWAPI_CONTRACT_BASE"))
        self.proxy_base = _origin(env.get("NEWAPI_CONTRACT_PROXY_BASE"))
        self._relay_base = _origin(env.get("NEWAPI_CONTRACT_RELAY_BASE"))
        self.admin_pat_file = env.get("NEWAPI_CONTRACT_ADMIN_PAT_FILE") or None
        self.settings_pat_file = env.get("NEWAPI_CONTRACT_SETTINGS_PAT_FILE") or None
        self.admin_creds_file = env.get("NEWAPI_CONTRACT_ADMIN_CREDS_FILE") or None
        web = env.get("WOOW_NEWAPI_PROXY_AUTH_WEB") or None
        cron = env.get("WOOW_NEWAPI_PROXY_AUTH_CRON") or None
        self.auth_web = Secret(web) if web else None
        self.auth_cron = Secret(cron) if cron else None
        self.user_prefix = (env.get("NEWAPI_CONTRACT_USER_PREFIX") or "wct").strip()
        self.proxy_prefix = (env.get("NEWAPI_CONTRACT_PROXY_PREFIX") or "wsct").strip()
        self.free_model = (env.get("NEWAPI_CONTRACT_FREE_MODEL") or "qwen3-4b-cluster1").strip()
        self.paid_model = (env.get("NEWAPI_CONTRACT_PAID_MODEL") or "").strip() or None
        self.svc_username = (env.get("NEWAPI_CONTRACT_SVC_USERNAME") or "wpsvc-stg").strip()
        self.skip_slow = _flag(env, "NEWAPI_CONTRACT_SKIP_SLOW")
        self.disruptive = _flag(env, "NEWAPI_CONTRACT_DISRUPTIVE")
        self.ct_exhaust = _flag(env, "NEWAPI_CONTRACT_CT_EXHAUST")
        self.upstream_down = _flag(env, "NEWAPI_CONTRACT_UPSTREAM_DOWN")
        self.expect_pergroup = _flag(env, "NEWAPI_CONTRACT_EXPECT_PERGROUP")
        self.gate = _flag(env, "NEWAPI_CONTRACT_GATE") or _flag(env, "NEWAPI_CONTRACT_REQUIRE_ALL")
        self.report_path = env.get("NEWAPI_CONTRACT_REPORT") or None
        self.record_dir = env.get("NEWAPI_CONTRACT_RECORD_DIR") or None
        self.f10_confirm = _flag(env, "NEWAPI_CONTRACT_F10_CONFIRM")
        self.f10_model = (env.get("NEWAPI_CONTRACT_F10_MODEL") or "").strip() or None
        self.f10_max_usd = _float_env(env, "NEWAPI_CONTRACT_F10_MAX_USD", 0.50)
        self.f10_max_tokens = _int_env(env, "NEWAPI_CONTRACT_F10_MAX_TOKENS", 512)
        self.f10_concurrency = _int_env(env, "NEWAPI_CONTRACT_F10_CONCURRENCY", 200)
        self.max_ct_wait = _int_env(env, "NEWAPI_CONTRACT_MAX_CT_WAIT", 1300)

    # ---- validation (no network) -------------------------------------- #
    def problems(self):
        """Static problems that make a live run unsafe (empty list = fine)."""
        out = [f"{k} was removed: {REMOVED_ENV[k]}" for k in self.removed]
        if not PREFIX_RE.fullmatch(self.user_prefix):
            out.append("NEWAPI_CONTRACT_USER_PREFIX: 2-6 lowercase letters")
        if self.user_prefix.startswith("w") and self.user_prefix[:2] in ("wp", "ws"):
            out.append("NEWAPI_CONTRACT_USER_PREFIX must not start with wp/ws (PaaS tenant prefixes)")
        if not PROXY_PREFIX_RE.fullmatch(self.proxy_prefix):
            out.append("NEWAPI_CONTRACT_PROXY_PREFIX: 'ws' + two lowercase letters (the stg proxy's username rule)")
        if self.f10_max_tokens < 16 or self.f10_concurrency < 1:
            out.append("F10 knobs out of range")
        return out

    # ---- availability (drives skips; never raises) -------------------- #
    @property
    def live(self):
        """Anything at all points at a cluster."""
        return bool(self.base or self.proxy_base or self.kubectl)

    @property
    def have_kube(self):
        return bool(self.kubectl)

    @property
    def have_newapi(self):
        return bool((self.base or self.kubectl) and self.admin_pat_file)

    @property
    def have_admin_creds(self):
        return bool(self.admin_creds_file)

    @property
    def have_settings_pat(self):
        return bool(self.settings_pat_file)

    @property
    def have_paid_model(self):
        return bool(self.paid_model)

    @property
    def have_proxy_cron(self):
        return bool((self.proxy_base or self.kubectl) and self.auth_cron)

    @property
    def have_proxy_web(self):
        return bool((self.proxy_base or self.kubectl) and self.auth_web)

    def relay_base(self, newapi_base):
        return self._relay_base or newapi_base
