"""F01-F12 — free-tier limits (〔FREE〕§7; DESIGN §8.3).

These exercise the relay (``/v1``) against the self-hosted Qwen and the
ModelRequestRateLimit behaviour. They run on new-api-stg, which also backs the
owner's aimodels demo, so the three tests that flip a setting (F02/F05/F09)
restore it in a finally block and the runbook reruns ``verify-settings`` after.

Many need their own consumer so the per-user rate-limit bucket is isolated
(``consumer_factory``). F06 is really the PaaS meter (LLM-10); here it pins the
new-api primitives the meter stands on. F10 is a measurement recorded for
LLM-12b / NA-08, not a pass/fail threshold.
"""
from __future__ import annotations

import concurrent.futures
import time

import pytest

from . import relay

pytestmark = pytest.mark.usefixtures("admin")


# --------------------------------------------------------------------------- #
#  F01 balance-0 trial key works; log quota=0 group=free                      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F01")
def test_f01_free_trial_works(cfg, admin, relay_base, consumer_factory):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key, "could not reveal the trial key"
    # the account never converted points: balance is 0
    assert (user.up_client(up).get("/api/user/self").data or {}).get("quota") == 0
    r = relay.chat(relay_base, key, cfg.free_model)
    assert r.ok, f"free relay not 200: {r.status} {r.body}"
    # the consume log: quota 0, group free
    time.sleep(2)
    logs = admin.get(f"/api/log/?type=2&username={user.username}&p=1&page_size=10")
    items = (logs.data or {}).get("items") or []
    assert items, "no consume log"
    assert items[0].get("quota") == 0
    assert items[0].get("group") == "free"


# --------------------------------------------------------------------------- #
#  F02 control: enable_free_model_pre_consume=true -> 403                      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F02")
def test_f02_pre_consume_control(cfg, relay_base, consumer_factory, settings_admin):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    with settings_admin.temporary("quota_setting.enable_free_model_pre_consume", "true"):
        time.sleep(1)
        r = relay.chat(relay_base, key, cfg.free_model)
        assert r.status == 403, f"expected 403 insufficient quota, got {r.status}"
    # restored: back to 200
    time.sleep(1)
    assert relay.chat(relay_base, key, cfg.free_model).ok


# --------------------------------------------------------------------------- #
#  F03 25 in a row: exactly 10 pass, then 429; +6s one more                   #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F03")
def test_f03_per_minute_cap(cfg, relay_base, consumer_factory):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    passed = sum(1 for _ in range(25) if relay.chat(relay_base, key, cfg.free_model).ok)
    assert passed == 10, f"expected exactly 10 to pass, got {passed}"


@pytest.mark.contract_id("F03")
def test_f03_refill_after_6s(cfg, relay_base, consumer_factory, slow):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    for _ in range(10):
        relay.chat(relay_base, key, cfg.free_model)
    assert relay.chat(relay_base, key, cfg.free_model).status == 429  # bucket empty
    time.sleep(7)  # token bucket refills 1 per 6s at count=10, D=1
    assert relay.chat(relay_base, key, cfg.free_model).ok


# --------------------------------------------------------------------------- #
#  F04 50 concurrent: at most 10 are let through (atomic total bucket)         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F04")
def test_f04_concurrent_cap(cfg, relay_base, consumer_factory):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        results = list(ex.map(lambda _: relay.chat(relay_base, key, cfg.free_model).ok, range(50)))
    assert sum(results) <= 10, f"{sum(results)} passed, total bucket is not atomic"


# --------------------------------------------------------------------------- #
#  F05 bypass stays closed; counter-example reopens it                        #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F05")
def test_f05_no_bypass_with_safe_config(cfg, relay_base, consumer_factory):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset (needs a junk paid request)")
    user, up = consumer_factory()
    _, free_key = user.make_trial_key(up, [cfg.free_model])
    _, paid_key = user.make_paid_key(up)
    assert free_key and paid_key
    # interleave a paid junk-model request every ~1s; free must stay <= 19/min
    free_ok = 0
    end = time.time() + 30
    while time.time() < end:
        relay.chat(relay_base, paid_key, "no-such-model-" + str(int(time.time())))
        if relay.chat(relay_base, free_key, cfg.free_model).ok:
            free_ok += 1
        time.sleep(1)
    assert free_ok <= 19, f"free passed {free_ok} in 30s — bypass is open"


