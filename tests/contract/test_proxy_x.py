"""X01-X09 — the newapi-proxy contract (DESIGN §8.3; proxy README).

These sign their own requests to the stg proxy exactly as Odoo does (the suite
port-forwards svc/newapi-proxy itself; a port-forward bypasses the
NetworkPolicy and the proxy authenticates by HMAC, not by source address).
The ``user`` identity is a suite-owned ws-prefixed account (``proxy_user``,
tenant.py) on the suite's own synthetic IPs — never a sandbox tenant.

X06's "upstream stalled" holds a row lock on that account so new-api's
add_quota blocks (kube.row_lock; nothing is written). X06's "upstream closed"
is a staged run (``-m upstream_down`` with newapi-master scaled to 0). X09
kills a proxy pod mid add_quota (disruptive window).
"""
from __future__ import annotations

import concurrent.futures
import json
import time

import pytest

from .config import AP_IP, OP_IP
from .proxy import TransportError
from .redact import assert_secret_shape

NO_SUCH_ID = 2147483647  # an id no user has: a policy regression cannot hit a real account


# --------------------------------------------------------------------------- #
#  X01 each identity's synthetic IP is what new-api records                   #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.contract_id("X01")
def test_x01_synthetic_ips(ip_observation):
    obs = ip_observation
    assert obs["ap_ip"] == AP_IP, f"AP last_used_ip {obs['ap_ip']!r} ({obs['ap_problem']})"
    assert obs["op_ip"] == OP_IP, f"OP last_used_ip {obs['op_ip']!r} ({obs['op_problem']})"
    assert obs["tenant_up_ip"] == obs["tenant_ip"], f"tenant UP last_used_ip {obs['tenant_up_ip']!r}"
    assert obs["tenant_session_ip"] == obs["tenant_ip"], f"tenant session ip {obs['tenant_session_ip']!r}"


# --------------------------------------------------------------------------- #
#  X02 forged XFF / X-Real-IP / Forwarded are ignored                         #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.contract_id("X02")
def test_x02_forged_headers_ignored(forged_observation):
    obs = forged_observation
    assert obs["session_ip"] == obs["tenant_ip"], f"forged login: session ip {obs['session_ip']!r}"
    assert obs["up_ip"] == obs["tenant_ip"], f"forged UP call: last_used_ip {obs['up_ip']!r} ({obs['up_problem']})"
    assert obs["ap_ip"] == AP_IP, f"forged AP call: last_used_ip {obs['ap_ip']!r} ({obs['ap_problem']})"


# --------------------------------------------------------------------------- #
#  X03 non-whitelisted path -> 403 policy route                               #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X03")
@pytest.mark.parametrize("identity,method,path", [
    ("ap", "GET", "/api/option/"),      # option is op-only
    ("ap", "GET", "/api/channel/"),     # never whitelisted
    ("anon", "GET", "/v1/models"),      # /v1 never through the proxy
    ("ap", "POST", "/api/user/topup"),  # topup blocked
    ("ap", "PUT", "/api/user/"),        # PUT /api/user/ is not a route (group changes stay manual)
])
def test_x03_blocked_paths(proxy_cron, fixture_sink, identity, method, path):
    r = proxy_cron.send(method, path, identity=identity)
    assert r.status == 403 and r.proxy_error == "policy" and r.reason == "route", f"{identity} {method} {path}: {r}"
    if path == "/api/channel/":
        fixture_sink.capture("proxy_policy_route", r, route="channel")


# --------------------------------------------------------------------------- #
#  X04 missing / wrong / expired / replayed / unknown-kid signature -> 401;   #
#  a WEB key on an ap route -> 403 key_scope                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X04")
def test_x04_unsigned(proxy_cron, fixture_sink):
    r = proxy_cron.send("GET", "/api/status", identity="anon", signed=False)
    assert r.status == 401 and r.proxy_error == "auth" and r.reason == "auth", f"unsigned: {r}"
    fixture_sink.capture("proxy_auth", r, route="status")


@pytest.mark.contract_id("X04")
@pytest.mark.parametrize("case", ["expired_ts", "future_ts", "wrong_sig", "unknown_kid"])
def test_x04_bad_signatures(proxy_cron, case):
    kw = {"expired_ts": {"ts_offset": -31}, "future_ts": {"ts_offset": 31},
          "wrong_sig": {"secret": b"contract-wrong-secret-0123456789abcdef"},
          "unknown_kid": {"kid": "contract-unknown-kid"}}[case]
    r = proxy_cron.send("GET", "/api/status", identity="anon", **kw)
    assert r.status == 401 and r.proxy_error == "auth", f"{case}: {r}"


