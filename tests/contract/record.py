"""Record de-identified response fixtures for LLM-02's newapi_client tests.

The fixtures carry the *shape* of every response the PaaS classifies
(DESIGN §3.4): new-api success/error envelopes and the proxy's own error
bodies. Transport failures (``RemoteDisconnected``, ``ReadTimeout``,
``ConnectTimeout``) are client exceptions, not bodies, so LLM-02 simulates
those directly — only their matching proxy error *bodies* are captured here.

Usage (on stg, with the env of config.py set)::

    python3 -m tests.contract.record --out tests/contract/fixtures

Live new-api/proxy responses are recorded when configured; the proxy error
bodies are emitted from the fixed contract shape (extra/newapi-proxy/README.md
〈回應與錯誤〉) so the set is complete even when a class is hard to provoke.
Every file is scrubbed (scrub.py) and re-scanned for residue before writing; a
leak aborts the run (acceptance 5).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tests.contract import newapi, scrub, users
    from tests.contract.config import Config
else:
    from . import newapi, scrub, users
    from .config import Config


# Proxy error bodies are a fixed shape; emit one per class (reason is an
# example). The PaaS classifier keys on proxy_error + HTTP status, not reason.
PROXY_ERROR_CASES = [
    ("proxy_bad_request", 400, "bad_request", "manage_id"),
    ("proxy_too_large", 413, "too_large", "body"),
    ("proxy_body_timeout", 408, "body_timeout", "body_timeout"),
    ("proxy_auth", 401, "auth", "auth"),
    ("proxy_policy", 403, "policy", "token_group"),
    ("proxy_denied_reveal_window", 403, "denied", "reveal_window"),
    ("proxy_denied_manage_target", 403, "denied", "manage_target"),
    ("proxy_budget", 429, "budget", "add_quota_cap"),
    ("proxy_busy_inflight", 503, "busy", "inflight"),
    ("proxy_busy_replay_store", 503, "busy", "replay_store"),
    ("proxy_config", 503, "config", "ap_missing"),
    ("proxy_connect", 502, "connect", "deadline"),
    ("proxy_reset", 502, "reset", "upstream_reset"),
    ("proxy_timeout", 504, "timeout", "deadline"),
    ("proxy_internal", 500, "internal", "internal"),
]


def _write(out_dir, name, obj):
    cleaned = scrub.scrub(obj)
    leak = scrub.residue(cleaned)
    if leak:
        raise SystemExit(f"refusing to write {name}: secret residue {leak}")
    path = os.path.join(out_dir, name + ".json")
    with open(path, "w") as f:
        json.dump(cleaned, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {path}")


def _record_proxy_errors(out_dir):
    for name, status, kind, reason in PROXY_ERROR_CASES:
        body = {
            "success": False,
            "message": f"newapi-proxy: {kind}",
            "proxy_error": kind,
            "reason": "auth" if kind == "auth" else reason,
        }
        _write(out_dir, name, {"_http_status": status, "_proxy_error": kind, "body": body})


def _record_live(out_dir, cfg):
    if not cfg.have_newapi:
        print("new-api not configured; skipping live capture (proxy error bodies still written)")
        return
    admin = newapi.Client(cfg.base, token=_read_pat(cfg.admin_pat_file))
    base = newapi.Client(cfg.base)
    anon = base.get("/api/status")
    _write(out_dir, "status_ok", {"_http_status": anon.status, "body": anon.body})
    user = users.create_test_user(admin, base, cfg.user_prefix)
    try:
        login = user.login()
        _write(out_dir, "login_ok", {"_http_status": login.status, "body": login.body})
        up_token, item = user.create_up(scopes=["usage:read"])
        # create-access-token success (token already scrubbed to <redacted>)
        _write(out_dir, "access_token_create_ok",
               {"_http_status": 200, "body": {"success": True, "message": "",
                                              "data": {"token": up_token, "item": item}}})
        # add_quota success + zero-value error
        ok = admin.post("/api/user/manage", {"id": user.id, "action": "add_quota", "mode": "add", "value": 15625})
        _write(out_dir, "add_quota_ok", {"_http_status": ok.status, "body": ok.body,
                                         "_request_id": ok.request_id})
        zero = admin.post("/api/user/manage", {"id": user.id, "action": "add_quota", "mode": "add", "value": 0})
        _write(out_dir, "add_quota_zero_error", {"_http_status": zero.status, "body": zero.body})
        # token create + list + reveal
        tid, created, listed = user.create_relay_token(up_token, f"wct_{user.id}_rec")
        _write(out_dir, "token_create_ok", {"_http_status": created.status, "body": created.body})
        listing = user.up_client(up_token).get("/api/token/?p=1&page_size=10")
        _write(out_dir, "token_list_ok", {"_http_status": listing.status, "body": listing.body})
        reveal = user.up_client(up_token).post(f"/api/token/{tid}/key")
        _write(out_dir, "token_reveal_ok", {"_http_status": reveal.status, "body": reveal.body})
        # UP on a session-only route (403 code) and a bad PAT (401 code)
        denied = user.up_client(up_token).get("/api/user/access_tokens")
        _write(out_dir, "session_required_error", {"_http_status": denied.status, "body": denied.body})
        unauth = user.up_client("nap_" + "0" * 43).get("/api/user/self")
        _write(out_dir, "unauthorized_error", {"_http_status": unauth.status, "body": unauth.body})
        self_log = user.up_client(up_token).get("/api/log/self?type=1&p=1&page_size=5")
        _write(out_dir, "log_self_ok", {"_http_status": self_log.status, "body": self_log.body})
    finally:
        user.cleanup(up_token)


def _read_pat(path):
    with open(os.path.expanduser(path)) as f:
        return f.read().strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "fixtures"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    cfg = Config()
    _record_proxy_errors(args.out)
    _record_live(args.out, cfg)
    print("done")


if __name__ == "__main__":
    main()
