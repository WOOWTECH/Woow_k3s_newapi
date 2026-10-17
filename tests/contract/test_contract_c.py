"""C01-C27 — the new-api API surface the PaaS depends on (〔PROV〕§6.3).

Reads share the session ``consumer`` (one login, one full UP); tests that
need a fresh account make one with ``create_test_user`` and delete it in a
finally block. Admin writes use the run's admin PAT (root, user:read/write +
log:read), never the proxy's AP. Every CT-spending call is paced by the run's
CtBudget (conftest.py), so a full run spans two or three 20-minute windows.

C23's 429 run (it empties the 127.0.0.1 CT bucket for 20 minutes) is a
separate invocation: ``NEWAPI_CONTRACT_CT_EXHAUST=1 pytest -m ct_exhaust``,
after the main run (README).
"""
from __future__ import annotations

import re
import secrets
import time

import pytest

from . import budget as budgetmod
from . import relay
from .redact import assert_secret_shape
from .users import TestUser, create_test_user, new_password

pytestmark = pytest.mark.usefixtures("lab")

#: Longest wait at the end of the run for the 1-hour UP / 15-minute JWT to expire.
MAX_EXPIRY_WAIT = 40 * 60
JWT_LEEWAY_S = 5  # new-api rc.44 service/auth_token.go:194 (jwt.WithLeeway)


# --------------------------------------------------------------------------- #
#  C01 login envelope                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C01")
def test_c01_login_envelope(consumer, fixture_sink):
    r = consumer.user.login_response  # the consumer's own login (no extra CT)
    data = r.data or {}
    assert r.status == 200 and r.success is True, f"login: {r}"
    assert_secret_shape(data.get("access_token"), what="data.access_token")
    assert (data.get("session") or {}).get("sid"), "data.session.sid missing"
    assert (data.get("user") or {}).get("id") == consumer.user.id, "data.user.id != the search id"
    assert isinstance(data.get("access_expires_at"), int), "data.access_expires_at missing"
    assert "require_verification" not in data, "require_verification set (2FA on a consumer account?)"
    fixture_sink.capture("login_ok", r, route="login")


# --------------------------------------------------------------------------- #
#  C02 verify request/response + 60s proof expiry                             #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C02")
def test_c02_verify_shape(consumer):
    now = int(time.time())
    r = consumer.user.proof("access_token.generate", {"scopes": ["usage:read"], "expires_at": 0})
    data = r.data or {}
    assert r.success is True, f"verify: {r}"
    assert_secret_shape(data.get("proof_token"), what="data.proof_token")
    assert data.get("method") == "password", "data.method"
    assert data.get("scope") == "access_token.generate", "data.scope"
    assert 50 <= int(data.get("expires_at") or 0) - now <= 70, "proof lifetime is not ~60s"


@pytest.mark.slow
@pytest.mark.contract_id("C02")
def test_c02_proof_expires_after_60s(consumer, slow, fixture_sink):
    r = consumer.user.proof("access_token.generate", {"scopes": ["usage:read"], "expires_at": 0})
    proof = (r.data or {}).get("proof_token")
    assert_secret_shape(proof, what="proof_token")
    time.sleep(63)
    used = consumer.jwt.post("/api/user/access_tokens",
                             {"name": "woow-paas-up-expired-000000000000", "scopes": ["usage:read"], "expires_at": 0},
                             headers={"X-Security-Proof": proof})
    assert used.code == "SECURITY_PROOF_EXPIRED", f"expired proof: {used}"
    fixture_sink.capture("security_proof_expired_error", used, route="access_tokens.create")


# --------------------------------------------------------------------------- #
#  C03 scope strings + context schemas                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C03")
def test_c03_generate_context_must_be_exact(consumer, fixture_sink):
    r = consumer.jwt.post("/api/verify", {
        "method": "password", "scope": "access_token.generate",
        "context": {"scopes": ["usage:read"], "expires_at": 0, "extra": 1}, "password": consumer.user.password})
    assert r.status == 400 and r.code == "SECURITY_CONTEXT_INVALID", f"generate + extra field: {r}"
    fixture_sink.capture("security_context_invalid_error", r, route="verify")


@pytest.mark.contract_id("C03")
def test_c03_revoke_context_is_token_id(consumer):
    r = consumer.jwt.post("/api/verify", {"method": "password", "scope": "access_token.revoke",
                                          "context": {"not_token_id": 1}, "password": consumer.user.password})
    assert r.status == 400 and r.code == "SECURITY_CONTEXT_INVALID", f"revoke without token_id: {r}"


@pytest.mark.contract_id("C03")
def test_c03_admin_contexts(lab, consumer):
    """admin.user.manage is exactly {user_id, action}; admin.user.create is
    exactly {role} with role >= 10 (service/security_verification.go)."""
    if not lab.creds:
        pytest.skip("admin contexts need NEWAPI_CONTRACT_ADMIN_CREDS_FILE (root step-up)")
    pw = lab.creds["password"]
    manage = lab.admin.post("/api/verify", {"method": "password", "scope": "admin.user.manage",
                                            "context": {"user_id": consumer.user.id, "action": "disable", "x": 1},
                                            "password": pw})
    assert manage.status == 400 and manage.code == "SECURITY_CONTEXT_INVALID", f"manage + extra: {manage}"
    create = lab.admin.post("/api/verify", {"method": "password", "scope": "admin.user.create",
                                            "context": {"role": 1}, "password": pw})
    assert create.status == 400 and create.code == "SECURITY_CONTEXT_INVALID", f"create role 1: {create}"


# --------------------------------------------------------------------------- #
#  C04 generate method is password when no 2FA                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C04")
def test_c04_generate_method_is_password(consumer):
    r = consumer.jwt.get("/api/verify/methods?scope=access_token.generate")
    methods = (r.data or {}).get("methods") or []
    pw = next((m for m in methods if m.get("method") == "password"), None)
    assert pw is not None and pw.get("available") is True, f"methods: {[m.get('method') for m in methods]}"


