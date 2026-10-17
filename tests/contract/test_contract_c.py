"""C01-C27 — the new-api API surface the PaaS depends on (〔PROV〕§6.3).

Each test carries its contract id so the end-of-run report lists pass/fail per
id. Reads use the session-scoped ``consumer`` (one login, one UP) to stay well
inside the shared 127.0.0.1 CT budget of a port-forward; mutating tests make
their own throw-away user. Admin writes use the run's admin PAT, never the
proxy's AP.
"""
from __future__ import annotations

import re
import time

import pytest

from .users import create_test_user

pytestmark = pytest.mark.usefixtures("admin")  # whole file needs new-api


# --------------------------------------------------------------------------- #
#  C01 login envelope                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C01")
def test_c01_login_envelope(consumer):
    user = consumer.user
    r = user.login()
    data = r.data or {}
    assert r.success is True
    assert isinstance(data.get("access_token"), str) and data["access_token"]
    assert (data.get("session") or {}).get("sid")
    assert (data.get("user") or {}).get("id") == user.id
    assert "require_verification" not in data  # no 2FA on the consumer account


# --------------------------------------------------------------------------- #
#  C02 verify request/response + 60s proof expiry                             #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C02")
def test_c02_verify_shape_and_window(consumer):
    now = int(time.time())
    r = consumer.user.proof("access_token.generate", {"scopes": ["usage:read"], "expires_at": 0})
    data = r.data or {}
    assert r.success is True
    assert isinstance(data.get("proof_token"), str) and data["proof_token"]
    assert data.get("method") == "password"
    assert data.get("scope") == "access_token.generate"
    assert 50 <= int(data["expires_at"]) - now <= 70  # ~60s one-time proof


@pytest.mark.contract_id("C02")
def test_c02_proof_expires_after_60s(consumer, slow):
    r = consumer.user.proof("access_token.generate", {"scopes": ["usage:read"], "expires_at": 0})
    proof = (r.data or {}).get("proof_token")
    assert proof
    time.sleep(63)
    used = consumer.jwt.post(
        "/api/user/access_tokens",
        {"name": "woow-paas-up-expired-000000000000", "scopes": ["usage:read"], "expires_at": 0},
        headers={"X-Security-Proof": proof},
    )
    assert used.code == "SECURITY_PROOF_EXPIRED"


# --------------------------------------------------------------------------- #
#  C03 scope strings + context schema                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C03")
def test_c03_generate_context_must_be_exact(consumer):
    # An extra field in the generate context -> SECURITY_CONTEXT_INVALID (400).
    r = consumer.jwt.post(
        "/api/verify",
        {
            "method": "password",
            "scope": "access_token.generate",
            "context": {"scopes": ["usage:read"], "expires_at": 0, "extra": 1},
            "password": consumer.user.password,
        },
    )
    assert r.status == 400
    assert r.code == "SECURITY_CONTEXT_INVALID"


@pytest.mark.contract_id("C03")
def test_c03_revoke_context_shape(consumer):
    # revoke context is exactly {token_id}; a wrong field is rejected.
    r = consumer.jwt.post(
        "/api/verify",
        {
            "method": "password",
            "scope": "access_token.revoke",
            "context": {"not_token_id": 1},
            "password": consumer.user.password,
        },
    )
    assert r.code in ("SECURITY_CONTEXT_INVALID", "SECURITY_PROOF_SCOPE_MISMATCH")
    assert r.status in (400, 403, 200)


# --------------------------------------------------------------------------- #
#  C04 generate method is password when no 2FA                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C04")
def test_c04_generate_method_is_password(consumer):
    r = consumer.jwt.get("/api/verify/methods?scope=access_token.generate")
    methods = (r.data or {}).get("methods") or []
    password = next((m for m in methods if m.get("method") == "password"), None)
    assert password is not None and password.get("available") is True