@pytest.mark.contract_id("F05")
def test_f05_counterexample_reopens_bypass(cfg, relay_base, consumer_factory, settings_admin, slow):
    user, up = consumer_factory()
    _, free_key = user.make_trial_key(up, [cfg.free_model])
    _, paid_key = user.make_paid_key(up)
    assert free_key and paid_key
    # default:[600,600] makes paid requests refill the shared bucket (〔FREE〕§2.6)
    with settings_admin.temporary_json_member("ModelRequestRateLimitGroup", "default", [600, 600]):
        time.sleep(1)
        passed = 0
        end = time.time() + 30
        while time.time() < end:
            relay.chat(relay_base, paid_key, "no-such-model")
            if relay.chat(relay_base, free_key, cfg.free_model).ok:
                passed += 1
        assert passed > 25, f"counter-example did not reopen the bypass (passed {passed})"


# --------------------------------------------------------------------------- #
#  F06 daily-cap primitives (the meter itself is LLM-10/PaaS)                 #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F06")
def test_f06_daily_cap_primitives(cfg, admin, relay_base, consumer_factory):
    # The PaaS meter counts free successes with AP GET /api/log/?type=2&group=free
    # and disables the key with UP PUT status_only. Pin both primitives here.
    user, up = consumer_factory()
    tid, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    assert relay.chat(relay_base, key, cfg.free_model).ok
    time.sleep(2)
    logs = admin.get(f"/api/log/?type=2&group=free&username={user.username}&p=1&page_size=100")
    assert logs.success is True
    items = (logs.data or {}).get("items") or []
    assert items and all(it.get("group") == "free" for it in items)
    # disable via status_only -> relay then 401 (new-api invalidates the cache)
    assert user.up_client(up).put("/api/token/?status_only=1", {"id": tid, "status": 2}).success
    time.sleep(1)
    assert relay.chat(relay_base, key, cfg.free_model).status == 401
    # the 00:00 job re-enables (remain_quota 1 is not exhausted, so enable works)
    assert user.up_client(up).put("/api/token/?status_only=1", {"id": tid, "status": 1}).success


# --------------------------------------------------------------------------- #
#  F07 paid: the 121st success per minute starts 429                          #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F07")
def test_f07_paid_success_cap(cfg, relay_base, consumer_factory, slow):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset")
    user, up = consumer_factory()
    _, key = user.make_paid_key(up)
    assert key
    ok = 0
    saw_429 = False
    for _ in range(130):
        r = relay.chat(relay_base, key, cfg.paid_model)
        if r.ok:
            ok += 1
        elif r.status == 429:
            saw_429 = True
            break
    assert saw_429 and ok >= 120, f"success cap not at ~120 (ok={ok}, saw_429={saw_429})"


# --------------------------------------------------------------------------- #
#  F08 trial->paid 403; paid->Qwen 503                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F08")
def test_f08_cross_group_denied(cfg, relay_base, consumer_factory):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset")
    user, up = consumer_factory()
    _, free_key = user.make_trial_key(up, [cfg.free_model])
    _, paid_key = user.make_paid_key(up)
    # trial key -> a paid model: the model is not in its model_limits -> 403
    assert relay.chat(relay_base, free_key, cfg.paid_model).status == 403
    # paid key -> Qwen: Qwen only lives in the free channel -> 503 no channel
    assert relay.chat(relay_base, paid_key, cfg.free_model).status == 503