# --------------------------------------------------------------------------- #
#  C05 create access token shape (the narrow UP C07/C25 reuse)                #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def narrow_up(consumer):
    """A UP with profile:read only (2 CT, made once)."""
    token, item = consumer.user.create_up(scopes=["profile:read"], name=f"woow-paas-up-{consumer.user.id}-narrow")
    return token, item


@pytest.mark.contract_id("C05")
def test_c05_create_access_token(consumer, narrow_up, fixture_sink):
    token, item = narrow_up
    assert_secret_shape(token, prefix="nap_", length=47, alphabet=r"[0-9A-Za-z]{43}", what="data.token")
    assert isinstance(item.get("id"), int), "data.item.id"
    assert_secret_shape(item.get("token_ref"), length=64, alphabet=r"[0-9a-f]{64}", what="data.item.token_ref")
    for field in ("name", "token_hint", "expires_at", "last_used_at", "last_used_ip", "created_at", "scopes"):
        assert field in item, f"data.item.{field} missing"
    assert item.get("scopes") == ["profile:read"], "data.item.scopes"
    whoami = consumer.user.up_client(token).get("/api/user/self")
    assert whoami.success is True and (whoami.data or {}).get("id") == consumer.user.id, f"fresh UP: {whoami}"


@pytest.mark.contract_id("C05")
def test_c05_expiry_floor(consumer, fixture_sink):
    """expires_at must be 0 or >= now + 1 h (checked before the proof)."""
    r = consumer.jwt.post("/api/user/access_tokens", {"name": "woow-paas-up-c05-expiry", "scopes": ["usage:read"],
                                                      "expires_at": int(time.time()) + 120})
    assert r.status == 400 and r.code == "ACCESS_TOKEN_EXPIRY_INVALID", f"short expiry: {r}"
    fixture_sink.capture("access_token_expiry_invalid_error", r, route="access_tokens.create")


# --------------------------------------------------------------------------- #
#  C06 PAT and session management are session-only                            #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C06")
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/user/access_tokens"),
    ("GET", "/api/user/access_tokens/catalog"),
    ("GET", "/api/user/sessions"),
    ("DELETE", "/api/user/sessions/not-a-session"),
])
def test_c06_session_only_routes(consumer, fixture_sink, method, path):
    r = consumer.up.request(method, path)
    assert r.status == 403 and r.code == "AUTH_SESSION_REQUIRED", f"UP {method} {path}: {r}"
    if path == "/api/user/access_tokens":
        fixture_sink.capture("session_required_error", r, route="access_tokens.list")


# --------------------------------------------------------------------------- #
#  C07 scope names and the per-route scope map                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C07")
def test_c07_catalog_scope_names(consumer):
    r = consumer.jwt.get("/api/user/access_tokens/catalog")
    data = r.data or {}
    resources = {}
    for grp in data.get("groups") or []:
        for res in grp.get("resources") or []:
            resources[res.get("resource")] = {a.get("action") for a in res.get("actions") or []}
    # the consumer (role 1) sees the personal group; admin scopes are pinned on the admin PAT below
    assert {"read", "write", "reveal"} <= resources.get("api_key", set()), "api_key read/write/reveal"
    assert "read" in resources.get("usage", set()), "usage:read"
    assert "read" in resources.get("profile", set()), "profile:read"


@pytest.mark.contract_id("C07")
def test_c07_route_scope_map_consumer(consumer, narrow_up, fixture_sink):
    full, narrow = consumer.up, consumer.user.up_client(narrow_up[0])
    now = int(time.time())
    allowed = [("GET", "/api/user/self"), ("GET", "/api/user/models"), ("GET", "/api/token/?p=1&page_size=1"),
               ("GET", "/api/log/self?p=1&page_size=1"), ("GET", "/api/log/self/stat?type=2"),
               ("GET", f"/api/data/self?start_timestamp={now - 3600}&end_timestamp={now}")]
    for method, path in allowed:
        r = full.request(method, path)
        assert r.status == 200 and r.success is True, f"full UP {path}: {r}"
    denied = {"/api/token/?p=1&page_size=1": "api_key:read", "/api/log/self?p=1&page_size=1": "usage:read",
              f"/api/data/self?start_timestamp={now - 3600}&end_timestamp={now}": "usage:read"}
    for path, scope in denied.items():
        r = narrow.get(path)
        assert r.status == 403 and r.code == "ACCESS_TOKEN_SCOPE_DENIED", f"profile-only UP {path}: {r}"
        assert ((r.body.get("message_params") or {}).get("scope")) == scope, f"{path}: required scope"
    fixture_sink.capture("access_token_scope_denied_error", narrow.get("/api/token/?p=1&page_size=1"),
                         route="token.list")
    ok = narrow.get("/api/user/self")
    assert ok.success is True, f"profile-only UP /api/user/self: {ok}"


@pytest.mark.contract_id("C07")
def test_c07_route_scope_map_admin(lab):
    """The admin PAT (user:read/write, log:read): its routes work, an option
    route is scope-denied."""
    now = int(time.time())
    for path in ("/api/user/?p=1&page_size=1", "/api/log/?type=1&p=1&page_size=1",
                 f"/api/log/stat?type=2&start_timestamp={now - 3600}&end_timestamp={now}",
                 f"/api/data/?start_timestamp={now - 3600}&end_timestamp={now}"):
        r = lab.admin.get(path)
        assert r.status == 200 and r.success is True, f"admin PAT {path}: {r}"
    r = lab.admin.get("/api/option/")
    assert r.status == 403 and r.code == "ACCESS_TOKEN_SCOPE_DENIED", f"admin PAT /api/option/: {r}"