# --------------------------------------------------------------------------- #
#  C05 create access token shape                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C05")
def test_c05_create_access_token(consumer):
    token, item = consumer.user.create_up(scopes=["usage:read"], name="woow-paas-up-c05-000000000001")
    assert token.startswith("nap_") and len(token) == 47
    assert isinstance(item.get("id"), int)
    assert item.get("token_ref")
    # the fresh UP actually authenticates
    whoami = consumer.user.up_client(token).get("/api/user/self")
    assert whoami.success is True


# --------------------------------------------------------------------------- #
#  C06 PAT management is session-only                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C06")
def test_c06_up_cannot_list_pats(consumer):
    r = consumer.up.get("/api/user/access_tokens")
    assert r.status == 403
    assert r.code == "AUTH_SESSION_REQUIRED"


# --------------------------------------------------------------------------- #
#  C07 scope names + missing scope denial                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C07")
def test_c07_catalog_scope_names(consumer):
    r = consumer.jwt.get("/api/user/access_tokens/catalog")
    data = r.data or {}
    resources = {}
    for grp in data.get("groups") or []:
        for res in grp.get("resources") or []:
            actions = {a.get("action") for a in res.get("actions") or []}
            resources[res.get("resource")] = actions
    assert {"read", "write"} <= resources.get("user", set())
    assert "read" in resources.get("log", set())
    assert {"read", "write", "reveal"} <= resources.get("api_key", set())


@pytest.mark.contract_id("C07")
def test_c07_missing_scope_denied(consumer):
    # A UP with only profile:read may not call GET /api/token/ (api_key:read).
    token, _ = consumer.user.create_up(scopes=["profile:read"], name="woow-paas-up-c07-000000000002")
    r = consumer.user.up_client(token).get("/api/token/?p=1&page_size=10")
    assert r.status == 403
    assert r.code == "ACCESS_TOKEN_SCOPE_DENIED"


# --------------------------------------------------------------------------- #
#  C08 delete session revokes the JWT                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C08")
def test_c08_delete_session(cfg, admin, base_client):
    user = create_test_user(admin, base_client, cfg.user_prefix)
    try:
        user.login()
        jwt = user.jwt_client()
        r = jwt.delete(f"/api/user/sessions/{user.sid}")
        assert r.success is True
        assert (r.data or {}).get("revoked_sid")
        after = jwt.get("/api/user/self")
        assert after.status == 401
        user.sid = None  # already revoked
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  C09 create user: no proof, no id, ignores group/remark                     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C09")
def test_c09_create_user(cfg, admin, base_client):
    import secrets

    from .users import TestUser

    name = cfg.user_prefix + f"{secrets.randbelow(10**10):010d}"
    password = "Pa" + "9" * 44
    r = admin.post(
        "/api/user/",
        {"username": name, "password": password, "display_name": "PaaS 0",
         "role": 1, "group": "vip", "remark": "should-be-ignored"},
    )
    assert r.success is True
    assert "id" not in (r.data or {})  # response carries no id
    search = admin.get(f"/api/user/search?keyword={name}&p=1&page_size=10")
    item = next((u for u in (search.data or {}).get("items") or [] if u.get("username") == name), None)
    assert item is not None
    assert item.get("group") == "default"  # the sent group was ignored
    uid = item["id"]
    user = TestUser(admin, base_client, name, password, uid)
    try:
        full = admin.get(f"/api/user/{uid}")
        assert (full.data or {}).get("remark") in (None, "")  # remark not set on create
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  C10 search is Unscoped LIKE, id desc; equal-width prefix => exact match     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C10")
def test_c10_search_like_and_order(cfg, admin, base_client):
    import secrets

    # Two equal-width names sharing a 6-digit stem in their 10-digit tail.
    stem = f"{secrets.randbelow(10**6):06d}"
    names = [cfg.user_prefix + stem + "01", cfg.user_prefix + stem + "02"]
    users = [create_test_user(admin, base_client, cfg.user_prefix, username=n) for n in names]
    try:
        sub = admin.get(f"/api/user/search?keyword={cfg.user_prefix + stem}&p=1&page_size=10")
        items = (sub.data or {}).get("items") or []
        matched = [u for u in items if u.get("username") in names]
        assert len(matched) == 2
        ids = [u["id"] for u in items if u.get("username") in names]
        assert ids == sorted(ids, reverse=True)  # default id desc
        for u in matched:
            for field in ("id", "username", "status", "group", "role"):
                assert field in u
            assert "DeletedAt" in u or "deleted_at" in u
        # exact full name returns exactly that one (substring == equality)
        exact = admin.get(f"/api/user/search?keyword={names[0]}&p=1&page_size=10")
        exact_items = [u for u in (exact.data or {}).get("items") or [] if u.get("username") in names]
        assert [u["username"] for u in exact_items] == [names[0]]
    finally:
        for u in users:
            u.cleanup()