@pytest.mark.contract_id("X04")
def test_x04_replay(proxy_cron):
    from . import signing

    nonce = signing.new_nonce()
    first = proxy_cron.send("GET", "/api/status", identity="anon", nonce=nonce)
    assert first.status == 200, f"first request: {first}"
    second = proxy_cron.send("GET", "/api/status", identity="anon", nonce=nonce)
    assert second.status == 401 and second.proxy_error == "auth", f"replay: {second}"


@pytest.mark.contract_id("X04")
def test_x04_web_key_scope(proxy_web, fixture_sink):
    r = proxy_web.send("GET", f"/api/user/{NO_SUCH_ID}", identity="ap")
    assert r.status == 403 and r.proxy_error == "policy" and r.reason == "key_scope", f"WEB key as ap: {r}"
    fixture_sink.capture("proxy_policy_key_scope", r, route="user.get")


# --------------------------------------------------------------------------- #
#  X05 ap carrying its own Authorization -> 400                               #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X05")
def test_x05_ap_with_authorization(proxy_cron, fixture_sink):
    r = proxy_cron.send("GET", f"/api/user/{NO_SUCH_ID}", identity="ap", authorization="Bearer nap_" + "0" * 43)
    assert r.status == 400 and r.proxy_error == "bad_request" and r.reason == "authorization_not_allowed", f"{r}"
    fixture_sink.capture("proxy_bad_request_authorization_not_allowed", r, route="user.get")


# --------------------------------------------------------------------------- #
#  X06 upstream closed -> connect; upstream stalled -> timeout                #
# --------------------------------------------------------------------------- #
@pytest.mark.upstream_down
@pytest.mark.contract_id("X06")
def test_x06_upstream_down(cfg, proxy_cron, fixture_sink):
    if not cfg.upstream_down:
        pytest.skip("staged: scale newapi-master to 0, then NEWAPI_CONTRACT_UPSTREAM_DOWN=1 pytest -m upstream_down")
    r = proxy_cron.send("GET", "/api/status", identity="anon")
    assert r.status == 502 and r.proxy_error == "connect", f"master down: {r}"
    fixture_sink.capture("proxy_connect", r, route="status")


@pytest.mark.contract_id("X06")
def test_x06_upstream_stalled(kube, proxy_cron, proxy_user, fixture_sink, contract_metric):
    """Hold a row lock on the suite's own user so new-api's add_quota blocks;
    a 2-second read budget then ends in 504 timeout (sent, outcome unknown)."""
    if kube is None:
        pytest.skip("needs NEWAPI_CONTRACT_KUBECTL (the row lock)")
    lock = kube.row_lock(proxy_user.id, proxy_user.username, 10)
    try:
        time.sleep(3)  # let the lock session start
        body = {"id": proxy_user.id, "action": "add_quota", "mode": "add", "value": 1}
        t0 = time.time()
        r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body, read_timeout=2, timeout=20)
        contract_metric("X06", {"stalled_answer_s": round(time.time() - t0, 2)})
        assert r.status == 504 and r.proxy_error == "timeout", f"stalled upstream: {r}"
        fixture_sink.capture("proxy_timeout", r, route="manage")
    finally:
        lock.wait(timeout=30)


