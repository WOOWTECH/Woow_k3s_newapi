"""F01-F12 — free-tier limits (〔FREE〕§7; DESIGN §8.3).

These exercise the relay (``/v1``) against the self-hosted Qwen and the
ModelRequestRateLimit behaviour. One user serves them all (``f_consumer``:
one UP, one trial key, one paid key, one batch reveal — 5 CT in total); the
per-user buckets are shared, so each test first waits for the free token
bucket to refill (``wait_full``, ~60 s) instead of making a new user.

new-api-stg also backs the owner's aimodels demo, so the tests that change
shared state are ``disruptive`` (NEWAPI_CONTRACT_DISRUPTIVE=1, announced
window, run last): F02/F05-counter-example/F09 flip a global setting
(settings.py restores the *declared* value and the session runs
verify-settings), F11 restarts Valkey.

F06 pins only the new-api primitives the PaaS daily meter (LLM-10) stands on:
it reports ``known_partial``. F10 is a measurement for LLM-12b / NA-08 with
its own model, opt-in and spend cap.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from . import relay
from .ids import KNOWN_PARTIAL
from .redact import assert_secret_shape

pytestmark = pytest.mark.usefixtures("lab")

POINT = 15625  # quota per point (D5)
LONG_PROMPT = [{"role": "user", "content": "Count from 1 to 4000, separated by single spaces. "
                                            "Output only the numbers, nothing else."}]


def _free(fc, relay_base, cfg, **kw):
    r = relay.chat(relay_base, fc.trial_key, cfg.free_model, max_tokens=kw.pop("max_tokens", 1), **kw)
    fc.touched()
    return r


@pytest.fixture
def fc(f_consumer):
    assert_secret_shape(f_consumer.trial_key, length=48, what="trial key")
    assert_secret_shape(f_consumer.paid_key, length=48, what="paid key")
    f_consumer.wait_full()
    return f_consumer


# --------------------------------------------------------------------------- #
#  F01 balance-0 trial key works; log quota=0 group=free; max_tokens cap      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F01")
def test_f01_free_trial_works(cfg, admin, relay_base, fc):
    fc.set_wallet(admin, 0)
    r = _free(fc, relay_base, cfg)
    assert r.ok, f"free relay at balance 0: {r.error_summary()}"
    time.sleep(2)
    logs = admin.get(f"/api/log/?type=2&username={fc.user.username}&p=1&page_size=10")
    items = (logs.data or {}).get("items") or []
    assert items, "no consume log"
    assert items[0].get("quota") == 0 and items[0].get("group") == "free", \
        f"consume log quota={items[0].get('quota')} group={items[0].get('group')}"
    assert fc.quota() == 0, "the wallet moved"


@pytest.mark.slow
@pytest.mark.contract_id("F01")
def test_f01_free_channel_caps_max_tokens(cfg, relay_base, fc, slow, contract_metric, contract_partial):
    """U18: the free channel's param_override {"max_tokens": 2048}
    (deploy/pricing/channels.json) — does it cap / overwrite the client's value?"""
    big = _free(fc, relay_base, cfg, max_tokens=8192, messages=LONG_PROMPT, timeout=240)
    assert big.ok, f"long free request: {big.error_summary()}"
    small = _free(fc, relay_base, cfg, max_tokens=4, messages=LONG_PROMPT, timeout=240)
    assert small.ok, f"short free request: {small.error_summary()}"
    obs = {"client_8192": {"completion_tokens": big.completion_tokens, "finish_reason": big.finish_reason},
           "client_4": {"completion_tokens": small.completion_tokens, "finish_reason": small.finish_reason}}
    obs["override_replaces_client_value"] = bool(small.completion_tokens and small.completion_tokens > 4)
    contract_metric("U18", obs)
    assert isinstance(big.completion_tokens, int), "no usage.completion_tokens"
    assert big.completion_tokens <= 2048, f"param_override did not cap max_tokens: {big.completion_tokens} > 2048"
    if big.finish_reason != "length" and big.completion_tokens < 2000:
        contract_partial(f"the model stopped by itself at {big.completion_tokens} tokens "
                         f"({big.finish_reason}); the 2048 cap was not exercised")