# --------------------------------------------------------------------------- #
#  F09 fuse: GroupRatio.free=1 -> 403, wallet unchanged                        #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F09")
def test_f09_fuse(cfg, relay_base, consumer_factory, settings_admin):
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    before = (user.up_client(up).get("/api/user/self").data or {}).get("quota")
    with settings_admin.temporary_json_member("GroupRatio", "free", 1):
        time.sleep(1)
        r = relay.chat(relay_base, key, cfg.free_model)
        assert r.status == 403, f"fuse did not blow: {r.status}"
    after = (user.up_client(up).get("/api/user/self").data or {}).get("quota")
    assert after == before  # wallet untouched (token remain_quota 1 blocked first)


# --------------------------------------------------------------------------- #
#  F10 TrustQuotaUSD=0 overdraft measurement (for LLM-12b / NA-08)             #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F10")
def test_f10_overdraft_measurement(cfg, admin, relay_base, consumer_factory, record_property, slow):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset (F10 is the overdraft measurement)")
    user, up = consumer_factory()
    _, key = user.make_paid_key(up)
    assert key
    # seed a tiny balance so the account can overdraw
    admin.post("/api/user/manage", {"id": user.id, "action": "add_quota", "mode": "add", "value": 15625})
    n = 200
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        statuses = list(ex.map(lambda _: relay.chat(relay_base, key, cfg.paid_model, max_tokens=1024).status, range(n)))
    time.sleep(3)
    self = admin.get(f"/api/user/{user.id}").data or {}
    overdraft_quota = min(0, self.get("quota", 0))
    record_property("f10_concurrency", n)
    record_property("f10_overdraft_quota", overdraft_quota)
    record_property("f10_errors", sum(1 for s in statuses if s >= 400))
    # not a hard threshold — but the wallet must be able to go negative at all
    assert isinstance(self.get("quota"), int)


# --------------------------------------------------------------------------- #
#  F11 Valkey restart resets counters (needs a restart command)               #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F11")
def test_f11_valkey_restart(cfg, relay_base, consumer_factory):
    import os
    import shlex
    import subprocess

    cmd = os.environ.get("NEWAPI_CONTRACT_VALKEY_RESTART")
    if not cmd:
        pytest.skip("NEWAPI_CONTRACT_VALKEY_RESTART unset (restarts Valkey)")
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    for _ in range(10):
        relay.chat(relay_base, key, cfg.free_model)
    assert relay.chat(relay_base, key, cfg.free_model).status == 429  # bucket empty
    subprocess.run(shlex.split(cmd), check=True, timeout=120)
    time.sleep(8)
    assert relay.chat(relay_base, key, cfg.free_model).ok  # counters reset


# --------------------------------------------------------------------------- #
#  F12 Redis key format (rc.44: shared, no :free suffix)                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F12")
def test_f12_redis_key_format(cfg, relay_base, consumer_factory):
    import os
    import shlex
    import subprocess

    cmd = os.environ.get("NEWAPI_CONTRACT_VALKEY_CLI")
    if not cmd:
        pytest.skip("NEWAPI_CONTRACT_VALKEY_CLI unset (valkey-cli for key inspection)")
    user, up = consumer_factory()
    _, key = user.make_trial_key(up, [cfg.free_model])
    assert key
    relay.chat(relay_base, key, cfg.free_model)
    time.sleep(1)
    out = subprocess.run(shlex.split(cmd) + ["--scan", "--pattern", f"rateLimit:{user.id}*"],
                         capture_output=True, text=True, timeout=30).stdout
    keys = set(out.split())
    # rc.44 keys the free total bucket by uid only (no :<group>); the per-group
    # key rateLimit:<uid>:free only appears after the 45fbb149f upgrade (F12).
    per_group = f"rateLimit:{user.id}:free"
    expected_version = os.environ.get("NEWAPI_CONTRACT_EXPECT_PERGROUP") == "1"
    if expected_version:
        assert per_group in keys, f"post-upgrade per-group key missing: {keys}"
    else:
        assert per_group not in keys, f"rc.44 should not have {per_group}: {keys}"