# --------------------------------------------------------------------------- #
#  C11 PUT /api/user/ overwrites four fields; unchanged group keeps session    #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C11")
def test_c11_put_user_keeps_session(cfg, admin, base_client):
    user = create_test_user(admin, base_client, cfg.user_prefix)
    try:
        user.login()
        jwt = user.jwt_client()
        current = admin.get(f"/api/user/{user.id}")
        assert current.success
        r = admin.put("/api/user/", {"id": user.id, "username": user.username,
                                     "display_name": "PaaS 0", "group": "default",
                                     "remark": "paas_llm_account=0"})
        assert r.success is True
        # group unchanged -> auth_version not bumped -> the old JWT still works
        after = jwt.get("/api/user/self")
        assert after.status == 200 and after.success is True
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  C12 add_quota request / log                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C12")
def test_c12_add_quota_and_log(cfg, admin, consumer):
    quota = 15625  # 1 point
    r = admin.post("/api/user/manage", {"id": consumer.user.id, "action": "add_quota",
                                        "mode": "add", "value": quota})
    assert r.success is True
    request_id = r.request_id
    assert request_id
    # find the type=1 log the quota write produced
    logs = admin.get(f"/api/log/?type=1&username={consumer.user.username}&p=1&page_size=100")
    items = (logs.data or {}).get("items") or []
    mine = next((lg for lg in items if (lg.get("other") or {}).get("op", {}).get("action") == "user.quota_add"
                 and lg.get("request_id") == request_id), None)
    assert mine is not None, "add_quota type=1 log not found"
    op = (mine["other"]["op"]) if isinstance(mine.get("other"), dict) else {}
    params = op.get("params") or {}
    assert params.get("quota") == quota
    assert "from" in params and "to" in params
    admin_info = (mine["other"] or {}).get("admin_info") or {}
    assert admin_info.get("admin_id")


# --------------------------------------------------------------------------- #
#  C13 add_quota error message_key                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C13")
def test_c13_add_quota_zero(admin, consumer):
    r = admin.post("/api/user/manage", {"id": consumer.user.id, "action": "add_quota",
                                        "mode": "add", "value": 0})
    assert r.success is False
    assert r.message_key == "Quota change amount cannot be zero"


# --------------------------------------------------------------------------- #
#  C14 create token: no id/key; field names                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C14")
def test_c14_create_token_no_id_key(consumer):
    up = consumer.up
    name = f"wct_{consumer.user.id}_c14a"
    created = up.post("/api/token/", {
        "name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
        "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
        "cross_group_retry": False, "allow_user_quota_query": False,
    })
    assert created.success is True
    assert "id" not in (created.data or {}) and "key" not in (created.data or {})
    item = next((t for t in (up.get("/api/token/?p=1&page_size=100").data or {}).get("items") or []
                 if t.get("name") == name), None)
    assert item is not None
    for field in ("expired_time", "remain_quota", "unlimited_quota", "model_limits_enabled",
                  "model_limits", "allow_ips", "group", "cross_group_retry"):
        assert field in item
    consumer.user.note_relay_token(item["id"])