# --------------------------------------------------------------------------- #
#  C08 delete session revokes that JWT                                        #
# --------------------------------------------------------------------------- #
def _revoked_session(consumer):
    """Log the consumer in a second time and revoke that session at once."""
    user = consumer.user
    r = user.lab.base.post("/api/user/login", {"username": user.username, "password": user.password})
    data = r.data or {}
    jwt, sid = data.get("access_token"), (data.get("session") or {}).get("sid")
    assert jwt and sid, f"second login: {r}"
    client = user.lab.base.with_token(jwt)
    rev = client.delete(f"/api/user/sessions/{sid}")
    return client, sid, rev


@pytest.fixture(scope="module")
def revoked_jwt(consumer):
    """C08's revoked second session. C25 makes its own: it runs after the long
    waits, when this 15-minute access token has expired and new-api answers
    AUTH_TOKEN_EXPIRED before it looks at the session."""
    return _revoked_session(consumer)


@pytest.mark.contract_id("C08")
def test_c08_delete_session(consumer, revoked_jwt, fixture_sink):
    client, sid, rev = revoked_jwt
    assert rev.success is True, f"delete session: {rev}"
    assert (rev.data or {}).get("revoked_sid") == sid, "data.revoked_sid"
    assert (rev.data or {}).get("current") is True, "data.current"
    after = client.get("/api/user/self")
    assert after.status == 401, f"revoked JWT still works: {after}"
    fixture_sink.capture("auth_session_revoked_error", after, route="user.self")
    # the consumer's other session is untouched
    assert consumer.jwt.get("/api/user/self").success is True, "the other session was revoked too"


# --------------------------------------------------------------------------- #
#  C09 create user: no proof (role 1), no id, ignores group/remark            #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C09")
def test_c09_create_user(cfg, lab, fixture_sink):
    name = cfg.user_prefix + f"{secrets.randbelow(10 ** 10):010d}"
    password = new_password()
    r = lab.admin.post("/api/user/", {"username": name, "password": password, "display_name": "PaaS 0", "role": 1,
                                      "group": "vip", "remark": "should-be-ignored"})
    if r.success is True:
        lab.registry.add(name)
    assert r.success is True, f"create user: {r}"
    fixture_sink.capture("user_create_ok", r, route="user.create")
    assert r.data in (None, {}, "") and "id" not in (r.body or {}), "response carries an id"
    uid = lab.find_id(name)
    assert uid, "created user not found"
    user = TestUser(lab, name, password, uid)
    lab.registry.add(name, user)
    try:
        item = next(u for u in lab.search(name) if u.get("username") == name)
        assert item.get("group") == "default", "the sent group was not ignored"
        full = lab.admin.get(f"/api/user/{uid}")
        assert (full.data or {}).get("remark") in (None, ""), "remark was set on create"
    finally:
        user.cleanup()


# --------------------------------------------------------------------------- #
#  C10 search: Unscoped LIKE, id desc, equal width => exact                    #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C10")
def test_c10_search_like_order_unscoped(cfg, lab, fixture_sink):
    stem = f"{secrets.randbelow(10 ** 6):06d}"
    names = [cfg.user_prefix + stem + "0001", cfg.user_prefix + stem + "0002"]
    users = []
    try:
        for n in names:
            users.append(create_test_user(lab, username=n))
        r = lab.admin.get(f"/api/user/search?keyword={cfg.user_prefix + stem}&p=1&page_size=10")
        fixture_sink.capture("user_search_ok", r, route="user.search")
        items = (r.data or {}).get("items") or []
        matched = [u for u in items if u.get("username") in names]
        assert len(matched) == 2, f"LIKE on the stem matched {len(matched)} of 2"
        all_ids = [u["id"] for u in items]
        assert all_ids == sorted(all_ids, reverse=True), "not id desc"
        for u in matched:
            for field in ("id", "username", "status", "group", "role", "DeletedAt"):
                assert field in u, f"search item lacks {field}"
        exact = [u["username"] for u in lab.search(names[0]) if u.get("username") in names]
        assert exact == [names[0]], "the full equal-width name did not match exactly one"
        # Unscoped: a soft-deleted test user is still listed, with DeletedAt set.
        soft = [u for u in lab.search(cfg.user_prefix) if u.get("DeletedAt")
                and re.fullmatch(re.escape(cfg.user_prefix) + r"\d{10}", str(u.get("username")))]
        if not soft:
            if not lab.creds:
                pytest.skip("no soft-deleted test user to show Unscoped search, and no root creds to make one")
            target = users.pop()
            lab.soft_delete(target.id)
            target.deleted = True
            lab.registry.mark_deleted(target.username, soft=True)
            soft = [u for u in lab.search(target.username) if u.get("username") == target.username]
            assert soft and soft[0].get("DeletedAt"), "a soft-deleted user is missing from search (not Unscoped?)"
            gone = lab.admin.get(f"/api/user/{target.id}")
            assert gone.success is False and gone.message == "record not found", f"GET soft-deleted: {gone}"
            fixture_sink.capture("record_not_found_error", gone, route="user.get")
    finally:
        for u in users:
            u.cleanup()


# --------------------------------------------------------------------------- #
#  C11 PUT /api/user/ overwrites the four fields; same group keeps session    #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C11")
def test_c11_put_user_whole_overwrite_keeps_session(lab, consumer):
    user = consumer.user
    jwt = consumer.jwt
    full = {"id": user.id, "username": user.username, "display_name": "PaaS 0", "group": "default",
            "remark": "paas_llm_account=0"}
    assert lab.admin.put("/api/user/", full).success is True, "PUT with remark"
    assert (lab.admin.get(f"/api/user/{user.id}").data or {}).get("remark") == "paas_llm_account=0", "remark not set"
    partial = {k: full[k] for k in ("id", "username", "display_name", "group")}
    assert lab.admin.put("/api/user/", partial).success is True, "PUT without remark"
    assert (lab.admin.get(f"/api/user/{user.id}").data or {}).get("remark") in ("", None), \
        "a PUT without remark kept it (not a whole overwrite)"
    after = jwt.get("/api/user/self")
    assert after.status == 200 and after.success is True, "an unchanged group revoked the session"