# --------------------------------------------------------------------------- #
#  X07 reveal: fresh key 200; >120 s old 403 reveal_window; other denials     #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.contract_id("X07")
def test_x07_reveal_window(cfg, slow, proxy_web, proxy_user, fixture_sink):
    t = proxy_user
    web = lambda m, p, **kw: proxy_web.send(m, p, identity="user", client_ip=t.ip,  # noqa: E731
                                            authorization="Bearer " + t.up, **kw)
    name = t.wp_name()
    made = []
    try:
        created = web("POST", "/api/token/", body=t.token_body(name))
        assert created.status == 200 and created.success is True, f"create via proxy: {created}"
        listing = web("GET", "/api/token/", query_pairs=[("p", "1"), ("page_size", "100")])
        items = (listing.data or {}).get("items") or []
        tid = next((it["id"] for it in items if it.get("name") == name), None)
        assert tid, "created token not in the list"
        made.append(tid)
        fresh = web("POST", f"/api/token/{tid}/key")
        assert fresh.status == 200, f"fresh reveal: {fresh}"
        assert_secret_shape((fresh.data or {}).get("key"), length=48, what="revealed key")
        # a relayed pre-check: the UP is not valid -> new-api's 401 comes back as is
        bad = proxy_web.send("POST", f"/api/token/{tid}/key", identity="user", client_ip=t.ip,
                             authorization="Bearer nap_" + "0" * 43)
        assert bad.status == 401 and bad.precheck == "reveal" and bad.code == "AUTH_UNAUTHORIZED", f"{bad}"
        fixture_sink.capture("proxy_precheck_relayed_reveal_401", bad, route="token.reveal")
        nf = web("POST", f"/api/token/{NO_SUCH_ID}/key")
        assert nf.status == 403 and nf.proxy_error == "denied" and nf.reason == "reveal_not_found", f"{nf}"
        fixture_sink.capture("proxy_denied_reveal_not_found", nf, route="token.reveal")
        # a key not named wp_<n>_xxxx (made directly, not through the proxy)
        other_id, _, _ = t.user.create_relay_token(t.up, f"wct_{t.id}_x07")
        made.append(other_id)
        rn = web("POST", f"/api/token/{other_id}/key")
        assert rn.status == 403 and rn.proxy_error == "denied" and rn.reason == "reveal_name", f"{rn}"
        fixture_sink.capture("proxy_denied_reveal_name", rn, route="token.reveal")
        time.sleep(125)
        old = web("POST", f"/api/token/{tid}/key")
        assert old.status == 403 and old.proxy_error == "denied" and old.reason == "reveal_window", f"{old}"
        fixture_sink.capture("proxy_denied_reveal_window", old, route="token.reveal")
    finally:
        # no DELETE /api/token/{id} route in the proxy yet (LLM-02): delete directly with the UP
        direct = t.user.up_client(t.up)
        for tid in made:
            r = direct.delete(f"/api/token/{tid}")
            if r.success is True:
                t.user.relay_tokens.discard(tid)


# --------------------------------------------------------------------------- #
#  X08 body policy: override / subtract / non-prefix target / non-prefix       #
#  create / value bounds -> 403                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("X08")
@pytest.mark.parametrize("body,reason", [
    ({"id": NO_SUCH_ID, "action": "add_quota", "mode": "override", "value": 1}, "manage_mode"),
    ({"id": NO_SUCH_ID, "action": "add_quota", "mode": "subtract", "value": 1}, "manage_mode"),
    ({"id": NO_SUCH_ID, "action": "add_quota", "mode": "add", "value": 0}, "manage_value"),
    ({"id": NO_SUCH_ID, "action": "add_quota", "mode": "add", "value": 156250001}, "manage_value"),
    ({"id": NO_SUCH_ID, "action": "disable", "mode": "add", "value": 1}, "manage_action"),
    ({"id": NO_SUCH_ID, "action": "add_quota", "Mode": "add", "value": 1}, "manage_fields"),
])
def test_x08_manage_body_policy(proxy_cron, fixture_sink, body, reason):
    r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "policy" and r.reason == reason, f"{body}: {r}"
    if reason == "manage_mode" and body["mode"] == "override":
        fixture_sink.capture("proxy_policy_manage_mode", r, route="manage")


@pytest.mark.contract_id("X08")
def test_x08_create_non_prefix_username(proxy_cron):
    body = {"username": "root", "password": "P" * 40, "display_name": "PaaS 0", "role": 1}
    r = proxy_cron.send("POST", "/api/user/", identity="ap", body=body)
    assert r.status == 403 and r.proxy_error == "policy" and r.reason == "user_create_username", f"{r}"


@pytest.mark.contract_id("X08")
def test_x08_manage_targets(lab, consumer, proxy_cron, fixture_sink):
    """A valid add_quota body is pre-checked against the target (AP GET
    /api/user/{id}): a role-1 user without the PaaS prefix -> denied
    manage_target; an unknown id -> denied manage_not_found; an account the AP
    may not read (root) -> new-api's refusal relayed with X-Woow-Proxy-Precheck."""
    ok = {"action": "add_quota", "mode": "add", "value": 1}
    before = (lab.admin.get(f"/api/user/{consumer.user.id}").data or {}).get("quota")
    r = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=dict(ok, id=consumer.user.id))
    assert r.status == 403 and r.proxy_error == "denied" and r.reason == "manage_target", f"wct target: {r}"
    fixture_sink.capture("proxy_denied_manage_target", r, route="manage")
    assert (lab.admin.get(f"/api/user/{consumer.user.id}").data or {}).get("quota") == before, "quota moved"
    nf = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=dict(ok, id=NO_SUCH_ID))
    assert nf.status == 403 and nf.proxy_error == "denied" and nf.reason == "manage_not_found", f"unknown id: {nf}"
    fixture_sink.capture("proxy_denied_manage_not_found", nf, route="manage")
    root_id = lab.admin_id or 1
    rel = proxy_cron.send("POST", "/api/user/manage", identity="ap", body=dict(ok, id=root_id))
    assert rel.precheck == "manage" and rel.success is False and rel.message_key, f"root target: {rel}"
    fixture_sink.capture("proxy_precheck_relayed_manage", rel, route="manage")


