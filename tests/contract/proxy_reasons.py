"""Every ``reason`` the newapi-proxy can answer with, by error class.

Copied from the proxy source at the commit stg runs
(``woow_paas_platform`` 59b03479, ``extra/newapi-proxy/newapi_proxy/``:
``app.py`` ProxyError calls, ``policy.py`` PolicyDenied codes,
``canonical.py`` CanonicalError codes, ``upstream.py`` details). The
fixture self-check (``selfcheck.check_fixture_reasons``) refuses a fixture
whose reason is not in here, so a hand-made fixture cannot carry a reason the
proxy never sends (NA-03 review: ``add_quota_cap``, ``body``, ``ap_missing``).
``NEWAPI_CONTRACT_PROXY_SRC=<path to extra/newapi-proxy>`` makes the
self-check re-derive the literal codes from that source and compare.

A 401 always answers ``reason: "auth"`` (the precise code is only logged).
"""
from __future__ import annotations

import re

PROXY_SOURCE_COMMIT = "59b03479ef833b1b62260692431a03782bb7b47c"

STATUS = {
    "bad_request": 400, "auth": 401, "policy": 403, "denied": 403, "body_timeout": 408,
    "too_large": 413, "budget": 429, "internal": 500, "connect": 502, "reset": 502,
    "config": 503, "busy": 503, "timeout": 504,
}

_CANONICAL = {
    "path_backslash", "path_chars", "path_dot_segment", "path_dotdot", "path_double_slash",
    "path_missing", "path_percent", "path_semicolon", "path_too_long", "query_chars",
    "query_duplicate_key", "query_empty_value", "query_key_format", "query_pair",
    "query_percent", "query_plus", "query_semicolon", "query_too_long",
}

_POLICY_BODY = {
    "user_create_fields", "user_create_username", "user_create_password",
    "user_create_display_name", "user_create_role",
    "manage_fields", "manage_id", "manage_action", "manage_mode", "manage_value",
    "login_fields", "login_username", "login_password",
    "verify_fields", "verify_method", "verify_context", "verify_scopes", "verify_expires_at",
    "verify_scope", "verify_password",
    "pat_fields", "pat_name", "pat_scopes", "pat_expires_at",
    "token_fields", "token_name", "token_expired_time", "token_remain_quota",
    "token_unlimited_quota", "token_model_limits_enabled", "token_cross_group_retry",
    "token_allow_user_quota_query", "token_flags", "token_allow_ips", "token_group",
    "token_model_limits", "token_free_models", "token_free_quota",
}

REASONS = {
    "bad_request": {
        "duplicate_header", "body_json", "body_not_object", "client_ip_missing", "client_ip_format",
        "client_ip_range", "client_disconnect", "content_length", "identity_format", "read_timeout",
        "identity_unknown", "client_ip_not_allowed", "query_key", "query_value",
        "authorization_not_allowed", "authorization_missing", "authorization_format",
        "proof_missing", "proof_not_allowed", "body_not_allowed",
    } | _CANONICAL,
    # what the client sees; the logged codes are auth_missing, auth_kid,
    # auth_ts, auth_ts_skew, auth_nonce_format, auth_sig_format, auth_sig, auth_replay
    "auth": {"auth"},
    "policy": {"key_scope", "break_glass_off", "route", "service_token"} | _POLICY_BODY,
    "denied": {"reveal_token_mismatch", "reveal_name", "reveal_window", "reveal_not_found",
               "manage_target", "manage_not_found"},
    "body_timeout": {"body_timeout"},
    "too_large": {"body_too_large"},
    "budget": {"hourly_cap"},
    "internal": {"internal"},
    "connect": {"deadline", "upstream_connect"},
    "reset": {"upstream_reset", "response_too_large"},
    "config": {"ap_token_missing", "op_token_missing"},
    "busy": {"inflight", "replay_store", "replay_cache_full"},
    "timeout": {"deadline", "upstream_timeout"},
}

#: Reasons built at run time (``f"{what}_precheck_..."``).
DYNAMIC = {
    "connect": re.compile(r"(reveal|manage)_precheck_(connect|reset|timeout|body|http_\d{3})"),
}


def known(kind, reason):
    if kind not in REASONS:
        return False
    if reason in REASONS[kind]:
        return True
    rx = DYNAMIC.get(kind)
    return bool(rx and rx.fullmatch(reason or ""))


def source_literals(src_dir):
    """Literal (kind, reason) pairs and policy codes found in a proxy checkout."""
    import os

    pairs, policy, canonical = set(), set(), set()
    pkg = os.path.join(src_dir, "newapi_proxy")
    for name in ("app.py", "policy.py", "canonical.py"):
        with open(os.path.join(pkg, name)) as f:
            text = f.read()
        pairs |= set(re.findall(r'ProxyError\("([a-z_]+)", "([a-z_]+)"', text))
        policy |= set(re.findall(r'PolicyDenied\("([a-z_]+)"\)', text))
        policy |= set(re.findall(r'(?:_exact_keys|_int_in|_up_scopes|_up_expires_at|_password|_username|_model_list)'
                                 r'\([^)]*"([a-z_]+)"\)', text))
        canonical |= set(re.findall(r'CanonicalError\("([a-z_]+)"\)', text))
    return pairs, policy, canonical