# --------------------------------------------------------------------------- #
#  C12 add_quota request / type=1 log                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C12")
def test_c12_add_quota_and_log(lab, consumer, fixture_sink):
    quota = 15625  # 1 point
    r = lab.admin.post("/api/user/manage", {"id": consumer.user.id, "action": "add_quota", "mode": "add",
                                            "value": quota})
    assert r.success is True, f"add_quota: {r}"
    assert r.request_id, "no X-Oneapi-Request-Id"
    fixture_sink.capture("add_quota_ok", r, route="manage")
    logs = lab.admin.get(f"/api/log/?type=1&username={consumer.user.username}&p=1&page_size=100")
    items = (logs.data or {}).get("items") or []
    mine = next((lg for lg in items if lg.get("request_id") == r.request_id), None)
    assert mine is not None, "no type=1 log with the response's request id"
    other = mine.get("other")
    if isinstance(other, str):
        import json

        other = json.loads(other or "{}")
    op = (other or {}).get("op") or {}
    assert op.get("action") == "user.quota_add", "other.op.action"
    params = op.get("params") or {}
    assert params.get("quota") == quota, "other.op.params.quota"
    assert params.get("target_user_id") == consumer.user.id, "other.op.params.target_user_id"
    assert params.get("to", 0) - params.get("from", 0) == quota, "other.op.params.from/to"
    assert ((other or {}).get("admin_info") or {}).get("admin_id"), "other.admin_info.admin_id"


# --------------------------------------------------------------------------- #
#  C13 add_quota error message_keys (〔PROV〕§4.3)                             #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C13")
def test_c13_add_quota_errors(lab, consumer, fixture_sink):
    uid = consumer.user.id
    cases = [
        ({"id": uid, "action": "add_quota", "mode": "add", "value": 0}, "Quota change amount cannot be zero"),
        ({"id": uid, "action": "add_quota", "mode": "bogus", "value": 1}, "Invalid parameters"),
        ({"id": 2147483647, "action": "add_quota", "mode": "add", "value": 1}, "User does not exist"),
        ({"id": uid, "action": "add_quota", "mode": "add", "value": 2 ** 53}, "Wallet quota limit exceeded"),
    ]
    for body, key in cases:
        r = lab.admin.post("/api/user/manage", body)
        assert r.success is False and r.message_key == key, f"{body['mode']}/{body['value']}: {r}"
    fixture_sink.capture("add_quota_zero_error", lab.admin.post("/api/user/manage", cases[0][0]), route="manage")
    # the fifth key (same-or-higher role) is unreachable with the root PAT:
    # ids.UNREACHABLE["C13"] (in the report notes)


# --------------------------------------------------------------------------- #
#  C14 create token: no id/key; field names; created_time                     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C14")
def test_c14_create_token(consumer, fixture_sink):
    up = consumer.up
    name = f"wct_{consumer.user.id}_c14a"
    created = up.post("/api/token/", {
        "name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
        "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
        "cross_group_retry": False, "allow_user_quota_query": False})
    assert created.success is True, f"create: {created}"
    fixture_sink.capture("token_create_ok", created, route="token.create")
    assert not created.data, "POST /api/token/ returned data (id/key)"
    item = next((t for t in (up.get("/api/token/?p=1&page_size=100").data or {}).get("items") or []
                 if t.get("name") == name), None)
    assert item is not None, "created token not in the list"
    consumer.user.relay_tokens.add(item["id"])
    for field in ("id", "user_id", "status", "name", "created_time", "accessed_time", "expired_time", "remain_quota",
                  "unlimited_quota", "model_limits_enabled", "model_limits", "allow_ips", "used_quota", "group",
                  "cross_group_retry"):
        assert field in item, f"token item lacks {field}"
    assert isinstance(item["created_time"], int) and abs(item["created_time"] - time.time()) < 300, \
        "created_time is not the creation unix time (the proxy's reveal window needs it)"


# --------------------------------------------------------------------------- #
#  C15 list tokens: id desc; masked key                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C15")
def test_c15_list_tokens_order_and_mask(consumer, fixture_sink):
    for n in ("c15a", "c15b"):
        consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_{n}")
    r = consumer.up.get("/api/token/?p=1&page_size=100")
    fixture_sink.capture("token_list_ok", r, route="token.list")
    items = (r.data or {}).get("items") or []
    ids = [t["id"] for t in items]
    assert ids == sorted(ids, reverse=True), "not id desc"
    mask = re.compile(r"[0-9A-Za-z]{4}\*{10}[0-9A-Za-z]{4}")
    bad = [t["id"] for t in items if not mask.fullmatch(str.__str__(t.get("key") or ""))]
    assert not bad, f"keys not masked as 4+10*+4 for token ids {bad}"
    missing = consumer.up.get("/api/token/2147483647")
    assert missing.success is False and missing.message == "record not found" and not missing.message_key, \
        f"unknown token id: {missing}"
    fixture_sink.capture("token_record_not_found", missing, route="token.get")


# --------------------------------------------------------------------------- #
#  C16 reveal: 48-char key, no sk-; costs a CT                                #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C16")
def test_c16_reveal(consumer, budget, fixture_sink):
    tid, _, _ = consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_c16")
    before = budget.snapshot(budgetmod.ip_key("127.0.0.1"))
    r = consumer.up.post(f"/api/token/{tid}/key")
    after = budget.snapshot(budgetmod.ip_key("127.0.0.1"))
    assert r.success is True, f"reveal: {r}"
    fixture_sink.capture("token_reveal_ok", r, route="token.reveal")
    key = (r.data or {}).get("key")
    assert_secret_shape(key, length=48, alphabet=r"[0-9A-Za-z]{48}", what="data.key")
    assert not key.startswith("sk-"), "data.key carries sk-"
    if before is not None and after is not None and after[0] >= before[0]:
        assert after[0] - before[0] >= 1, "reveal did not take a CT"