# --------------------------------------------------------------------------- #
#  X09 add_quota handed upstream, proxy pod killed before the reply           #
# --------------------------------------------------------------------------- #
@pytest.mark.disruptive
@pytest.mark.contract_id("X09")
def test_x09_kill_mid_add_quota(cfg, lab, kube, proxy_user, disruptive, fixture_sink, contract_metric):
    """Deterministic: a row lock stalls new-api's add_quota on the suite's own
    user; the request goes through a port-forward to one named proxy pod; once
    Postgres shows the UPDATE waiting (= the proxy forwarded it), that pod is
    force-deleted. The client must see a dropped connection (send-then-drop =
    Unknown), and the type=1 log / quota delta shows whether it executed."""
    from . import proxy as proxymod
    from .kube import KubeError

    if kube is None or not cfg.auth_cron:
        pytest.skip("needs NEWAPI_CONTRACT_KUBECTL and the CRON key")
    for _ in range(24):  # F11's Valkey restart makes the proxies briefly unready
        pods = kube.proxy_pods()
        if len(pods) >= 2:
            break
        time.sleep(5)
    assert len(pods) >= 2, f"needs 2 Ready proxy replicas (have {len(pods)}): the other keeps stg served"
    pod = pods[0]
    uid, value = proxy_user.id, 1
    t_start = int(time.time())
    snap = lab.admin.get(f"/api/user/{uid}").data or {}
    before = (snap.get("quota") or 0) + (snap.get("used_quota") or 0)
    lock = kube.row_lock(uid, proxy_user.username, 25)
    result = {}
    try:
        with kube.port_forward(f"pod/{pod}", 8080) as origin:
            pc = proxymod.ProxyClient(origin, cfg.auth_cron)
            time.sleep(2)

            def call():
                try:
                    return pc.send("POST", "/api/user/manage", identity="ap",
                                   body={"id": uid, "action": "add_quota", "mode": "add", "value": value},
                                   read_timeout=20, timeout=40)
                except TransportError as e:
                    return e

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(call)
                waiting = False
                for _ in range(40):  # Postgres: an UPDATE on users waiting on our lock
                    # AdjustUserQuota's SELECT ... FOR UPDATE on "users" (model/locking.go)
                    rows = kube.psql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                     "AND query ILIKE '%\"users\"%FOR UPDATE%'")
                    if rows and int(rows[0]) > 0:
                        waiting = True
                        break
                    time.sleep(0.25)
                result["forwarded"] = waiting
                assert waiting, "the add_quota never reached new-api (no waiting UPDATE)"
                kube.force_delete_pod(pod)
                outcome = fut.result(timeout=60)
    finally:
        lock.wait(timeout=60)
    time.sleep(3)
    snap = lab.admin.get(f"/api/user/{uid}").data or {}
    delta = (snap.get("quota") or 0) + (snap.get("used_quota") or 0) - before
    logs = lab.admin.get(f"/api/log/?type=1&username={proxy_user.username}&p=1&page_size=20").data or {}
    executed = any(isinstance(lg, dict) and value == _quota_param(lg) and (lg.get("created_at") or 0) >= t_start - 1
                   for lg in logs.get("items") or [])
    if isinstance(outcome, TransportError):
        kind = outcome.exc_type
        fixture_sink.capture_transport("transport_send_then_drop_manage", kind, after_send=True, route="manage",
                                       executed=executed)
    else:
        kind = f"proxy {outcome.status} {outcome.proxy_error}"
    result.update({"client_saw": kind, "quota_delta": delta, "executed_per_log": executed, "pod": pod})
    contract_metric("X09", result)
    try:
        kube.rollout_status("deploy/newapi-proxy", timeout=180)
    except KubeError:
        pass
    assert isinstance(outcome, TransportError) or (outcome.proxy_error in ("reset", "timeout")), \
        f"a request already sent must not end as a definite answer: {kind}"
    assert delta in (0, value) and executed == (delta == value), f"log and quota disagree: {result}"


def _quota_param(log):
    other = log.get("other")
    if isinstance(other, str):
        try:
            other = json.loads(other or "{}")
        except ValueError:
            return None
    return (((other or {}).get("op") or {}).get("params") or {}).get("quota")