# --------------------------------------------------------------------------- #
#  C15 list tokens: id desc; masked key                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C15")
def test_c15_list_tokens_order_and_mask(consumer):
    for n in ("c15a", "c15b"):
        consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_{n}")
    items = (consumer.up.get("/api/token/?p=1&page_size=100").data or {}).get("items") or []
    ids = [t["id"] for t in items]
    assert ids == sorted(ids, reverse=True)
    for t in items:
        assert re.fullmatch(r".{4}\*{10}.{4}", t.get("key") or ""), t.get("key")


# --------------------------------------------------------------------------- #
#  C16 reveal: 48-char key, no sk-; costs a CT                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C16")
def test_c16_reveal(consumer):
    tid, _, _ = consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_c16")
    r = consumer.up.post(f"/api/token/{tid}/key")
    assert r.success is True
    key = (r.data or {}).get("key")
    assert isinstance(key, str) and len(key) == 48
    assert not key.startswith("sk-")


# --------------------------------------------------------------------------- #
#  C17 whole PUT overwrites (drops allow_ips => clears); status_only           #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C17")
def test_c17_put_token_whole_overwrite(consumer):
    tid, _, item = consumer.user.create_relay_token(
        consumer.up_token, f"wct_{consumer.user.id}_c17", allow_ips="203.0.113.7")
    up = consumer.up
    got = (up.get(f"/api/token/{tid}").data or {})
    assert got.get("allow_ips") == "203.0.113.7"
    # whole PUT without allow_ips clears it
    body = {k: got.get(k) for k in ("id", "name", "expired_time", "remain_quota", "unlimited_quota",
                                    "model_limits_enabled", "model_limits", "group", "cross_group_retry")}
    r = up.put("/api/token/", body)
    assert r.success is True
    after = (up.get(f"/api/token/{tid}").data or {})
    assert (after.get("allow_ips") or "") == ""
    # status_only disable
    s = up.put("/api/token/?status_only=1", {"id": tid, "status": 2})
    assert s.success is True
    assert (up.get(f"/api/token/{tid}").data or {}).get("status") == 2


# --------------------------------------------------------------------------- #
#  C18 Redis: expired key status not written back                            #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C18")
def test_c18_expired_status_not_written_back(consumer):
    tid, _, item = consumer.user.create_relay_token(
        consumer.up_token, f"wct_{consumer.user.id}_c18",
        unlimited_quota=True, remain_quota=0, expired_time=-1)
    up = consumer.up
    got = (up.get(f"/api/token/{tid}").data or {})
    body = {k: got.get(k) for k in ("id", "name", "remain_quota", "unlimited_quota",
                                    "model_limits_enabled", "model_limits", "allow_ips",
                                    "group", "cross_group_retry")}
    body["expired_time"] = int(time.time()) - 3600  # already expired
    r = up.put("/api/token/", body)
    assert r.success is True
    after = (up.get(f"/api/token/{tid}").data or {})
    assert after.get("status") == 1  # Redis mode: 3/4 is not persisted


# --------------------------------------------------------------------------- #
#  C19 log/self shape (id display seq, request_id, channel, no admin_info)     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C19")
def test_c19_log_self_shape(admin, consumer):
    # produce a type=1 log on the consumer via add_quota, then read it as self
    admin.post("/api/user/manage", {"id": consumer.user.id, "action": "add_quota",
                                    "mode": "add", "value": 15625})
    logs = consumer.up.get("/api/log/self?type=1&p=1&page_size=10")
    items = (logs.data or {}).get("items") or []
    assert items, "no self log to inspect"
    item = items[0]
    assert isinstance(item.get("id"), int)  # display sequence, an int
    assert "request_id" in item
    assert "channel" in item  # the field is present (PaaS DTO strips it)
    other = item.get("other")
    if isinstance(other, dict):
        assert "admin_info" not in other  # self view strips admin_info