# --------------------------------------------------------------------------- #
#  C17 whole PUT overwrites (drops allow_ips => clears); status_only           #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C17")
def test_c17_put_token_whole_overwrite(consumer):
    tid, _, _ = consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_c17",
                                                 allow_ips="203.0.113.7")
    up = consumer.up
    got = up.get(f"/api/token/{tid}").data or {}
    assert got.get("allow_ips") == "203.0.113.7", "allow_ips not stored"
    body = {k: got.get(k) for k in ("id", "name", "expired_time", "remain_quota", "unlimited_quota",
                                    "model_limits_enabled", "model_limits", "group", "cross_group_retry")}
    assert up.put("/api/token/", body).success is True, "whole PUT"
    assert ((up.get(f"/api/token/{tid}").data or {}).get("allow_ips") or "") == "", "a PUT without allow_ips kept it"
    assert up.put("/api/token/?status_only=1", {"id": tid, "status": 2}).success is True, "status_only"
    after = up.get(f"/api/token/{tid}").data or {}
    assert after.get("status") == 2 and after.get("name") == got.get("name"), "status_only changed more than status"


# --------------------------------------------------------------------------- #
#  C18 Redis mode: status 3/4 never written back; expired/exhausted/disabled  #
#  keys all 401 at the relay                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C18")
def test_c18_bad_keys_401_and_status_not_written_back(cfg, consumer, relay_base, fixture_sink):
    user, up = consumer.user, consumer.up_token
    expired, _, _ = user.create_relay_token(up, f"wct_{user.id}_c18e", expired_time=int(time.time()) - 3600)
    exhausted, _, _ = user.create_relay_token(up, f"wct_{user.id}_c18x", unlimited_quota=False, remain_quota=0)
    disabled, _, _ = user.create_relay_token(up, f"wct_{user.id}_c18d")
    assert consumer.up.put("/api/token/?status_only=1", {"id": disabled, "status": 2}).success is True
    keys = user.batch_reveal(up, [expired, exhausted, disabled])  # one CT for all three
    for tid, what in ((expired, "expired"), (exhausted, "exhausted"), (disabled, "disabled")):
        r = relay.chat(relay_base, keys[tid], cfg.free_model, max_tokens=1)
        assert r.status == 401, f"{what} key at the relay: {r.error_summary()}"
    status = {t["id"]: t.get("status") for t in (consumer.up.get("/api/token/?p=1&page_size=100").data or {})
              .get("items") or []}
    assert status.get(expired) == 1, f"expired key status {status.get(expired)} (Redis mode must not write 3)"
    assert status.get(exhausted) == 1, f"exhausted key status {status.get(exhausted)} (Redis mode must not write 4)"
    assert status.get(disabled) == 2, "disabled key status"


# --------------------------------------------------------------------------- #
#  C19 log/self shape; retention columns                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C19")
def test_c19_log_self_shape(lab, consumer, fixture_sink):
    lab.admin.post("/api/user/manage", {"id": consumer.user.id, "action": "add_quota", "mode": "add", "value": 15625})
    r = consumer.up.get("/api/log/self?type=1&p=1&page_size=10")
    fixture_sink.capture("log_self_ok", r, route="log.self")
    items = (r.data or {}).get("items") or []
    assert items, "no self log to inspect"
    item = items[0]
    # model/log.go assignDisplayLogIds: id = offset + i + 1, not the DB id
    assert [it.get("id") for it in items] == list(range(1, len(items) + 1)), "log/self id is not the display sequence"
    assert "request_id" in item and "channel" in item, "request_id / channel"
    other = item.get("other")
    if isinstance(other, str):
        import json

        other = json.loads(other or "{}") if other.strip().startswith("{") else {}
    assert "admin_info" not in (other or {}), "self view keeps admin_info"
    admin_view = lab.admin.get(f"/api/log/?type=1&username={consumer.user.username}&p=1&page_size=10")
    by_rid = {lg.get("request_id"): lg.get("id") for lg in (admin_view.data or {}).get("items") or []}
    assert by_rid.get(item.get("request_id")), "the admin view lacks the same log (request_id)"


@pytest.mark.contract_id("C19")
def test_c19_retention_columns(psql):
    """The retention CronJob deletes by logs.created_at / audit_logs.created_at
    (unix seconds; DESIGN §7.6)."""
    rows = psql("SELECT table_name || ':' || data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'created_at' "
                "AND table_name IN ('logs', 'audit_logs') ORDER BY table_name")
    assert rows == ["audit_logs:bigint", "logs:bigint"], f"created_at columns: {rows}"


# --------------------------------------------------------------------------- #
#  C20 log stat + data/self + flow/self                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C20")
def test_c20_stat_and_data(consumer, fixture_sink):
    up = consumer.up
    stat = up.get("/api/log/self/stat?type=2")
    assert stat.success is True, f"stat: {stat}"
    for field in ("quota", "rpm", "tpm"):
        assert field in (stat.data or {}), f"stat lacks {field}"
    now = int(time.time())
    daily = up.get(f"/api/data/self?start_timestamp={now - 86400}&end_timestamp={now}")
    assert daily.success is True and isinstance(daily.data, list), f"data/self: {daily}"
    flow = up.get(f"/api/data/flow/self?start_timestamp={now - 86400}&end_timestamp={now}")
    assert flow.success is True and isinstance(flow.data, list), f"flow/self: {flow}"
    bad = up.get("/api/data/flow/self")
    assert bad.success is False, "flow/self without timestamps succeeded"
    fixture_sink.capture("flow_self_missing_timestamps_error", bad, route="data.flow.self")


