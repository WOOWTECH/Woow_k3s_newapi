"""Fixtures, guards, pacing and the per-id report for the contract suite.

Order of business in a live run:

1. **No secret ever printed**: ``--assert=plain`` (pytest.ini) and no
   ``-l/--showlocals`` (refused here); every credential is a
   :class:`redact.Secret`.
2. **Staging only** (``stg_guard``, autouse): ``NEWAPI_CONTRACT_TARGET=stg``,
   ``/api/status`` ServerAddress == the stg URL directly *and* through the
   proxy, the root creds file names the stg URL; otherwise ``pytest.exit(3)``
   before anything is created. Then the run lock (ConfigMap
   ``new-api-stg/newapi-contract-run-lock``).
3. **CT pacing**: every critical-route call waits for the shared 127.0.0.1
   window (budget.py).
4. **Cleanup that proves itself**: every test user is registered as soon as it
   exists; at session finish the sweep deletes whatever is left and the run
   fails if a test user (or a relay key of one) survives; ``verify-settings``
   runs after any settings flip and fails the run on a non-zero exit.
5. **Report**: one status per id, worst wins (fail > partial > skip > pass);
   ``NEWAPI_CONTRACT_GATE=1`` exits non-zero unless every id passes (or is a
   documented known partial); ``NEWAPI_CONTRACT_REPORT`` gets the JSON
   (report.py) with the server version, both images, F10/C23/F11 numbers,
   the CT budget and per-test durations.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import secrets
import subprocess
import sys
import time

import pytest

from . import newapi, proxy, report
from .budget import CtBudget
from .config import NAMESPACE, SUITE_TENANT_IPS, Config, ConfigError, load_creds, load_pat
from .ids import CONTRACT_IDS, UNREACHABLE
from .record import FixtureSink
from .redact import Secret

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

MARKERS = (
    "contract_id(id): the C/F/X id this test pins",
    "offline: runs without any cluster (CI)",
    "slow: long waits; skipped with NEWAPI_CONTRACT_SKIP_SLOW=1",
    "disruptive: touches shared stg state; needs NEWAPI_CONTRACT_DISRUPTIVE=1, runs last",
    "ct_exhaust: C23's 429 run; alone (pytest -m ct_exhaust) with NEWAPI_CONTRACT_CT_EXHAUST=1",
    "upstream_down: X06 connect run; alone (pytest -m upstream_down) with NEWAPI_CONTRACT_UPSTREAM_DOWN=1",
    "run_last: ordered after everything else",
)


class State:
    """Run-wide state the hooks need after the fixtures are gone."""

    def __init__(self):
        self.started = time.time()
        self.tests = {}  # nodeid -> record
        self.metrics = {}
        self.notes = {}
        self.env = {"target": None}
        self.lab = None
        self.kube = None
        self.budget = None
        self.settings = None
        self.ports = contextlib.ExitStack()
        self.origins = None
        self.lock_id = None
        self.writer = None
        self.cleanup = None
        self.verify_exit = None
        self.sink = None

    def note(self, msg):
        if self.writer is not None:
            self.writer.write_line(f"[contract] {msg}")
        else:
            print(f"[contract] {msg}", file=sys.__stderr__, flush=True)


def _state(config):
    return config._contract


def pytest_configure(config):
    for m in MARKERS:
        config.addinivalue_line("markers", m)
    if config.getoption("showlocals", False):
        pytest.exit("never run the contract suite with -l/--showlocals (it would print credentials)", returncode=4)
    if config.getoption("assertmode", "plain") != "plain":
        pytest.exit("the contract suite runs with --assert=plain only (assertion rewriting prints values)", returncode=4)
    config._contract = State()


def pytest_unconfigure(config):
    st = getattr(config, "_contract", None)
    if st is not None:
        st.ports.close()


# --------------------------------------------------------------------------- #
#  collection: staged runs alone, disruptive and run_last at the end          #
# --------------------------------------------------------------------------- #
@pytest.hookimpl(trylast=True)  # after -m / -k deselection
def pytest_collection_modifyitems(session, config, items):
    cfg = Config()
    for flag, marker, how in ((cfg.ct_exhaust, "ct_exhaust", "pytest -m ct_exhaust"),
                              (cfg.upstream_down, "upstream_down", "pytest -m upstream_down")):
        if flag:
            others = [i for i in items if not i.get_closest_marker(marker) and not i.get_closest_marker("offline")]
            if others:
                pytest.exit(f"NEWAPI_CONTRACT_{marker.upper()}=1 runs alone: {how}", returncode=4)

    def rank(item):
        if item.get_closest_marker("run_last"):
            return 2
        if item.get_closest_marker("disruptive"):
            return 1
        return 0

    items.sort(key=rank)  # stable


# --------------------------------------------------------------------------- #
#  config, guard, transport                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def cfg():
    c = Config()
    problems = c.problems()
    if problems:
        pytest.exit("contract config: " + "; ".join(problems), returncode=3)
    return c


@pytest.fixture(scope="session")
def kube(cfg, request):
    if not cfg.have_kube:
        return None
    from .kube import Kube, KubeError

    st = _state(request.config)
    if st.kube is None:
        try:
            st.kube = Kube(cfg.kubectl)
        except KubeError as e:
            pytest.exit(f"NEWAPI_CONTRACT_KUBECTL: {e}", returncode=3)
    return st.kube


def _origins(cfg, st):
    """(newapi_base, proxy_base): configured, or the suite's own port-forwards."""
    if st.origins is not None:
        return st.origins
    base, proxy_base = cfg.base, cfg.proxy_base
    if st.kube is not None:
        if not base and not cfg.upstream_down:
            base = st.ports.enter_context(st.kube.port_forward("svc/newapi", 3000))
        if not proxy_base and cfg.auth_cron:
            proxy_base = st.ports.enter_context(st.kube.port_forward("svc/newapi-proxy", 8080))
    st.origins = (base, proxy_base)
    return st.origins


