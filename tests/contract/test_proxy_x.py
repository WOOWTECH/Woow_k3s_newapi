"""X01-X09 — the newapi-proxy contract (DESIGN §8.3; proxy README).

These sign their own requests to the stg proxy exactly as Odoo does. Most are
pure proxy policy (no upstream needed); X01/X02 read the DB to confirm the
synthetic XFF landed; X06/X07/X09 need the operator to set the stage first and
skip otherwise.

The proxy only admits sandbox Odoo pods, so on stg these run from inside a
sandbox pod (or with a one-off NetworkPolicy + port-forward); the transport is
the same either way.
"""
from __future__ import annotations

import time

import pytest

AP_SYNTHETIC_IP = "198.18.0.1"


# --------------------------------------------------------------------------- #
#  X01 synthetic IP in last_used_ip                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X01")
def test_x01_synthetic_ip(cfg, proxy_cron, psql):
    r = proxy_cron.send("GET", "/api/user/1", identity="ap")
    assert r.status in (200, 403, 404)  # reached new-api as AP
    assert r.proxy_error is None
    time.sleep(2)
    ip = psql(
        "SELECT last_used_ip FROM user_access_tokens WHERE user_id = "
        f"(SELECT id FROM users WHERE username = '{cfg.svc_username}') "
        "ORDER BY last_used_at DESC LIMIT 1"
    )
    assert ip == AP_SYNTHETIC_IP, f"AP last_used_ip = {ip!r}"


# --------------------------------------------------------------------------- #
#  X02 forged XFF is ignored                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X02")
def test_x02_forged_xff_ignored(cfg, proxy_cron, psql):
    r = proxy_cron.send(
        "GET", "/api/user/1", identity="ap",
        extra_headers={"X-Forwarded-For": "114.32.21.18", "X-Real-IP": "114.32.21.18"},
    )
    assert r.status in (200, 403, 404) and r.proxy_error is None
    time.sleep(2)
    ip = psql(
        "SELECT last_used_ip FROM user_access_tokens WHERE user_id = "
        f"(SELECT id FROM users WHERE username = '{cfg.svc_username}') "
        "ORDER BY last_used_at DESC LIMIT 1"
    )
    assert ip == AP_SYNTHETIC_IP, f"forged XFF leaked: last_used_ip = {ip!r}"


# --------------------------------------------------------------------------- #
#  X03 non-whitelisted path -> 403 policy                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X03")
@pytest.mark.parametrize(
    "identity,method,path",
    [
        ("ap", "GET", "/api/option/"),      # option is op-only
        ("ap", "GET", "/api/channel/"),     # never whitelisted
        ("anon", "GET", "/v1/models"),      # /v1 never through the proxy
        ("ap", "POST", "/api/user/topup"),  # topup path blocked
    ],
)
def test_x03_blocked_paths(proxy_cron, identity, method, path):
    r = proxy_cron.send(method, path, identity=identity)
    assert r.status == 403
    assert r.proxy_error == "policy"


# --------------------------------------------------------------------------- #
#  X04 bad/missing/expired/replayed signature -> 401                          #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X04")
def test_x04_unsigned(proxy_cron):
    r = proxy_cron.send("GET", "/api/status", identity="anon", signed=False)
    assert r.status == 401 and r.proxy_error == "auth"


@pytest.mark.contract_id("X04")
def test_x04_expired_ts(proxy_cron):
    r = proxy_cron.send("GET", "/api/status", identity="anon", ts_offset=-31)
    assert r.status == 401 and r.proxy_error == "auth"


@pytest.mark.contract_id("X04")
def test_x04_replay(proxy_cron):
    from . import signing

    nonce = signing.new_nonce()
    first = proxy_cron.send("GET", "/api/status", identity="anon", nonce=nonce)
    assert first.status == 200, f"first request should pass: {first}"
    second = proxy_cron.send("GET", "/api/status", identity="anon", nonce=nonce)
    assert second.status == 401 and second.proxy_error == "auth"  # replay across replicas


# --------------------------------------------------------------------------- #
#  X05 ap carrying its own Authorization -> 400                               #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X05")
def test_x05_ap_with_authorization(proxy_cron):
    r = proxy_cron.send("GET", "/api/user/1", identity="ap",
                        authorization="Bearer nap_" + "0" * 43)
    assert r.status == 400 and r.proxy_error == "bad_request"


# --------------------------------------------------------------------------- #
#  X06 upstream closed -> connect; stalled -> timeout                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X06")
def test_x06_upstream_down(cfg, proxy_cron):
    if not cfg.upstream_down:
        pytest.skip("NEWAPI_CONTRACT_UPSTREAM_DOWN!=1 (scale newapi-master to 0 first)")
    r = proxy_cron.send("GET", "/api/status", identity="anon")
    assert r.status == 502 and r.proxy_error == "connect"


@pytest.mark.contract_id("X06")
def test_x06_upstream_slow(cfg, proxy_cron):
    if not cfg.upstream_slow:
        pytest.skip("NEWAPI_CONTRACT_UPSTREAM_SLOW!=1 (inject a per-response delay first)")
    r = proxy_cron.send("GET", "/api/status", identity="anon", read_timeout=1, timeout=20)
    assert r.status == 504 and r.proxy_error == "timeout"