# --------------------------------------------------------------------------- #
#  C21 user/self fields; GET /api/user/:id request_count                      #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C21")
def test_c21_user_self_and_admin_get(lab, consumer, fixture_sink):
    r = consumer.up.get("/api/user/self")
    fixture_sink.capture("user_self_ok", r, route="user.self")
    data = r.data or {}
    for field in ("id", "group", "quota", "used_quota", "request_count", "status"):
        assert field in data, f"user/self lacks {field}"
    a = lab.admin.get(f"/api/user/{consumer.user.id}")
    fixture_sink.capture("user_get_ok", a, route="user.get")
    for field in ("id", "username", "group", "quota", "used_quota", "request_count", "status", "role"):
        assert field in (a.data or {}), f"GET /api/user/:id lacks {field}"
    # model/user.go: Remark is `json:"remark,omitempty"` — absent when empty, so
    # the PaaS reads a missing remark as "" (C11 just cleared it)
    assert isinstance((a.data or {}).get("remark", ""), str), "GET /api/user/:id remark is not a string"


# --------------------------------------------------------------------------- #
#  C22 /api/status invariants                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C22")
def test_c22_status_invariants(anon, fixture_sink):
    r = anon.get("/api/status")
    fixture_sink.capture("status_ok", r, route="status")
    data = r.data or {}
    want = {"quota_per_unit": 500000, "password_login_enabled": True, "turnstile_check": False,
            "password_login_encryption_enabled": False, "enable_batch_update": False, "enable_data_export": True,
            "register_enabled": False, "self_use_mode_enabled": False}
    bad = {k: data.get(k) for k, v in want.items() if data.get(k) != v}
    assert not bad, f"/api/status drifted: {bad}"
    assert data.get("version"), "/api/status version missing"


# --------------------------------------------------------------------------- #
#  C23 rate-limit map (counter deltas; main run) + the 429 run (staged)       #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C23")
def test_c23_route_limit_map(lab, consumer, kube, budget, contract_metric):
    """Which route takes which bucket (〔PROV〕§1.2), from Valkey counter
    deltas — no bucket is emptied."""
    if kube is None:
        pytest.skip("needs NEWAPI_CONTRACT_KUBECTL (Valkey counters)")
    ct, ga = budgetmod.ip_key("127.0.0.1"), budgetmod.ip_key("127.0.0.1", "GA")
    uc = budgetmod.user_key(consumer.user.id)
    jwt = consumer.jwt  # refresh (if due) before measuring
    budget.take(ct, n=1, label="C23 map")  # room for the one CT call below

    def delta(call):
        before = {k: kube.counter(k)[0] for k in (ct, ga, uc)}
        call()
        after = {k: kube.counter(k) for k in (ct, ga, uc)}
        return {k: after[k][0] - before[k] for k in before}, after[ct][1]

    d_methods, _ = delta(lambda: jwt.get("/api/verify/methods?scope=access_token.generate", ct_exempt=True))
    d_list, _ = delta(lambda: jwt.get("/api/user/access_tokens", ct_exempt=True))
    d_self, _ = delta(lambda: consumer.up.get("/api/user/self", ct_exempt=True))
    d_verify, ttl = delta(lambda: consumer.user.lab.base.with_token(consumer.user.jwt).post(
        "/api/verify", {"method": "password", "scope": "access_token.generate",
                        "context": {"scopes": ["usage:read"], "expires_at": 0}, "password": consumer.user.password},
        ct_exempt=True))
    contract_metric("C23", {"map": {"verify_methods": d_methods, "access_tokens_list": d_list, "user_self": d_self,
                                    "verify": d_verify}, "ct_ttl_seconds": ttl})
    for name, d in (("GET /api/verify/methods", d_methods), ("GET /api/user/access_tokens", d_list),
                    ("GET /api/user/self", d_self)):
        assert d[ct] == 0, f"{name} took a CT"
        assert d[ga] >= 1, f"{name} did not take a GA"
    assert d_verify[ct] == 1 and d_verify[uc] == 1 and d_verify[ga] >= 1, f"POST /api/verify deltas {d_verify}"
    assert 0 < ttl <= 1200, f"CT window TTL {ttl}s (default 1200)"


@pytest.mark.ct_exhaust
@pytest.mark.contract_id("C23")
def test_c23_critical_limit_429(cfg, base_client, kube, contract_metric, fixture_sink):
    """21+ bad logins trip the 127.0.0.1 CT bucket (20/1200 s): 429, empty
    body, Retry-After; then measure how long until it lets a login through.
    Blocks every port-forward user of new-api-stg for up to 20 minutes."""
    if not cfg.ct_exhaust:
        pytest.skip("C23 429 run: NEWAPI_CONTRACT_CT_EXHAUST=1 pytest -m ct_exhaust (alone, after the main run)")
    t0 = time.time()
    hit, attempts = None, 0
    for attempts in range(1, 26):  # noqa: B007 — read after the loop
        r = base_client.post("/api/user/login", {"username": "nouser000000", "password": "x" * 12}, ct_exempt=True)
        if r.status == 429:
            hit = r
            break
    assert hit is not None, "the CT limit never tripped in 25 attempts"
    fixture_sink.capture("ratelimit_429_empty", hit, route="login")
    assert hit.raw_empty, "a limiter 429 must have an empty body"
    assert hit.retry_after and hit.retry_after.isdigit(), "Retry-After missing"
    ttl = kube.counter(budgetmod.ip_key("127.0.0.1"))[1] if kube is not None else int(hit.retry_after)
    time.sleep(max(ttl, 1) + 2)
    back = base_client.post("/api/user/login", {"username": "nouser000000", "password": "x" * 12}, ct_exempt=True)
    recovered = time.time() - t0
    contract_metric("C23", {"exhaust": {"attempts_to_429": attempts, "retry_after": int(hit.retry_after),
                                        "recovery_seconds": round(recovered, 1)}})
    assert back.status == 200 and back.success is False, f"after the window: {back}"