@pytest.fixture(scope="session", autouse=True)
def stg_guard(cfg, kube, request):
    """Refuse anything but new-api-stg before a single write."""
    st = _state(request.config)
    st.writer = request.config.pluginmanager.get_plugin("terminalreporter")
    if not cfg.live:
        return None
    from . import settings as settings_mod

    stg_url = settings_mod.NA.TARGET_URLS["stg"]

    def stop(msg):
        pytest.exit(f"stg guard: {msg}", returncode=3)

    if cfg.target != "stg":
        stop("set NEWAPI_CONTRACT_TARGET=stg (the suite runs on new-api-stg only)")
    if cfg.gate and not (cfg.have_kube and cfg.have_admin_creds and cfg.auth_cron and cfg.auth_web):
        stop("a gate run needs NEWAPI_CONTRACT_KUBECTL, NEWAPI_CONTRACT_ADMIN_CREDS_FILE and both proxy keys")
    if cfg.admin_creds_file:
        try:
            creds = load_creds(cfg.admin_creds_file)
        except ConfigError as e:
            stop(str(e))
        if settings_mod.NA.target_of(creds.get("url")) != "stg":
            stop("NEWAPI_CONTRACT_ADMIN_CREDS_FILE url= is not the stg URL: refusing to send that password anywhere")
    try:
        base, proxy_base = _origins(cfg, st)
    except Exception as e:  # noqa: BLE001
        stop(f"port-forward failed: {e}")
    st.env.update({"target": "stg", "newapi_base_kind": "port-forward" if not cfg.base else "configured"})
    if base:
        r = newapi.Client(base).get("/api/status")
        data = r.data if isinstance(r.data, dict) else {}
        if settings_mod.NA.target_of(data.get("server_address")) != "stg":
            stop(f"{base}/api/status ServerAddress is {data.get('server_address')!r}, not {stg_url}")
        st.env.update({"server_address": data.get("server_address"), "newapi_version": data.get("version")})
    elif not cfg.upstream_down:
        stop("no new-api origin (set NEWAPI_CONTRACT_BASE or NEWAPI_CONTRACT_KUBECTL)")
    if proxy_base:
        if not cfg.auth_cron:
            stop("a proxy origin needs WOOW_NEWAPI_PROXY_AUTH_CRON (the guard asks /api/status through it)")
        pc = proxy.ProxyClient(proxy_base, cfg.auth_cron)
        r = pc.send("GET", "/api/status", identity="anon")
        data = r.data if isinstance(r.data, dict) else {}
        if cfg.upstream_down:
            if r.proxy_error != "connect":
                stop("NEWAPI_CONTRACT_UPSTREAM_DOWN=1 but the proxy still reaches new-api (scale newapi-master to 0 first)")
        elif settings_mod.NA.target_of(data.get("server_address")) != "stg":
            stop(f"proxy /api/status ServerAddress is {data.get('server_address')!r}, not {stg_url}")
        st.env["proxy_server_address"] = data.get("server_address")
    relay = cfg.relay_base(base)
    if relay and relay != base:
        r = newapi.Client(relay).get("/api/status")
        if settings_mod.NA.target_of((r.data or {}).get("server_address") if isinstance(r.data, dict) else None) != "stg":
            stop("NEWAPI_CONTRACT_RELAY_BASE is not new-api-stg")
    if kube is not None:
        from .kube import KubeError

        run_id = secrets.token_hex(6)
        try:
            kube.take_lock(run_id, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        except KubeError as e:
            stop(str(e))
        st.lock_id = run_id
    return True


@pytest.fixture(scope="session")
def budget(cfg, kube, request):
    st = _state(request.config)
    if st.budget is None:
        st.budget = CtBudget(counter=kube.counter if kube is not None else None,
                             max_wait=cfg.max_ct_wait, note=st.note)
    return st.budget


@pytest.fixture(scope="session")
def origins(cfg, request, stg_guard):
    return _origins(cfg, _state(request.config))


@pytest.fixture(scope="session")
def newapi_base(origins):
    if not origins[0]:
        pytest.skip("no new-api origin (NEWAPI_CONTRACT_BASE or NEWAPI_CONTRACT_KUBECTL)")
    return origins[0]


@pytest.fixture(scope="session")
def anon(newapi_base):
    return newapi.Client(newapi_base)


@pytest.fixture(scope="session")
def base_client(newapi_base, budget):
    return newapi.Client(newapi_base, budget=budget)


@pytest.fixture(scope="session")
def lab(cfg, newapi_base, budget, request):
    """Admin PAT client, tokenless client, root step-up, user registry."""
    if not cfg.admin_pat_file:
        pytest.skip("NEWAPI_CONTRACT_ADMIN_PAT_FILE unset")
    from .users import Lab

    st = _state(request.config)
    if st.lab is None:
        try:
            pat = load_pat(cfg.admin_pat_file, "admin PAT")
            creds = load_creds(cfg.admin_creds_file) if cfg.admin_creds_file else None
        except ConfigError as e:
            pytest.exit(str(e), returncode=3)
        # The admin PAT has user:read/write + log:read only (no profile:read for
        # /api/user/self): find its owner — the root in the creds file — by search.
        probe = newapi.Client(newapi_base, token=pat, budget=budget)
        kw = creds["username"] if creds else cfg.user_prefix
        me = probe.get(f"/api/user/search?keyword={kw}&p=1&page_size=100")
        if me.success is not True:
            pytest.exit(f"admin PAT does not work for user search (HTTP {me.status}, {me.code})", returncode=3)
        owner = None
        if creds:
            owner = next((u for u in (me.data or {}).get("items") or [] if u.get("username") == creds["username"]), None)
            if owner is None:
                pytest.exit("the root named in NEWAPI_CONTRACT_ADMIN_CREDS_FILE is not on this server", returncode=3)
        admin_id = owner["id"] if owner else None
        admin = newapi.Client(newapi_base, token=pat, budget=budget, uc_user_id=admin_id)
        st.lab = Lab(cfg, admin, newapi.Client(newapi_base, budget=budget), creds=creds,
                     admin_id=admin_id, admin_role=owner.get("role") if owner else None)
    return st.lab


@pytest.fixture(scope="session")
def admin(lab):
    return lab.admin


@pytest.fixture(scope="session")
def relay_base(cfg, newapi_base):
    return cfg.relay_base(newapi_base)


@pytest.fixture(scope="session")
def psql(kube):
    """Read-only SQL on new-api-stg's Postgres -> list of output lines."""
    if kube is None:
        pytest.skip("NEWAPI_CONTRACT_KUBECTL unset (read-only SQL)")
    return kube.psql


@pytest.fixture(scope="session")
def proxy_cron(cfg, origins):
    if not (origins[1] and cfg.auth_cron):
        pytest.skip("no proxy origin / WOOW_NEWAPI_PROXY_AUTH_CRON unset")
    return proxy.ProxyClient(origins[1], cfg.auth_cron)


@pytest.fixture(scope="session")
def proxy_web(cfg, origins):
    if not (origins[1] and cfg.auth_web):
        pytest.skip("no proxy origin / WOOW_NEWAPI_PROXY_AUTH_WEB unset")
    return proxy.ProxyClient(origins[1], cfg.auth_web)


@pytest.fixture(scope="session")
def settings_admin(cfg, newapi_base, budget, request):
    """Root option read/write for the F control tests (disruptive window only)."""
    if not cfg.disruptive:
        pytest.skip("NEWAPI_CONTRACT_DISRUPTIVE!=1 (flips a global stg setting; announced window only)")
    if not cfg.settings_pat_file:
        pytest.skip("NEWAPI_CONTRACT_SETTINGS_PAT_FILE unset (root option:read+write PAT)")
    st = _state(request.config)
    if st.lock_id is None:
        pytest.skip("settings flips need the run lock (NEWAPI_CONTRACT_KUBECTL)")
    from .settings import SettingsAdmin

    if st.settings is None:
        try:
            pat = load_pat(cfg.settings_pat_file, "settings PAT")
        except ConfigError as e:
            pytest.exit(str(e), returncode=3)
        st.settings = SettingsAdmin(newapi.Client(newapi_base, token=pat, budget=budget), notify=st.note)
    return st.settings


@pytest.fixture(scope="session")
def slow(cfg):
    if cfg.skip_slow:
        pytest.skip("NEWAPI_CONTRACT_SKIP_SLOW=1")
    return True


@pytest.fixture(scope="session")
def disruptive(cfg):
    if not cfg.disruptive:
        pytest.skip("NEWAPI_CONTRACT_DISRUPTIVE!=1 (announced window only, docs/OPERATIONS.md 〈升版〉)")
    return True


@pytest.fixture(scope="session")
def fixture_sink(cfg, request):
    st = _state(request.config)
    if st.sink is None:
        st.sink = FixtureSink(cfg.record_dir)
    return st.sink


@pytest.fixture
def contract_partial(request):
    """Mark this test as covering only part of its id's contract."""

    def mark(reason):
        request.node.user_properties.append(("contract_partial", reason))

    return mark


@pytest.fixture
def contract_metric(request):
    """Record numbers for the report: ``contract_metric("F10", {...})``."""
    st = _state(request.config)

    def put(key, values):
        st.metrics.setdefault(key, {}).update(json.loads(json.dumps(values, default=str)))

    return put


# --------------------------------------------------------------------------- #
#  test users                                                                 #
# --------------------------------------------------------------------------- #
class Consumer:
    """The C-tests' shared logged-in user (one login, one full UP)."""

    def __init__(self, user, up_token, expiring=None):
        self.user = user
        self.up_token = up_token
        self.expiring = expiring  # (token, expires_at) for the end-of-run expiry check

    @property
    def jwt(self):
        return self.user.jwt_client()

    @property
    def up(self):
        return self.user.up_client(self.up_token)


@pytest.fixture(scope="session")
def consumer(cfg, lab):
    from .users import create_test_user

    user = create_test_user(lab)
    try:
        user.login()
        up_token, _ = user.create_up()
        expiring = None
        if not cfg.skip_slow:
            exp = int(time.time()) + 3600 + 30
            tok, _ = user.create_up(scopes=["profile:read"], expires_at=exp,
                                    name=f"woow-paas-up-{user.id}-expiry")
            expiring = (tok, exp)
        yield Consumer(user, up_token, expiring)
    finally:
        user.cleanup()


class FreeConsumer:
    """The F-tests' one user: a UP, a trial key and a paid key (one batch
    reveal). The per-user rate buckets are shared, so each test waits for the
    free token bucket to refill (``wait_full``) instead of making a new user."""

    BUCKET_REFILL = 62  # 10 per minute -> full after 60 s idle

    def __init__(self, user, up, trial_id, trial_key, paid_id, paid_key):
        self.user, self.up = user, up
        self.trial_id, self.trial_key = trial_id, trial_key
        self.paid_id, self.paid_key = paid_id, paid_key
        self.last_free = 0.0

    @property
    def upc(self):
        return self.user.up_client(self.up)

    def touched(self):
        self.last_free = time.time()

    def wait_full(self):
        wait = self.BUCKET_REFILL - (time.time() - self.last_free)
        if wait > 0:
            time.sleep(wait)

    def quota(self):
        return (self.upc.get("/api/user/self").data or {}).get("quota")

    def set_wallet(self, admin, target):
        """Admin add/subtract (direct, not through the proxy) to reach ``target``."""
        cur = self.quota()
        if cur is None:
            raise AssertionError("could not read the F consumer's wallet")
        delta = target - cur
        if delta:
            mode = "add" if delta > 0 else "subtract"
            r = admin.post("/api/user/manage", {"id": self.user.id, "action": "add_quota", "mode": mode,
                                                "value": abs(delta)})
            assert r.success is True, f"wallet adjust failed ({r.message_key})"


@pytest.fixture(scope="session")
def f_consumer(cfg, lab):
    from .users import create_test_user

    user = create_test_user(lab)
    try:
        user.login()
        up, _ = user.create_up()
        trial_id = user.make_trial_key(up, [cfg.free_model])
        paid_id = user.make_paid_key(up)
        keys = user.batch_reveal(up, [trial_id, paid_id])
        yield FreeConsumer(user, up, trial_id, keys.get(trial_id), paid_id, keys.get(paid_id))
    finally:
        user.cleanup()


@pytest.fixture(scope="session")
def proxy_user(lab, proxy_cron):
    """A suite-owned ws-prefixed user, logged in with a UP made *through the
    proxy* from the suite's first synthetic tenant IP."""
    from .tenant import ProxyTenant
    from .users import create_test_user

    user = create_test_user(lab, proxy=True)
    try:
        t = ProxyTenant(user, proxy_cron, SUITE_TENANT_IPS[0])
        t.create_up()
        yield t
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  what new-api recorded as the client IP (X01, X02, C24)                     #
# --------------------------------------------------------------------------- #
_SID_RE = __import__("re").compile(r"[A-Za-z0-9_-]{1,128}")
FORGED = {"X-Forwarded-For": "114.32.21.18", "X-Real-IP": "114.32.21.18", "Forwarded": "for=114.32.21.18"}


def _session_ip(psql, sid):
    if not sid or not _SID_RE.fullmatch(str.__str__(sid)):
        return None
    rows = psql(f"SELECT ip FROM user_sessions WHERE sid = '{str.__str__(sid)}'")
    return rows[0] if rows else None


def _pat_rows(psql, where):
    """[(id, last_used_at, last_used_ip)] of user_access_tokens matching a fixed WHERE."""
    rows = psql("SELECT id || '|' || coalesce(last_used_at, 0) || '|' || coalesce(last_used_ip, '') "
                f"FROM user_access_tokens WHERE {where} ORDER BY id")
    out = []
    for line in rows:
        tid, at, ip = line.split("|", 2)
        out.append((int(tid), int(at or 0), ip))
    return out


def _touch(psql, where, send, settle=30):
    """Wait out the 60 s last-use throttle, send, then return the IP new-api
    wrote for the request (the row whose last_used_at moved to our send)."""
    rows = _pat_rows(psql, where)
    if not rows:
        return None, "no such PAT"
    newest = max(at for _, at, _ in rows)
    wait = newest + 61 - time.time()
    if wait > 0:
        time.sleep(wait)
    t_send = int(time.time())
    send()
    deadline = time.time() + settle
    while time.time() < deadline:
        moved = [r for r in _pat_rows(psql, where) if r[1] >= t_send - 1]
        if moved:
            return moved[-1][2], None
        time.sleep(2)
    return None, "last_used_at did not move"


def _ap_where(cfg):
    import re as _re

    if not _re.fullmatch(r"[a-z0-9-]{1,32}", cfg.svc_username):
        raise AssertionError("NEWAPI_CONTRACT_SVC_USERNAME: lowercase letters, digits, '-'")
    return (f"user_id = (SELECT id FROM users WHERE username = '{cfg.svc_username}' AND deleted_at IS NULL) "
            "AND name LIKE 'woow-paas-ap-stg-%'")


OP_WHERE = "name LIKE 'woow-paas-op-stg-%'"


@pytest.fixture(scope="session")
def ip_observation(cfg, slow, psql, proxy_cron, proxy_user):
    """One pass for X01 and C24: the tenant's session ip and UP last_used_ip,
    the AP's and the OP's last_used_ip, all written by new-api from the
    proxy's synthetic XFF (TRUSTED_PROXIES)."""
    t = proxy_user
    obs = {"tenant_ip": t.ip, "tenant_session_ip": _session_ip(psql, t.sid)}
    up_where = f"id = {int(t.up_id)}"
    obs["tenant_up_ip"], obs["tenant_up_problem"] = _touch(
        psql, up_where, lambda: t.send("GET", "/api/user/self", auth=t.up))
    obs["ap_ip"], obs["ap_problem"] = _touch(
        psql, _ap_where(cfg), lambda: proxy_cron.send("GET", f"/api/user/{t.id}", identity="ap"))
    obs["op_ip"], obs["op_problem"] = _touch(
        psql, OP_WHERE, lambda: proxy_cron.send("GET", "/api/option/", identity="op"))
    return obs


@pytest.fixture(scope="session")
def forged_observation(cfg, ip_observation, psql, proxy_cron, proxy_user):
    """X02: the same calls with X-Forwarded-For / X-Real-IP / Forwarded set to
    an office address; new-api must still record the synthetic IP."""
    t = proxy_user
    obs = {"tenant_ip": t.ip}
    r = t.login(extra_headers=FORGED, keep=False)
    data = r.data or {}
    sid, jwt = (data.get("session") or {}).get("sid"), data.get("access_token")
    obs["session_ip"] = _session_ip(psql, sid)
    if sid and jwt:
        t.send("DELETE", f"/api/user/sessions/{str.__str__(sid)}", auth=jwt)
    obs["up_ip"], obs["up_problem"] = _touch(
        psql, f"id = {int(t.up_id)}", lambda: t.send("GET", "/api/user/self", auth=t.up, extra_headers=FORGED))
    obs["ap_ip"], obs["ap_problem"] = _touch(
        psql, _ap_where(cfg), lambda: proxy_cron.send("GET", f"/api/user/{t.id}", identity="ap", extra_headers=FORGED))
    return obs


# --------------------------------------------------------------------------- #
#  per-test report                                                            #
# --------------------------------------------------------------------------- #
def _contract_id_of(item):
    m = item.get_closest_marker("contract_id")
    return m.args[0] if m and m.args else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    cid = _contract_id_of(item)
    if not cid:
        return
    st = _state(item.config)
    rec = st.tests.setdefault(item.nodeid, {"id": cid, "outcome": "pass", "partial": None, "duration": 0.0,
                                            "skip_reason": None})
    rec["duration"] = round(rec["duration"] + (rep.duration or 0.0), 2)
    if rep.skipped and rec["outcome"] != "fail":
        rec["outcome"] = "skip"
        lr = rep.longrepr
        rec["skip_reason"] = str(lr[2])[:200] if isinstance(lr, tuple) and len(lr) == 3 else "skipped"
    elif rep.failed:
        rec["outcome"] = "fail"
    for k, v in rep.user_properties:
        if k == "contract_partial":
            rec["partial"] = str(v)[:300]


def _verify_settings(cfg, st):
    """Run verify-settings after a flip; returns the exit code."""
    base = st.origins[0] if st.origins else None
    if not base:
        return 99
    cmd = [sys.executable, os.path.join(REPO, "scripts", "newapi_admin.py"), "--base", base,
           "--token-file", os.path.expanduser(cfg.settings_pat_file), "verify-settings",
           os.path.join(REPO, "deploy", "settings.json"), os.path.join(REPO, "deploy", "settings-stg.json")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    summary = [ln for ln in proc.stdout.splitlines() if ln.startswith(("FAIL", "verify-settings"))]
    for ln in summary[-12:]:
        st.note(ln)
    return proc.returncode


def _collect_env(st):
    env = dict(st.env)
    env["declared"] = report.pins(REPO)
    live = {}
    if st.kube is not None:
        for field, ref in (("newapi_images", "deploy/newapi-master"), ("proxy_images", "deploy/newapi-proxy")):
            try:
                live[field] = st.kube.image_of(ref)
            except Exception as e:  # noqa: BLE001
                live[field] = f"unknown ({type(e).__name__})"
    env["live"] = live
    return env


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    st = getattr(config, "_contract", None)
    if st is None:
        return
    cfg = Config()
    failed = False
    # 1. sweep: no test user / relay key of this run may survive
    if st.lab is not None:
        try:
            psql = st.kube.psql if st.kube is not None else None
            left, soft, tokens_left = st.lab.sweep(psql=psql)
            st.cleanup = st.lab.registry.summary(left)
            st.cleanup["soft_deleted_seen"] = soft
            st.cleanup["relay_keys_left"] = tokens_left
            if left or tokens_left or st.lab.registry.errors:
                failed = True
                st.note(f"cleanup: {len(left)} test user(s) left {left}, relay keys left {tokens_left}, "
                        f"{len(st.lab.registry.errors)} cleanup error(s)")
        except Exception as e:  # noqa: BLE001
            failed = True
            st.cleanup = {"created": len(st.lab.registry.users), "deleted": len(st.lab.registry.deleted),
                          "left": ["<sweep failed>"], "errors": [f"sweep: {type(e).__name__}: {e}"]}
    # 2. verify-settings after any flip
    if st.settings is not None and st.settings.flipped:
        st.verify_exit = _verify_settings(cfg, st)
        if st.verify_exit != 0:
            failed = True
            st.note("verify-settings FAILED after the settings flips: restore with "
                    "scripts/newapi_admin.py ... apply-settings deploy/settings.json deploy/settings-stg.json")
    # 3. report
    ids, summary = report.aggregate(st.tests)
    st.ids, st.summary = ids, summary
    if st.budget is not None:
        st.metrics["ct_budget"] = st.budget.summary()
    invocation = ("ct_exhaust" if cfg.ct_exhaust else "upstream_down" if cfg.upstream_down
                  else "disruptive" if cfg.disruptive else "main")
    payload = {
        "schema": report.SCHEMA,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "run": {"invocation": invocation, "gate": cfg.gate, "pytest_exit": int(exitstatus),
                "started": int(st.started), "finished": int(time.time()),
                "wall_seconds": round(time.time() - st.started, 1), "skip_slow": cfg.skip_slow},
        "env": _collect_env(st) if cfg.live else {"target": None},
        "tests": st.tests,
        "ids": ids,
        "summary": summary,
        "metrics": st.metrics,
        "notes": dict({f"{k} unreachable": v for k, v in UNREACHABLE.items()}, **st.notes),
        "cleanup": st.cleanup or {"created": 0, "deleted": 0, "left": [], "errors": []},
        "settings": {"flipped": list(st.settings.flipped) if st.settings else [], "verify_settings_exit": st.verify_exit},
    }
    st.payload = payload
    if cfg.report_path:
        from . import scrub

        leak = scrub.residue(payload)
        if leak:
            failed = True
            st.note(f"report NOT written: secret residue {leak}")
        else:
            with open(cfg.report_path, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.write("\n")
    if cfg.gate and (summary["fail"] or summary["skip"] or summary["partial"] or summary["missing"]):
        failed = True
    # 4. release the run lock
    if st.kube is not None and st.lock_id:
        st.kube.release_lock(st.lock_id)
    if failed and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    st = getattr(config, "_contract", None)
    if st is None or not st.tests:
        return
    tr = terminalreporter
    tr.write_sep("=", "contract ids (C/F/X)")
    marks = {"pass": "PASS", "fail": "FAIL", "skip": "skip", "partial": "PART", "known_partial": "part",
             "missing": "----"}
    for cid in sorted(CONTRACT_IDS):
        rec = st.ids[cid]
        extra = ""
        if rec["status"] in ("partial", "known_partial", "skip") and (rec["notes"] or rec["skipped"]):
            extra = "  — " + "; ".join((rec["notes"] + rec["skipped"])[:2])[:160]
        tr.write_line(f"  {marks[rec['status']]:4}  {cid}  {CONTRACT_IDS[cid]}"[:150] + extra)
    s = st.summary
    tr.write_line(f"  total: {s['pass']} pass, {s['fail']} fail, {s['partial']} partial, "
                  f"{s['known_partial']} known partial, {s['skip']} skip, {s['missing']} not run")
    m = st.metrics
    if "ct_budget" in m:
        b = m["ct_budget"]
        tr.write_line(f"  CT budget: spent {b['spent']}, {b['waits']} wait(s), {b['waited_seconds']}s waited "
                      f"({'paced via Valkey' if b['pacing'] else 'NOT paced: no Valkey access'})")
    for key in ("F10", "C23", "F11", "U18", "X09"):
        if key in m:
            tr.write_line(f"  {key}: {json.dumps(m[key], ensure_ascii=False, sort_keys=True)[:400]}")
    if st.cleanup:
        c = st.cleanup
        tr.write_line(f"  cleanup: {c.get('created', 0)} test user(s) created, {c.get('deleted', 0)} deleted, "
                      f"left {c.get('left') or 'none'}, relay keys left {c.get('relay_keys_left')}, "
                      f"errors {len(c.get('errors') or [])}")
    if st.verify_exit is not None:
        tr.write_line(f"  verify-settings after the flips: exit {st.verify_exit}")
    tr.write_line(f"  wall time: {round(time.time() - st.started)}s")
    cfg = Config()
    if cfg.report_path:
        tr.write_line(f"  report: {cfg.report_path}")
    if cfg.gate:
        tr.write_line("  gate (NEWAPI_CONTRACT_GATE=1): " + ("OPEN" if not (s["fail"] or s["skip"] or s["partial"]
                                                                          or s["missing"]) else "CLOSED"))


# Secrets never become pytest ids or reprs.
def pytest_make_parametrize_id(config, val, argname):
    if isinstance(val, Secret):
        return "secret"
    return None


__all__ = ["NAMESPACE"]