# --------------------------------------------------------------------------- #
#  C20 log stat + data/self + flow/self                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C20")
def test_c20_stat_and_data(consumer):
    up = consumer.up
    stat = up.get("/api/log/self/stat?type=2")
    assert stat.success is True
    for field in ("quota", "rpm", "tpm"):
        assert field in (stat.data or {})
    now = int(time.time())
    daily = up.get(f"/api/data/self?start_timestamp={now - 86400}&end_timestamp={now}")
    assert daily.success is True
    assert isinstance(daily.data, list)
    # flow/self requires both timestamps > 0
    bad = up.get("/api/data/flow/self")
    assert bad.success is False


# --------------------------------------------------------------------------- #
#  C21 user/self fields                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C21")
def test_c21_user_self_fields(consumer):
    r = consumer.up.get("/api/user/self")
    data = r.data or {}
    for field in ("id", "group", "quota", "used_quota", "request_count", "status"):
        assert field in data


# --------------------------------------------------------------------------- #
#  C22 /api/status invariants                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C22")
def test_c22_status_invariants(anon):
    r = anon.get("/api/status")
    data = r.data or {}
    assert data.get("quota_per_unit") == 500000
    assert data.get("password_login_enabled") is True
    assert data.get("turnstile_check") is False
    assert data.get("password_login_encryption_enabled") is False
    assert data.get("enable_batch_update") is False
    assert data.get("enable_data_export") is True
    assert data.get("register_enabled") is False
    assert data.get("self_use_mode_enabled") is False


# --------------------------------------------------------------------------- #
#  C23 rate limit: 429 empty body + Retry-After (CT); slow, runs last         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C23")
def test_c23_critical_rate_limit(cfg, base_client, slow):
    # 21 bad logins trip the per-IP CT bucket (20/20min). The 429 has an empty
    # body and a Retry-After header; a session-issuance 429 would carry a JSON
    # code, so assert the body is empty. This pollutes 127.0.0.1's CT bucket
    # for 20 minutes, so it must be the last thing the suite does.
    saw_429 = None
    for _ in range(25):
        r = base_client.post("/api/user/login", {"username": "nouser000000", "password": "x" * 12})
        if r.status == 429:
            saw_429 = r
            break
    assert saw_429 is not None, "CT limit never tripped in 25 attempts"
    assert saw_429.raw_empty, "limiter 429 must have an empty body"
    assert saw_429.retry_after


# --------------------------------------------------------------------------- #
#  C24 XFF -> synthetic IP lands in last_used_ip (needs proxy + psql)          #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C24")
def test_c24_synthetic_ip_in_last_used_ip(cfg, proxy_cron, psql):
    # An AP request through the proxy sets its XFF to 198.18.0.1; new-api
    # records that as the AP PAT's last_used_ip (written at most every 60s).
    r = proxy_cron.send("GET", "/api/user/1", identity="ap")
    assert r.status in (200, 403, 404)  # reached new-api (ap can read users)
    time.sleep(2)
    ip = psql(
        "SELECT last_used_ip FROM user_access_tokens WHERE user_id = "
        f"(SELECT id FROM users WHERE username = '{cfg.svc_username}') "
        "ORDER BY last_used_at DESC LIMIT 1"
    )
    assert ip == "198.18.0.1", f"AP last_used_ip = {ip!r}"


# --------------------------------------------------------------------------- #
#  C25 stable error codes list                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C25")
def test_c25_error_codes(consumer):
    # AUTH_SESSION_REQUIRED (UP on a session-only route)
    assert consumer.up.get("/api/user/access_tokens").code == "AUTH_SESSION_REQUIRED"
    # ACCESS_TOKEN_SCOPE_DENIED (UP missing scope)
    token, _ = consumer.user.create_up(scopes=["profile:read"], name="woow-paas-up-c25-000000000003")
    assert consumer.user.up_client(token).get("/api/token/?p=1&page_size=10").code == "ACCESS_TOKEN_SCOPE_DENIED"
    # AUTH_UNAUTHORIZED (a bad PAT)
    bad = consumer.user.up_client("nap_" + "0" * 43).get("/api/user/self")
    assert bad.status == 401 and bad.code in ("AUTH_UNAUTHORIZED", "AUTH_TOKEN_EXPIRED")