# --------------------------------------------------------------------------- #
#  C24 TRUSTED_PROXIES: the proxy's XFF becomes ClientIP (session ip, PAT     #
#  last_used_ip) — the observation is shared with X01 (test_proxy_x.py)       #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.contract_id("C24")
def test_c24_trusted_proxies_honour_proxy_xff(ip_observation):
    obs = ip_observation
    assert obs["tenant_session_ip"] == obs["tenant_ip"], f"session ip {obs['tenant_session_ip']!r}"
    assert obs["tenant_up_ip"] == obs["tenant_ip"], f"tenant UP last_used_ip {obs['tenant_up_ip']!r}"
    assert obs["ap_ip"] == "198.18.0.1", f"AP last_used_ip {obs['ap_ip']!r}"


# --------------------------------------------------------------------------- #
#  C25 stable error codes                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C25")
def test_c25_error_codes(consumer, narrow_up, fixture_sink):
    assert consumer.up.get("/api/user/access_tokens").code == "AUTH_SESSION_REQUIRED"
    assert consumer.user.up_client(narrow_up[0]).get("/api/token/?p=1&page_size=1").code == "ACCESS_TOKEN_SCOPE_DENIED"
    bad = consumer.user.up_client("nap_" + "0" * 43).get("/api/user/self")
    assert bad.status == 401 and bad.code == "AUTH_UNAUTHORIZED", f"unknown PAT: {bad}"
    fixture_sink.capture("unauthorized_error", bad, route="user.self")
    rev = _revoked_session(consumer)[0].get("/api/user/self")
    assert rev.status == 401 and rev.code == "AUTH_SESSION_REVOKED", f"revoked session: {rev}"
    nojwt = consumer.user.lab.base.get("/api/user/self")
    assert nojwt.status == 401 and nojwt.code == "AUTH_UNAUTHORIZED", f"no credential: {nojwt}"
    priv = consumer.jwt.get("/api/user/search?keyword=x&p=1&page_size=1")
    assert priv.status == 403 and priv.code == "AUTH_INSUFFICIENT_PRIVILEGE", f"role 1 on an admin route: {priv}"
    fixture_sink.capture("insufficient_privilege_error", priv, route="user.search")


