"""De-identified response fixtures for LLM-02's ``newapi_client`` unit tests.

Every response class the PaaS classifies (DESIGN §3.4) gets one JSON file::

    {"_source": "live" | "contract-shape", "_route": "<what was called>",
     "_http_status": 403, "_headers": {"X-Woow-Proxy-Error": "denied", ...},
     "_raw_empty": false, "body": {...}}

and the one transport case, a request the proxy had already forwarded when
its pod was killed (X09)::

    {"_source": "live", "_transport": "RemoteDisconnected", "after_send": true,
     "route": "manage", "executed": true}

``live`` fixtures are written by the contract run itself: set
``NEWAPI_CONTRACT_RECORD_DIR`` and the tests that provoke a class (X03-X09,
C13, C18, C23, C25, C27, F11 …) capture what new-api-stg / the proxy really
answered, headers included. ``contract-shape`` fixtures are the classes stg
cannot produce on demand (a Go panic, a token-enable error that needs a
non-Redis deployment, the proxy's busy/config states); they are built here
from the source (``proxy_reasons.py``; 〔PROV〕§1.1/§4.1) and the self-check
refuses any proxy reason the proxy does not send.

Every file is scrubbed (scrub.py) and re-scanned before it is written; a
residue aborts (acceptance 5). Regenerate the static ones with::

    python3 -m tests.contract.record --out tests/contract/fixtures
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tests.contract import proxy_reasons, scrub
else:
    from . import proxy_reasons, scrub

FIXTURE_HEADERS = ("X-Woow-Proxy-Error", "X-Woow-Proxy-Precheck", "Retry-After", "Content-Type")

# Proxy classes stg cannot produce on demand (or that LLM-02 needs even when a
# live run skipped them): (file, kind, reason). Reasons are real codes.
STATIC_PROXY = [
    ("proxy_bad_request_authorization_not_allowed", "bad_request", "authorization_not_allowed"),
    ("proxy_too_large", "too_large", "body_too_large"),
    ("proxy_body_timeout", "body_timeout", "body_timeout"),
    ("proxy_auth", "auth", "auth"),
    ("proxy_policy_route", "policy", "route"),
    ("proxy_policy_manage_mode", "policy", "manage_mode"),
    ("proxy_denied_reveal_window", "denied", "reveal_window"),
    ("proxy_denied_reveal_name", "denied", "reveal_name"),
    ("proxy_denied_reveal_not_found", "denied", "reveal_not_found"),
    ("proxy_denied_reveal_token_mismatch", "denied", "reveal_token_mismatch"),
    ("proxy_denied_manage_target", "denied", "manage_target"),
    ("proxy_denied_manage_not_found", "denied", "manage_not_found"),
    ("proxy_budget", "budget", "hourly_cap"),
    ("proxy_busy_inflight", "busy", "inflight"),
    ("proxy_busy_replay_store", "busy", "replay_store"),
    ("proxy_config", "config", "ap_token_missing"),
    ("proxy_connect", "connect", "upstream_connect"),
    ("proxy_connect_deadline", "connect", "deadline"),
    ("proxy_reset", "reset", "upstream_reset"),
    ("proxy_timeout", "timeout", "upstream_timeout"),
    ("proxy_internal", "internal", "internal"),
]

# new-api answers stg cannot be made to give on demand (〔PROV〕§1.1, §4.1).
STATIC_NEWAPI = [
    ("newapi_500_empty_valkey_down", 500, None, {"_raw_empty": True}),
    ("newapi_500_panic", 500, {"error": {"type": "new_api_panic", "message": "Panic detected, error: runtime error"}}, {}),
    ("token_enable_expired_error", 200, {
        "success": False,
        "message": "Token has expired and cannot be enabled. Please modify the expiration time or set it to never expire",
        "message_key": "Token has expired and cannot be enabled. Please modify the expiration time or set it to never expire",
    }, {"_note": "needs DB status 3; a Redis deployment never writes it (C18), so stg cannot produce it"}),
    ("token_enable_exhausted_error", 200, {
        "success": False,
        "message": "Token quota is exhausted and cannot be enabled. Please modify the remaining quota or set it to unlimited",
        "message_key": "Token quota is exhausted and cannot be enabled. Please modify the remaining quota or set it to unlimited",
    }, {"_note": "needs DB status 4; a Redis deployment never writes it (C18), so stg cannot produce it"}),
]


def _clean_headers(headers):
    out = {}
    for name in FIXTURE_HEADERS:
        for k, v in (headers or {}).items():
            if k.lower() == name.lower():
                out[name] = v
    if any(k.lower() == "x-oneapi-request-id" for k in (headers or {})):
        out["X-Oneapi-Request-Id"] = "<request-id>"
    return out


def _plain(obj):
    """Secret wrappers -> plain JSON (the scrubber then redacts by name)."""
    return json.loads(json.dumps(obj))


def write_fixture(out_dir, name, obj):
    cleaned = scrub.scrub(_plain(obj))
    leak = scrub.residue(cleaned)
    if leak:
        raise SystemExit(f"refusing to write fixture {name}: secret residue {leak}")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".json")
    with open(path, "w") as f:
        json.dump(cleaned, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return path


class FixtureSink:
    """Live capture during a contract run (no-op when ``out_dir`` is None)."""

    def __init__(self, out_dir=None):
        self.out_dir = out_dir
        self.written = []

    @property
    def enabled(self):
        return bool(self.out_dir)

    def capture(self, name, resp, route=None):
        if not self.out_dir:
            return
        obj = {"_source": "live", "_route": route or "", "_http_status": resp.status,
               "_headers": _clean_headers(getattr(resp, "headers", {})),
               "_raw_empty": bool(getattr(resp, "raw_empty", False)),
               "body": getattr(resp, "body", None) or None}
        pe = getattr(resp, "proxy_error", None)
        if pe:
            obj["_proxy_error"] = pe
        self.written.append(write_fixture(self.out_dir, name, obj))

    def capture_transport(self, name, exc_type, after_send, route, executed=None):
        if not self.out_dir:
            return
        obj = {"_source": "live", "_transport": exc_type, "after_send": bool(after_send), "route": route}
        if executed is not None:
            obj["executed"] = bool(executed)
        self.written.append(write_fixture(self.out_dir, name, obj))


def _is_live(out_dir, name):
    try:
        with open(os.path.join(out_dir, name + ".json")) as f:
            return json.load(f).get("_source") == "live"
    except (OSError, ValueError):
        return False


def write_static(out_dir):
    """Write the contract-shape set; never replaces a live fixture of the same name."""
    paths = []
    real_write = write_fixture

    def write_fixture_unless_live(d, name, obj):  # noqa: N802
        if _is_live(d, name):
            return os.path.join(d, name + ".json") + " (live kept)"
        return real_write(d, name, obj)

    write = write_fixture_unless_live
    for name, kind, reason in STATIC_PROXY:
        if not proxy_reasons.known(kind, reason):
            raise SystemExit(f"{name}: {kind}/{reason} is not a reason the proxy sends")
        # app.py _error(): X-Woow-Proxy-Error + no-store; Retry-After only when a
        # pre-check relays new-api's 429 (connect *_precheck_http_429).
        headers = {"X-Woow-Proxy-Error": kind, "Content-Type": "application/json"}
        body = {"success": False, "message": f"newapi-proxy: {kind}", "proxy_error": kind, "reason": reason}
        paths.append(write(out_dir, name, {
            "_source": "contract-shape", "_route": "", "_http_status": proxy_reasons.STATUS[kind],
            "_headers": headers, "_raw_empty": False, "_proxy_error": kind, "body": body}))
    # the send-then-drop case LLM-02 must classify Unknown (X09 overwrites it with a live one)
    paths.append(write(out_dir, "transport_send_then_drop_manage", {
        "_source": "contract-shape", "_transport": "RemoteDisconnected", "after_send": True, "route": "manage"}))
    for name, status, body, extra in STATIC_NEWAPI:
        obj = {"_source": "contract-shape", "_route": "", "_http_status": status,
               "_headers": {} if body is None else {"Content-Type": "application/json"},
               "_raw_empty": body is None, "body": body}
        obj.update(extra)
        paths.append(write(out_dir, name, obj))
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
    args = ap.parse_args(argv)
    for p in write_static(args.out):
        print(f"wrote {p}")
    print("static fixtures done; live ones come from a contract run with NEWAPI_CONTRACT_RECORD_DIR")


if __name__ == "__main__":
    main()