# --------------------------------------------------------------------------- #
#  F02 control: enable_free_model_pre_consume=true -> 403 (disruptive)        #
# --------------------------------------------------------------------------- #
@pytest.mark.disruptive
@pytest.mark.contract_id("F02")
def test_f02_pre_consume_control(cfg, admin, relay_base, fc, settings_admin):
    fc.set_wallet(admin, 0)
    with settings_admin.temporary("quota_setting.enable_free_model_pre_consume", True):
        time.sleep(2)
        r = _free(fc, relay_base, cfg)
        assert r.status == 403, f"pre-consume on, balance 0: expected 403, got {r.error_summary()}"
    time.sleep(2)
    fc.wait_full()
    assert _free(fc, relay_base, cfg).ok, "the free relay did not come back after the restore"


# --------------------------------------------------------------------------- #
#  F03 25 in a row: exactly the 10-burst passes, then 429; +6 s one more      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F03")
def test_f03_per_minute_cap(cfg, relay_base, fc, contract_metric):
    t0 = time.time()
    results = []
    for _ in range(25):
        results.append(_free(fc, relay_base, cfg).status)
    elapsed = time.time() - t0
    passed = results.count(200)
    others = [s for s in results if s not in (200, 429)]
    contract_metric("F03", {"passed": passed, "elapsed_s": round(elapsed, 1)})
    assert not others, f"non-429 failures {others}"
    assert results[:10] == [200] * 10, f"the first 10 did not all pass: {results[:10]}"
    # token bucket: 10 at once, then 1 per 6 s (count=10, D=1 min)
    assert passed <= 10 + int(elapsed // 6), f"{passed} passed in {elapsed:.1f}s: the burst is not 10"
    assert 429 in results, "never hit 429"


@pytest.mark.slow
@pytest.mark.contract_id("F03")
def test_f03_refill_after_6s(cfg, relay_base, fc, slow):
    for _ in range(12):
        _free(fc, relay_base, cfg)
    assert _free(fc, relay_base, cfg).status == 429, "bucket not empty after 12"
    time.sleep(7)
    assert _free(fc, relay_base, cfg).ok, "no refill after 7s"


# --------------------------------------------------------------------------- #
#  F04 50 concurrent: at most 10 let through (atomic), the rest 429           #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F04")
def test_f04_concurrent_cap(cfg, relay_base, fc, contract_metric):
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        statuses = list(ex.map(lambda _: relay.chat(relay_base, fc.trial_key, cfg.free_model, max_tokens=1).status,
                               range(50)))
    fc.touched()
    elapsed = time.time() - t0
    ok = statuses.count(200)
    contract_metric("F04", {"passed": ok, "elapsed_s": round(elapsed, 1)})
    assert ok >= 1, f"nothing passed (is the free upstream up?): {sorted(set(statuses))}"
    assert ok <= 10 + int(elapsed // 6), f"{ok} of 50 passed: the total bucket is not atomic"
    assert all(s in (200, 429) for s in statuses), f"non-429 failures: {sorted(set(statuses) - {200, 429})}"


# --------------------------------------------------------------------------- #
#  F05 bypass stays closed; the counter-example reopens it                    #
# --------------------------------------------------------------------------- #
def _bypass_minute(cfg, relay_base, fc, seconds=60):
    """Free requests as fast as they come back, a paid junk-model request
    every second, for ``seconds``; returns free passes."""
    stop = time.time() + seconds
    passes = []

    def junk():
        while time.time() < stop:
            relay.chat(relay_base, fc.paid_key, "no-such-model-contract", max_tokens=1)
            time.sleep(1)

    t = threading.Thread(target=junk, daemon=True)
    t.start()
    while time.time() < stop:
        if _free(fc, relay_base, cfg).ok:
            passes.append(time.time())
        time.sleep(0.2)  # a 429 comes back at once: do not hammer
    t.join(timeout=10)
    return len(passes)


@pytest.mark.slow
@pytest.mark.contract_id("F05")
def test_f05_no_bypass_with_safe_config(cfg, relay_base, fc, slow, contract_metric):
    passed = _bypass_minute(cfg, relay_base, fc)
    contract_metric("F05", {"safe_config_free_per_minute": passed})
    # 10 burst + one per 6 s: at most 19-20 in any 60 s (〔FREE〕§2.5)
    assert passed <= 20, f"free passed {passed} in 60s with paid junk interleaved: the bypass is open"


@pytest.mark.disruptive
@pytest.mark.slow
@pytest.mark.contract_id("F05")
def test_f05_counterexample_reopens_bypass(cfg, relay_base, fc, settings_admin, slow, contract_metric):
    with settings_admin.temporary_json_member("ModelRequestRateLimitGroup", "default", [600, 600]):
        time.sleep(2)
        passed = _bypass_minute(cfg, relay_base, fc)
    contract_metric("F05", {"counterexample_free_per_minute": passed})
    assert passed > 25, f"default:[600,600] did not reopen the bypass (free passed {passed} in 60s)"


# --------------------------------------------------------------------------- #
#  F06 daily-cap primitives (the meter itself: LLM-10)                        #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F06")
def test_f06_daily_cap_primitives(cfg, admin, relay_base, fc, contract_partial):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset (a paid log proves the group filter)")
    contract_partial(KNOWN_PARTIAL["F06"])
    fc.set_wallet(admin, 20 * POINT)
    assert _free(fc, relay_base, cfg).ok, "free relay"
    paid = relay.chat(relay_base, fc.paid_key, cfg.paid_model, max_tokens=1)
    assert paid.ok, f"paid relay: {paid.error_summary()}"
    time.sleep(3)
    q = f"/api/log/?type=2&username={fc.user.username}&p=1&page_size=100"
    free_items = (admin.get(q + "&group=free").data or {}).get("items") or []
    all_items = (admin.get(q).data or {}).get("items") or []
    assert free_items and all(it.get("group") == "free" for it in free_items), "group=free returned other groups"
    assert any(it.get("group") == "default" for it in all_items), "the paid log is missing"
    assert len(free_items) < len(all_items), "group=free did not filter (same count as unfiltered)"
    # the meter's hand: status_only disable -> relay 401; enable -> 200
    upc = fc.upc
    assert upc.put("/api/token/?status_only=1", {"id": fc.trial_id, "status": 2}).success is True
    try:
        time.sleep(1)
        r = _free(fc, relay_base, cfg)
        assert r.status == 401, f"disabled trial key: {r.error_summary()}"
    finally:
        assert upc.put("/api/token/?status_only=1", {"id": fc.trial_id, "status": 1}).success is True
    time.sleep(1)
    fc.wait_full()
    assert _free(fc, relay_base, cfg).ok, "the re-enabled trial key does not work"


# --------------------------------------------------------------------------- #
#  F07 paid: the 121st success per minute starts 429                          #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.contract_id("F07")
def test_f07_paid_success_cap(cfg, admin, relay_base, fc, slow, contract_metric):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset")
    fc.set_wallet(admin, 60 * POINT)  # ~130 one-token calls of the cheapest model
    time.sleep(61)  # the success list (shared with free successes) must be empty
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        statuses = list(ex.map(lambda _: relay.chat(relay_base, fc.paid_key, cfg.paid_model, max_tokens=1).status,
                               range(136)))
    elapsed = time.time() - t0
    ok, limited = statuses.count(200), statuses.count(429)
    other = sorted(set(statuses) - {200, 429})
    contract_metric("F07", {"ok": ok, "429": limited, "other": other, "elapsed_s": round(elapsed, 1)})
    assert elapsed < 60, f"136 calls took {elapsed:.0f}s: the window was not exercised"
    assert not other, f"non-429 failures {other} (wallet / upstream?)"
    # the success list is not a hard cap under concurrency: up to 12 in flight
    assert 120 <= ok <= 132 and limited >= 1, f"success cap not at 120 (ok={ok}, 429={limited})"


# --------------------------------------------------------------------------- #
#  F08 trial -> paid model 403; paid -> Qwen 503                              #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F08")
def test_f08_cross_group_denied(cfg, relay_base, fc):
    if not cfg.have_paid_model:
        pytest.skip("NEWAPI_CONTRACT_PAID_MODEL unset")
    r = relay.chat(relay_base, fc.trial_key, cfg.paid_model, max_tokens=1)
    assert r.status == 403, f"trial key -> paid model: {r.error_summary()}"
    r = relay.chat(relay_base, fc.paid_key, cfg.free_model, max_tokens=1)
    assert r.status == 503, f"paid key -> Qwen: {r.error_summary()}"


# --------------------------------------------------------------------------- #
#  F09 fuse: GroupRatio.free=1 -> 403, wallet unchanged (disruptive)          #
# --------------------------------------------------------------------------- #
@pytest.mark.disruptive
@pytest.mark.contract_id("F09")
def test_f09_fuse(cfg, relay_base, fc, settings_admin):
    before = fc.quota()
    with settings_admin.temporary_json_member("GroupRatio", "free", 1):
        time.sleep(2)
        r = _free(fc, relay_base, cfg)
        assert r.status == 403, f"fuse did not blow: {r.error_summary()}"
    assert fc.quota() == before, "the wallet moved (the trial key's remain_quota 1 must block first)"


# --------------------------------------------------------------------------- #
#  F10 TrustQuotaUSD=0 overdraft measurement (LLM-12b / NA-08)                #
# --------------------------------------------------------------------------- #
def _out_price_per_token(item):
    """USD per output token at LiteLLM cost (ratio 1 = US$2 per 1M tokens)."""
    if item.get("quota_type") == 1:
        return None
    return float(item.get("model_ratio") or 0) * float(item.get("completion_ratio") or 1) * 2e-6


@pytest.mark.disruptive
@pytest.mark.slow
@pytest.mark.contract_id("F10")
def test_f10_overdraft_measurement(cfg, lab, relay_base, slow, disruptive, contract_metric):
    """200+ concurrent requests to the model with the highest output price
    from a user with a 10-point wallet; how far below 0 does it end up
    (TrustQuotaUSD=0 pre-consumes input only, 〔FREE〕§5.2)? Shared LiteLLM
    svc203 load and real spend: disruptive window, own opt-in, spend cap."""
    from .users import create_test_user

    if not (cfg.f10_confirm and cfg.f10_model):
        pytest.skip("F10 spends real upstream money: NEWAPI_CONTRACT_F10_CONFIRM=1 + NEWAPI_CONTRACT_F10_MODEL")
    user = create_test_user(lab)
    try:
        user.login()
        up, _ = user.create_up()
        pricing = user.up_client(up).get("/api/pricing")
        items = [it for it in (pricing.data or []) if "default" in (it.get("enable_groups") or [])]
        priced = {it["model_name"]: _out_price_per_token(it) for it in items if _out_price_per_token(it)}
        best = max(priced.values()) if priced else None
        tops = sorted(m for m, p in priced.items() if p >= best * (1 - 1e-9))  # a tie: any of them
        assert cfg.f10_model in tops, f"F10 must use the highest output price in group default: one of {tops}"
        top = cfg.f10_model
        n, max_tokens = cfg.f10_concurrency, cfg.f10_max_tokens
        worst_usd = n * max_tokens * priced[top]
        assert worst_usd <= cfg.f10_max_usd, (
            f"worst-case upstream spend US${worst_usd:.2f} > cap US${cfg.f10_max_usd:.2f}: lower "
            "NEWAPI_CONTRACT_F10_MAX_TOKENS / _CONCURRENCY or raise NEWAPI_CONTRACT_F10_MAX_USD")
        tid = user.make_paid_key(up)
        key = user.reveal(up, tid)
        seed = 10 * POINT
        r = lab.admin.post("/api/user/manage", {"id": user.id, "action": "add_quota", "mode": "add", "value": seed})
        assert r.success is True, "seed balance"
        prompt = [{"role": "user", "content": "Write a long essay about the history of tea. Do not stop early."}]

        def call(_):
            try:
                return relay.chat(relay_base, key, top, max_tokens=max_tokens, messages=prompt, timeout=180).status
            except relay.RelayTransportError:
                return -1

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            statuses = list(ex.map(call, range(n)))
        elapsed = time.time() - t0
        time.sleep(5)
        after = lab.admin.get(f"/api/user/{user.id}").data or {}
        quota = after.get("quota")
        assert isinstance(quota, int), "no quota after the burst"
        counts = {"ok": statuses.count(200), "403": statuses.count(403), "429": statuses.count(429),
                  "5xx": sum(1 for s in statuses if s >= 500), "transport": statuses.count(-1)}
        counts["other"] = n - sum(counts.values())
        metrics = {
            "model": top, "concurrency": n, "max_tokens": max_tokens, "elapsed_s": round(elapsed, 1),
            "seed_points": seed // POINT, "final_quota": quota, "overdraft_quota": min(0, quota),
            "overdraft_points": round(min(0, quota) / POINT, 2), "statuses": counts,
            "upstream_error_rate": round((counts["5xx"] + counts["transport"]) / n, 3),
            "denied_rate": round((counts["403"] + counts["429"]) / n, 3),
            "worst_case_usd": round(worst_usd, 4), "used_quota": after.get("used_quota"),
        }
        contract_metric("F10", metrics)
        assert counts["ok"] >= 1, f"no request succeeded: {counts}"
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  F11 Valkey restart: counters reset; 500s during the restart (disruptive)   #
# --------------------------------------------------------------------------- #
@pytest.mark.disruptive
@pytest.mark.contract_id("F11")
def test_f11_valkey_restart(cfg, kube, relay_base, fc, disruptive, contract_metric, fixture_sink):
    if kube is None:
        pytest.skip("needs NEWAPI_CONTRACT_KUBECTL")
    for _ in range(12):
        _free(fc, relay_base, cfg)
    assert _free(fc, relay_base, cfg).status == 429, "bucket not empty"
    seen, stop, captured = [], threading.Event(), []

    def probe():
        while not stop.is_set():
            try:
                r = relay.chat(relay_base, fc.trial_key, cfg.free_model, max_tokens=1, timeout=20)
                seen.append(r.status)
                if r.status == 500 and not captured:
                    captured.append(1)
                    fixture_sink.capture("relay_500_valkey_down", r, route="relay")
            except relay.RelayTransportError:
                seen.append(-1)
            time.sleep(0.5)

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t0 = time.time()
    try:
        kube.rollout_restart("deploy/newapi-valkey")
        kube.rollout_status("deploy/newapi-valkey", timeout=240)
    finally:
        time.sleep(8)
        stop.set()
        t.join(timeout=30)
    contract_metric("F11", {"restart_s": round(time.time() - t0, 1), "probe_requests": len(seen),
                            "probe_500": seen.count(500), "probe_transport": seen.count(-1),
                            "probe_429": seen.count(429), "probe_200": seen.count(200)})
    fc.wait_full()
    assert _free(fc, relay_base, cfg).ok, "the free relay does not work after the Valkey restart"


# --------------------------------------------------------------------------- #
#  F12 Redis key format (rc.44: shared rateLimit:<uid>; per-group after 45fbb149f)
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("F12")
def test_f12_redis_key_format(cfg, kube, relay_base, fc):
    if kube is None:
        pytest.skip("needs NEWAPI_CONTRACT_KUBECTL (valkey-cli)")
    assert _free(fc, relay_base, cfg).ok, "free relay"
    time.sleep(1)
    uid = fc.user.id
    shared, per_group, success = (f"rateLimit:{uid}", f"rateLimit:{uid}:free", f"rateLimit:MRRLS:{uid}")
    kinds = {k: kube.key_type(k) for k in (shared, per_group, success)}
    assert kinds[success] == "list", f"success list {success}: {kinds[success]}"
    if cfg.expect_pergroup:
        assert kinds[per_group] == "hash", f"post-upgrade per-group bucket {per_group}: {kinds[per_group]}"
    else:
        assert kinds[shared] == "hash", f"rc.44 shared total bucket {shared}: {kinds[shared]}"
        assert kinds[per_group] == "none", f"rc.44 must not have {per_group} (got {kinds[per_group]})"