@pytest.mark.slow
@pytest.mark.contract_id("C25")
def test_c25_session_limits(lab, proxy_cron, fixture_sink, contract_metric):
    """50 active sessions -> 409 AUTH_SESSION_LIMIT; 100 issued per 24 h ->
    429 AUTH_SESSION_ISSUANCE_LIMIT *with* a JSON code (a limiter 429 has an
    empty body, C23). Its own user, logging in through the proxy from the
    suite's spare tenant IPs (19 logins per IP keeps each CT bucket under 20)."""
    from .config import SUITE_TENANT_IPS
    from .tenant import ProxyTenant

    user = create_test_user(lab, proxy=True)
    try:
        t = ProxyTenant(user, proxy_cron, SUITE_TENANT_IPS[10])
        ips = list(SUITE_TENANT_IPS[10:])
        n = [0]

        def login():
            ip = ips[n[0] // 19]
            n[0] += 1
            return t.login(ip=ip, keep=False)

        def revoke_all(rs):
            for r in rs:
                data = r.data or {}
                jwt, sid = data.get("access_token"), (data.get("session") or {}).get("sid")
                d = t.send("DELETE", f"/api/user/sessions/{sid}", auth=jwt, ip=ips[0])
                assert d.success is True, f"revoke a session: {d}"

        first = [login() for _ in range(50)]
        assert all(r.success is True for r in first), "one of the first 50 logins failed"
        limit = login()
        assert limit.status == 409 and limit.code == "AUTH_SESSION_LIMIT", f"51st active session: {limit}"
        fixture_sink.capture("auth_session_limit_error", limit, route="login")
        revoke_all(first)
        second = [login() for _ in range(50)]
        assert all(r.success is True for r in second), "logins 51-100 failed"
        revoke_all(second)
        issued = login()
        assert issued.status == 429 and issued.code == "AUTH_SESSION_ISSUANCE_LIMIT", f"101st issue: {issued}"
        assert not issued.raw_empty, "the issuance 429 must carry a JSON body (unlike the limiter 429)"
        fixture_sink.capture("auth_session_issuance_limit_error", issued, route="login")
        contract_metric("C25", {"session_limit_logins": n[0]})
    finally:
        user.cleanup()


@pytest.mark.run_last
@pytest.mark.slow
@pytest.mark.contract_id("C25")
def test_c25_expiry_codes(cfg, consumer, fixture_sink, contract_partial):
    """Checked at the very end of the run: the consumer's first dashboard JWT
    (15 minutes) answers 401 AUTH_TOKEN_EXPIRED, and the UP made at session
    start with expires_at = now + 1 h (the minimum) answers 401
    ACCESS_TOKEN_EXPIRED. Waits at most 40 minutes for either (a full gate run
    with CT pacing takes about as long, README)."""
    if not consumer.expiring:
        pytest.skip("no expiring UP (NEWAPI_CONTRACT_SKIP_SLOW=1)")
    first_jwt = (consumer.user.login_response.data or {}).get("access_token")
    jwt_exp = (consumer.user.login_response.data or {}).get("access_expires_at") or 0
    # rc.44 parses the JWT with jwt.WithLeeway(5 * time.Second)
    # (service/auth_token.go:194): it is still accepted 5 s past exp.
    jwt_exp += JWT_LEEWAY_S
    wait = jwt_exp + 3 - time.time()
    if 0 < wait <= MAX_EXPIRY_WAIT:
        time.sleep(wait)
    if time.time() > jwt_exp:
        r = consumer.user.lab.base.with_token(first_jwt).get("/api/user/self")
        assert r.status == 401 and r.code == "AUTH_TOKEN_EXPIRED", f"expired JWT: {r}"
        fixture_sink.capture("auth_token_expired_error", r, route="user.self")
    else:
        contract_partial("the first JWT had not expired when the run ended")
    token, exp = consumer.expiring
    wait = exp + 3 - time.time()
    if wait > MAX_EXPIRY_WAIT:
        contract_partial(f"the 1-hour UP still had {int(wait)}s to live at the end of the run; "
                         "ACCESS_TOKEN_EXPIRED not observed")
        return
    if wait > 0:
        time.sleep(wait)
    r = consumer.user.up_client(token).get("/api/user/self")
    assert r.status == 401 and r.code == "ACCESS_TOKEN_EXPIRED", f"expired UP: {r}"
    fixture_sink.capture("access_token_expired_error", r, route="user.self")


# --------------------------------------------------------------------------- #
#  C26 length limits                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C26")
def test_c26_user_field_limits(cfg, lab):
    """username <= 20, display_name <= 20, password 8-128 (POST /api/user/)."""
    base = cfg.user_prefix + f"{secrets.randbelow(10 ** 10):010d}"
    cases = [
        ("username 21", base + "x" * (21 - len(base)), "PaaS 0", "p" * 12),
        ("display_name 21", base, "D" * 21, "p" * 12),
        ("password 7", base, "PaaS 0", "p" * 7),
        ("password 129", base, "PaaS 0", "p" * 129),
    ]
    for what, name, display, pw in cases:
        r = lab.admin.post("/api/user/", {"username": name, "password": pw, "display_name": display, "role": 1})
        if r.success is True:
            lab.registry.add(name)  # the sweep removes it
        assert r.success is False, f"{what} was accepted"


@pytest.mark.contract_id("C26")
def test_c26_pat_name_and_cap(consumer):
    """PAT name 1-64 *characters* (runes, checked before the proof), 20 PATs per user."""
    jwt = consumer.jwt
    for name in ("", "名" * 65):
        r = jwt.post("/api/user/access_tokens", {"name": name, "scopes": ["usage:read"], "expires_at": 0})
        assert r.status == 400 and r.code == "ACCESS_TOKEN_NAME_INVALID", f"name of {len(name)} runes: {r}"
    ok = jwt.post("/api/user/access_tokens", {"name": "名" * 64, "scopes": ["usage:read"], "expires_at": 0})
    assert ok.code == "SECURITY_PROOF_REQUIRED", f"a 64-rune (192-byte) name must pass the name check: {ok}"
    cat = jwt.get("/api/user/access_tokens/catalog")
    assert (cat.data or {}).get("max_tokens") == 20, "catalog max_tokens (PATs per user)"


@pytest.mark.contract_id("C26")
def test_c26_token_name_bytes_and_page_size(consumer):
    """relay token name <= 50 *bytes* (CJK = 3 bytes); page_size capped at 100."""
    def create(name):
        return consumer.up.post("/api/token/", {
            "name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
            "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
            "cross_group_retry": False, "allow_user_quota_query": False})

    assert create("名" * 17).success is False, "a 17-CJK-character (51-byte) token name was accepted"
    ok_name = "名" * 16  # 48 bytes
    assert create(ok_name).success is True, "a 16-CJK-character (48-byte) token name was refused"
    item = next((t for t in (consumer.up.get("/api/token/?p=1&page_size=100").data or {}).get("items") or []
                 if t.get("name") == ok_name), None)
    assert item, "the 48-byte token is not in the list"
    consumer.user.relay_tokens.add(item["id"])
    for i in range(3):
        consumer.user.create_relay_token(consumer.up_token, f"wct_{consumer.user.id}_c26{i}")
    r = consumer.up.get("/api/token/?p=1&page_size=500")
    data = r.data or {}
    assert r.success is True and "page_size" in data, f"page_size missing: {r}"
    assert data["page_size"] <= 100 and len(data.get("items") or []) <= 100, "page_size not capped at 100"
    small = consumer.up.get("/api/token/?p=1&page_size=2").data or {}
    assert len(small.get("items") or []) == 2, "page_size=2 did not return 2 items"


# --------------------------------------------------------------------------- #
#  C27 password/group change keeps PATs; disable 401s; enable restores         #
# --------------------------------------------------------------------------- #
@pytest.mark.contract_id("C27")
def test_c27_pat_survives_password_group_and_disable(lab, consumer, narrow_up, fixture_sink):
    if not lab.creds:
        pytest.skip("C27 needs the root step-up (NEWAPI_CONTRACT_ADMIN_CREDS_FILE)")
    user = consumer.user
    up = user.up_client(narrow_up[0])
    assert up.get("/api/user/self").success is True
    # password change (admin.user.update): browser sessions go, the PAT stays
    old_jwt = user.lab.base.with_token(user.jwt) if user.jwt else None
    lab.set_password(user, new_password())
    assert up.get("/api/user/self").success is True, "a password change broke the PAT"
    if old_jwt is not None:
        assert old_jwt.get("/api/user/self").status == 401, "a password change kept the old session"
    # group change: auth_version bump, PAT stays; back to default
    cur = lab.admin.get(f"/api/user/{user.id}").data or {}
    body = {"id": user.id, "username": user.username, "display_name": cur.get("display_name") or "PaaS 0",
            "remark": cur.get("remark") or ""}
    assert lab.admin.put("/api/user/", dict(body, group="free")).success is True, "group -> free"
    try:
        assert up.get("/api/user/self").success is True, "a group change broke the PAT"
    finally:
        assert lab.admin.put("/api/user/", dict(body, group="default")).success is True, "group -> default"
    # disable: the PAT answers 401 AUTH_USER_DISABLED; enable restores it
    lab.manage(user.id, "disable")
    try:
        r = up.get("/api/user/self")
        assert r.status == 401 and r.code == "AUTH_USER_DISABLED", f"disabled user's PAT: {r}"
        fixture_sink.capture("auth_user_disabled_error", r, route="user.self")
    finally:
        lab.manage(user.id, "enable")
    assert up.get("/api/user/self").success is True, "enable did not restore the PAT"
    user.jwt = None  # the next JWT use logs in again with the new password