# --------------------------------------------------------------------------- #
#  C26 length limits                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C26")
def test_c26_length_limits(consumer):
    up = consumer.up
    # page_size is capped at 100: page_size=500 must not return > 100 rows
    r = up.get("/api/token/?p=1&page_size=500")
    assert r.success is True
    assert (r.data or {}).get("page_size", 0) <= 100
    # PAT name 1..64: a 65-char name is rejected
    long_name = "woow-paas-up-" + "9" * 60
    pr = consumer.user.proof("access_token.generate", {"scopes": ["usage:read"], "expires_at": 0})
    pt = (pr.data or {}).get("proof_token")
    bad = consumer.jwt.post("/api/user/access_tokens",
                            {"name": long_name, "scopes": ["usage:read"], "expires_at": 0},
                            headers={"X-Security-Proof": pt or ""})
    assert bad.code == "ACCESS_TOKEN_NAME_INVALID" or bad.status == 400


@pytest.mark.contract_id("C26")
def test_c26_token_name_byte_limit(consumer):
    # relay token name <= 50 bytes
    r = consumer.up.post("/api/token/", {
        "name": "x" * 51, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
        "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
        "cross_group_retry": False, "allow_user_quota_query": False})
    assert r.success is False


# --------------------------------------------------------------------------- #
#  C27 password/group change keeps PATs; disable 401s; enable restores         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C27")
def test_c27_pat_survives_but_disable_blocks(cfg, admin, base_client):
    user = create_test_user(admin, base_client, cfg.user_prefix)
    try:
        user.login()
        token, _ = user.create_up(scopes=["profile:read"], name="woow-paas-up-c27-000000000004")
        up = user.up_client(token)
        assert up.get("/api/user/self").success is True
        # disable the user -> PAT 401
        if cfg.have_admin_creds:
            _admin_disable(cfg, admin, base_client, user.id, "disable")
            assert up.get("/api/user/self").status == 401
            _admin_disable(cfg, admin, base_client, user.id, "enable")
            assert up.get("/api/user/self").success is True
        else:
            pytest.skip("disable needs admin step-up proof (NEWAPI_CONTRACT_ADMIN_CREDS_FILE)")
    finally:
        user.cleanup()


def _admin_disable(cfg, admin, base_client, user_id, action):
    """enable needs no proof; disable needs admin.user.manage proof (root creds)."""
    from . import newapi

    if action == "enable":
        r = admin.post("/api/user/manage", {"id": user_id, "action": "enable"})
        assert r.success, r.message
        return
    creds = _read_creds(cfg.admin_creds_file)
    root = newapi.Client(cfg.base)
    login = root.post("/api/user/login", {"username": creds["username"], "password": creds["password"]})
    jwt = (login.data or {}).get("access_token")
    sid = ((login.data or {}).get("session") or {}).get("sid")
    rc = root.with_token(jwt)
    try:
        pr = rc.post("/api/verify", {"method": "password", "scope": "admin.user.manage",
                                     "context": {"user_id": user_id, "action": "disable"},
                                     "password": creds["password"]})
        pt = (pr.data or {}).get("proof_token")
        assert pt, pr.code
        r = rc.post("/api/user/manage", {"id": user_id, "action": "disable"},
                    headers={"X-Security-Proof": pt})
        assert r.success, r.message
    finally:
        if sid:
            rc.delete(f"/api/user/sessions/{sid}")


def _read_creds(path):
    import os

    out = {}
    with open(os.path.expanduser(path)) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    return out
