"""Fixtures, markers and the per-test-id report for the contract suite.

Design choices:
  * Every target is optional (config.py). A test whose target is unset *skips*,
    so ``pytest tests/contract`` collects on a laptop with no cluster and the
    offline self-checks still run. On stg the operator exports the env first.
  * Each test carries ``@pytest.mark.contract_id("C07")``. The report at the
    end of the run lists pass/fail/skip per id (acceptance 1/3) and, if
    ``NEWAPI_CONTRACT_REPORT`` names a path, writes it as JSON for the PR.
  * Admin writes go through a PAT the operator made for the run — never the
    proxy's AP (OPERATIONS〈staging〉: borrowing it rewrites last_used_ip and
    breaks X01 / C24).
"""
from __future__ import annotations

import json
import os

import pytest

from . import newapi, proxy
from .config import Config
from .ids import CONTRACT_IDS


def pytest_configure(config):
    config.addinivalue_line("markers", "contract_id(id): the C/F/X id this test pins")
    config.addinivalue_line("markers", "offline: runs without any cluster (CI)")
    config._contract_results = {}


# --------------------------------------------------------------------------- #
#  config and clients                                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def cfg():
    return Config()


def _read_pat(path):
    import stat

    path = os.path.expanduser(path)
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise AssertionError(f"{path}: a PAT file must be 0600")
    with open(path) as f:
        token = f.read().strip()
    if not token.startswith("nap_"):
        raise AssertionError(f"{path}: expected one nap_ PAT")
    return token


@pytest.fixture(scope="session")
def anon(cfg):
    if not cfg.base:
        pytest.skip("NEWAPI_CONTRACT_BASE unset")
    return newapi.Client(cfg.base)


@pytest.fixture(scope="session")
def admin(cfg):
    if not cfg.have_newapi:
        pytest.skip("NEWAPI_CONTRACT_BASE / NEWAPI_CONTRACT_ADMIN_PAT_FILE unset")
    return newapi.Client(cfg.base, token=_read_pat(cfg.admin_pat_file))


@pytest.fixture(scope="session")
def base_client(cfg):
    if not cfg.base:
        pytest.skip("NEWAPI_CONTRACT_BASE unset")
    return newapi.Client(cfg.base)


@pytest.fixture
def test_user(cfg, admin, base_client):
    """A fresh consumer account, cleaned up afterwards (function scope)."""
    from .users import create_test_user

    user = create_test_user(admin, base_client, cfg.user_prefix)
    try:
        yield user
    finally:
        user.cleanup()


class Consumer:
    """A logged-in test user with a UP, shared by the read-only C-tests to keep
    the shared 127.0.0.1 CT budget (20/20min over the port-forward) small."""

    def __init__(self, user, up_token):
        self.user = user
        self.up_token = up_token

    @property
    def jwt(self):
        return self.user.jwt_client()

    @property
    def up(self):
        return self.user.up_client(self.up_token)


@pytest.fixture(scope="session")
def consumer(cfg, admin, base_client):
    from .users import create_test_user

    user = create_test_user(admin, base_client, cfg.user_prefix)
    user.login()
    up_token, _ = user.create_up()
    try:
        yield Consumer(user, up_token)
    finally:
        user.cleanup(up_token)


@pytest.fixture
def consumer_factory(cfg, admin, base_client):
    """Make fresh logged-in consumers (each with its own new-api rate-limit
    bucket) and clean them all up. F03/F04/F07 need isolated buckets."""
    from .users import create_test_user

    made = []

    def make():
        user = create_test_user(admin, base_client, cfg.user_prefix)
        user.login()
        up_token, _ = user.create_up()
        made.append((user, up_token))
        return user, up_token

    try:
        yield make
    finally:
        for user, up_token in made:
            user.cleanup(up_token)


@pytest.fixture(scope="session")
def relay_base(cfg):
    if not cfg.have_relay:
        pytest.skip("NEWAPI_CONTRACT_RELAY_BASE / admin PAT unset")
    return cfg.relay_base


@pytest.fixture(scope="session")
def settings_admin(cfg, admin):
    """Root option read/write for the F control tests; gated on settings_admin."""
    if not cfg.settings_admin:
        pytest.skip("NEWAPI_CONTRACT_SETTINGS_ADMIN != 1 (needs a root option:write PAT)")
    from .settings import SettingsAdmin

    return SettingsAdmin(admin)


@pytest.fixture(scope="session")
def slow(cfg):
    """Skip a long-running assertion when NEWAPI_CONTRACT_SKIP_SLOW=1."""
    if cfg.skip_slow:
        pytest.skip("NEWAPI_CONTRACT_SKIP_SLOW=1")
    return True


@pytest.fixture(scope="session")
def proxy_cron(cfg):
    if not cfg.have_proxy_cron:
        pytest.skip("NEWAPI_CONTRACT_PROXY_BASE / WOOW_NEWAPI_PROXY_AUTH_CRON unset")
    return proxy.ProxyClient(cfg.proxy_base, cfg.auth_cron)


@pytest.fixture(scope="session")
def proxy_web(cfg):
    if not cfg.have_proxy_web:
        pytest.skip("NEWAPI_CONTRACT_PROXY_BASE / WOOW_NEWAPI_PROXY_AUTH_WEB unset")
    return proxy.ProxyClient(cfg.proxy_base, cfg.auth_web)


@pytest.fixture(scope="session")
def psql(cfg):
    """Run read-only SQL on new-api-stg's Postgres; returns stripped stdout."""
    if not cfg.have_psql:
        pytest.skip("NEWAPI_CONTRACT_PSQL unset")
    import subprocess

    def run(statement):
        proc = subprocess.run(
            cfg.psql_cmd + [statement], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise AssertionError(f"psql failed: {proc.stderr.strip()[:200]}")
        return proc.stdout.strip()

    return run


# --------------------------------------------------------------------------- #
#  per-test-id report                                                         #
# --------------------------------------------------------------------------- #
def _contract_id_of(item):
    m = item.get_closest_marker("contract_id")
    return m.args[0] if m and m.args else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    cid = _contract_id_of(item)
    if not cid:
        return
    results = item.config._contract_results
    rec = results.setdefault(cid, {"outcome": "pass", "phases": []})
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if report.skipped:
            # keep the first non-skip outcome, but a pure skip stays "skip"
            if rec["outcome"] == "pass" and not rec["phases"]:
                rec["outcome"] = "skip"
        elif report.failed:
            rec["outcome"] = "fail"
        rec["phases"].append(report.when)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    results = getattr(config, "_contract_results", {})
    if not results:
        return
    tr = terminalreporter
    tr.write_sep("=", "contract ids (C/F/X)")
    counts = {"pass": 0, "fail": 0, "skip": 0, "missing": 0}
    rows = []
    for cid in sorted(CONTRACT_IDS):
        rec = results.get(cid)
        status = rec["outcome"] if rec else "missing"
        counts[status] = counts.get(status, 0) + 1
        rows.append((cid, status, CONTRACT_IDS[cid]))
    for cid, status, subject in rows:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip", "missing": "----"}[status]
        tr.write_line(f"  {mark:4}  {cid}  {subject}")
    tr.write_line(
        f"  total: {counts['pass']} pass, {counts['fail']} fail, "
        f"{counts['skip']} skip, {counts['missing']} not collected"
    )
    path = os.environ.get("NEWAPI_CONTRACT_REPORT")
    if path:
        payload = {
            "summary": counts,
            "ids": {cid: (results.get(cid, {}).get("outcome", "missing")) for cid in CONTRACT_IDS},
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        tr.write_line(f"  report written to {path}")