# --------------------------------------------------------------------------- #
#  X07 fresh key reveal 200; >120s reveal 403 (needs a tenant UP)             #
# --------------------------------------------------------------------------- #
def _tenant_up(cfg):
    import os
    import stat

    path = os.path.expanduser(cfg.tenant_up_file)
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise AssertionError(f"{path}: UP file must be 0600")
    with open(path) as f:
        return f.read().strip()


@pytest.mark.contract_id("X07")
def test_x07_reveal_window(cfg, proxy_cron, slow):
    import json
    import re
    import secrets

    if not cfg.have_tenant_up:
        pytest.skip("NEWAPI_CONTRACT_TENANT_UP_FILE / _USERNAME unset (needs a provisioned ws account)")
    up = _tenant_up(cfg)
    m = re.search(r"\d+", cfg.tenant_username)
    acct = m.group(0) if m else "0"
    ip = cfg.tenant_ip
    auth = "Bearer " + up
    name = f"wp_{acct}_{secrets.token_hex(2)}"
    body = json.dumps({
        "name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
        "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
        "cross_group_retry": False, "allow_user_quota_query": False,
    }).encode()
    created = proxy_cron.send("POST", "/api/token/", identity="user", client_ip=ip,
                              authorization=auth, body=body)
    assert created.status == 200, f"token create via proxy: {created}"
    listing = proxy_cron.send("GET", "/api/token/", identity="user", client_ip=ip,
                              authorization=auth, query_pairs=[("p", "1"), ("page_size", "100")])
    items = (listing.data or {}).get("items") or []
    tid = next((t["id"] for t in items if t.get("name") == name), None)
    assert tid, "created token not found"
    # fresh reveal: 200
    fresh = proxy_cron.send("POST", f"/api/token/{tid}/key", identity="user", client_ip=ip, authorization=auth)
    assert fresh.status == 200, f"fresh reveal: {fresh}"
    # >120s later: 403 denied reveal_window
    time.sleep(125)
    old = proxy_cron.send("POST", f"/api/token/{tid}/key", identity="user", client_ip=ip, authorization=auth)
    assert old.status == 403 and old.proxy_error == "denied" and old.reason == "reveal_window"


# --------------------------------------------------------------------------- #
#  X08 body policy negatives -> 403                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X08")
def test_x08_manage_mode_policy(proxy_cron):
    import json

    for mode in ("override", "subtract"):
        body = json.dumps({"id": 2, "action": "add_quota", "mode": mode, "value": 1}).encode()
        r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body)
        assert r.status == 403 and r.proxy_error == "policy", f"mode {mode}: {r}"


@pytest.mark.contract_id("X08")
def test_x08_manage_value_bounds(proxy_cron):
    import json

    body = json.dumps({"id": 2, "action": "add_quota", "mode": "add", "value": 0}).encode()
    r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "policy"


@pytest.mark.contract_id("X08")
def test_x08_create_non_prefix_username(proxy_cron):
    import json

    body = json.dumps({"username": "root", "password": "P" * 40, "display_name": "PaaS 0", "role": 1}).encode()
    r = proxy_cron.send("POST", "/api/user/", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "policy"


@pytest.mark.contract_id("X08")
def test_x08_put_user_not_whitelisted(proxy_cron):
    import json

    body = json.dumps({"id": 2, "username": "x", "display_name": "PaaS 0", "group": "free"}).encode()
    r = proxy_cron.send("PUT", "/api/user/", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "policy"


@pytest.mark.contract_id("X08")
def test_x08_manage_non_prefix_target(proxy_cron):
    import json

    # Valid body, but target id 1 (root) fails the AP precheck prefix/role/group:
    # 403 denied manage_target, the request never reaches add_quota.
    body = json.dumps({"id": 1, "action": "add_quota", "mode": "add", "value": 1}).encode()
    r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "denied" and r.reason == "manage_target"


# --------------------------------------------------------------------------- #
#  X09 add_quota handed upstream, proxy killed -> client Unknown              #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X09")
def test_x09_kill_mid_add_quota(cfg, proxy_cron):
    import concurrent.futures
    import json
    import subprocess

    from . import proxy as proxymod

    if not (cfg.x09_kill_cmd and cfg.have_tenant_up):
        pytest.skip("NEWAPI_CONTRACT_X09_KILL / tenant UP unset (orchestrated kill)")
    import re

    acct = re.search(r"\d+", cfg.tenant_username).group(0)
    body = json.dumps({"id": int(acct), "action": "add_quota", "mode": "add", "value": 1}).encode()

    def call():
        return proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body,
                               read_timeout=20, timeout=30)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(call)
        time.sleep(0.3)
        subprocess.run(cfg.x09_kill_cmd, check=False, timeout=60)
        try:
            r = fut.result(timeout=40)
            # proxy killed after forwarding -> reset/timeout (Unknown), never a
            # clean NotExecuted connect for a request that was already sent.
            assert r.proxy_error in ("reset", "timeout", "internal"), f"got {r}"
        except proxymod.TransportError:
            pass  # the connection dropped: also Unknown for the client
