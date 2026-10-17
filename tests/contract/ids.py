"""Authoritative list of contract ids and their one-line subject.

The report (``conftest.py``) and the self-check (``selfcheck.py``) use this to
prove every id C01-C27, F01-F12, X01-X09 has at least one collected test. Keep
it in step with the source contracts: 〔PROV〕§6.3 (C), 〔FREE〕§7 / DESIGN §8.3
(F), DESIGN §8.3 + extra/newapi-proxy/README.md (X).
"""
from __future__ import annotations

CONTRACT_IDS = {
    # C01-C27 — new-api API surface the PaaS depends on (〔PROV〕§6.3)
    "C01": "login returns access_token, session.sid, user.id; no require_verification",
    "C02": "verify request fields and proof_token; proof expires in 60s",
    "C03": "scope strings and context schemas (generate/revoke/manage/create)",
    "C04": "access_token.generate method is password when no 2FA",
    "C05": "create access_token returns nap_ 47-char token and item.id/token_ref",
    "C06": "PAT management is session-only; UP gets AUTH_SESSION_REQUIRED",
    "C07": "scope names; a PAT missing a scope gets ACCESS_TOKEN_SCOPE_DENIED",
    "C08": "delete session returns revoked_sid; the JWT then 401s",
    "C09": "POST /api/user/ needs no proof (role 1), returns no id, ignores group/remark",
    "C10": "user search is Unscoped LIKE, id desc; items carry id/username/status/group/role/DeletedAt",
    "C11": "PUT /api/user/ overwrites the four fields; unchanged group keeps the session",
    "C12": "add_quota request, X-Oneapi-Request-Id, type=1 log other.op.params",
    "C13": "add_quota error message_key list",
    "C14": "POST /api/token/ returns no id/key; field names",
    "C15": "GET /api/token/ id desc; masked key format",
    "C16": "reveal returns 48-char data.key, no sk-; costs a CT",
    "C17": "PUT /api/token/ whole overwrite (dropping allow_ips clears it); status_only",
    "C18": "Redis mode: token status 3/4 is not written back to the DB",
    "C19": "log/self id is a display sequence, has request_id and channel, other has no admin_info",
    "C20": "log/self/stat quota/rpm/tpm; data/self and flow/self fields; flow needs both timestamps",
    "C21": "user/self fields id/group/quota/used_quota/request_count/status",
    "C22": "/api/status invariants (quota_per_unit 500000, flags)",
    "C23": "rate-limit map and defaults; 429 empty body + Retry-After",
    "C24": "TRUSTED_PROXIES / XFF: the synthetic IP lands in last_used_ip and session ip",
    "C25": "stable error codes list",
    "C26": "length limits (username, display_name, password, token name, PAT name, page_size, 20 PATs)",
    "C27": "a password/group change keeps PATs valid; disabling the user 401s, enabling restores",
    # F01-F12 — free tier (〔FREE〕§7 / DESIGN §8.3)
    "F01": "balance-0 trial key 200 OK; log quota=0, group=free",
    "F02": "control: enable_free_model_pre_consume=true -> 403 (then put it back)",
    "F03": "25 requests in a row: exactly 10 pass, then 429; +6s one more passes",
    "F04": "50 concurrent: at most 10 are let through (atomic)",
    "F05": "bypass: paid junk-model interleave, free still <=19/min; counter-example default:[600,600]",
    "F06": "daily cap (threshold 5): key disabled <=5min; 00:00 job restores; suspend keys untouched",
    "F07": "paid: the 121st success per minute starts 429",
    "F08": "trial key -> paid model 403; paid key -> Qwen 503",
    "F09": "fuse: GroupRatio.free=1 -> 403, wallet unchanged (then put it back)",
    "F10": "TrustQuotaUSD=0 overdraft: 200+ concurrent worst-output model, measure per-account overdraft",
    "F11": "Valkey restart: counters reset; watch for 500s",
    "F12": "after upgrade, Redis key format rateLimit:<uid>:free",
    # X01-X09 — proxy contract (DESIGN §8.3)
    "X01": "synthetic IP shows in last_used_ip and session ip",
    "X02": "forged X-Forwarded-For / X-Real-IP is ignored",
    "X03": "non-whitelisted path 403 policy",
    "X04": "missing/wrong signature 401 (incl. expired ts, replay)",
    "X05": "ap identity carrying its own Authorization 400",
    "X06": "upstream closed -> connect; upstream stalled -> timeout",
    "X07": "fresh key reveal 200; a key older than 120s reveal 403",
    "X08": "body policy (override/subtract/non-prefix target/non-prefix create/PUT group) 403",
    "X09": "add_quota handed upstream, proxy killed before the reply -> client Unknown",
}

C_IDS = tuple(f"C{i:02d}" for i in range(1, 28))
F_IDS = tuple(f"F{i:02d}" for i in range(1, 13))
X_IDS = tuple(f"X{i:02d}" for i in range(1, 10))
ALL_IDS = C_IDS + F_IDS + X_IDS
