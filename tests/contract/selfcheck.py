"""Offline self-checks for the contract suite — run in CI with plain python3.

CI (.gitea/workflows/ci.yml) cannot reach new-api-stg, so it cannot run the
C/F/X tests. It runs this instead, which proves the suite itself is sound:

  1. the signing copy still matches the committed golden vector (so a proxy
     signing change forces a visible update here, in the same PR);
  2. the scrubber removes every secret shape it claims to (acceptance 5);
  3. every id C01-C27, F01-F12, X01-X09 is pinned by at least one collected
     test (no id silently dropped);
  4. config parses with an empty environment and reports every target absent.

Run: ``python3 tests/contract/selfcheck.py`` (exit 0 = ok). ``test_offline.py``
runs the same checks under pytest for the stg runs.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from tests.contract import scrub, signing
    from tests.contract.config import Config
    from tests.contract.ids import ALL_IDS, CONTRACT_IDS
else:
    from . import scrub, signing
    from .config import Config
    from .ids import ALL_IDS, CONTRACT_IDS

# Golden HMAC vector. Inputs are fixed; the digest is recomputed only when the
# proxy's signing.py scheme deliberately changes (then bump the proxy image in
# the same PR). See extra/newapi-proxy/newapi_proxy/signing.py.
GOLDEN_SECRET = b"contract_golden_secret_0123456789abcdef"
GOLDEN = dict(
    method="GET",
    path="/api/user/search",
    query=[("p", "1"), ("keyword", "wct0000000001")],
    body=b"",
    ts="1760000000",
    nonce="nonce_golden_example0",
    kid="web-golden",
    identity="ap",
    client_ip="",
)
GOLDEN_CANONICAL_QUERY = "keyword=wct0000000001&p=1"
GOLDEN_SIG = "b0346af974a01d655d2d5397bb9dd5ee468504f061f0c0f42c3bc60c915f3b29"


def check_signing():
    cq = signing.canonical_query(GOLDEN["query"])
    assert cq == GOLDEN_CANONICAL_QUERY, f"canonical_query changed: {cq!r}"
    msg = signing.string_to_sign(
        GOLDEN["method"], GOLDEN["path"], cq, GOLDEN["body"],
        GOLDEN["ts"], GOLDEN["nonce"], GOLDEN["kid"], GOLDEN["identity"], GOLDEN["client_ip"],
    )
    sig = signing.sign(GOLDEN_SECRET, msg)
    assert sig == GOLDEN_SIG, f"signing scheme changed: {sig} != {GOLDEN_SIG}"
    kid, secret = signing.first_key(" web-x:" + "a" * 40 + " , cron-y:" + "b" * 40)
    assert kid == "web-x" and secret == b"a" * 40, "first_key parse"


def check_scrub():
    sample = {
        "success": True,
        "data": {
            "token": "nap_" + "A" * 43,
            "item": {"token_ref": "f" * 64, "token_hint": "ab12", "last_used_ip": "198.18.0.1"},
            "key": "sk-" + "B" * 40,
            "nested": ["Bearer eyJabc.eyJdef.sigsigsig", {"password": "hunter2hunter2hunter2hunter2hunter2"}],
            "note": "a sentence mentioning sk-" + "C" * 30 + " inline",
        },
    }
    assert scrub.residue(sample), "residue must see the planted secrets first"
    cleaned = scrub.scrub(sample)
    left = scrub.residue(cleaned)
    assert not left, f"scrub left secrets behind: {left}"
    # shape and non-secret values survive
    assert cleaned["success"] is True
    assert cleaned["data"]["item"]["token_hint"] in ("ab12", scrub.REDACTED)
    assert cleaned["data"]["item"]["last_used_ip"] == "198.18.0.1"


def _collected_ids():
    """Every contract_id("..") literal across the test files."""
    ids = set()
    pat = re.compile(r'contract_id\(\s*"([CFX]\d\d)"\s*\)')
    for name in os.listdir(HERE):
        if name.startswith("test_") and name.endswith(".py"):
            with open(os.path.join(HERE, name)) as f:
                ids.update(pat.findall(f.read()))
    return ids


def check_ids_complete():
    registry = set(CONTRACT_IDS)
    assert registry == set(ALL_IDS), "CONTRACT_IDS and ALL_IDS disagree"
    collected = _collected_ids()
    missing = registry - collected
    assert not missing, f"no test pins these ids: {sorted(missing)}"
    stray = collected - registry
    assert not stray, f"test pins unknown ids: {sorted(stray)}"


def check_config_empty_env():
    c = Config(environ={})
    for flag in ("have_newapi", "have_relay", "have_proxy", "have_proxy_cron",
                 "have_proxy_web", "have_psql", "have_paid_model", "have_admin_creds"):
        assert getattr(c, flag) is False, f"{flag} should be False with empty env"
    assert c.user_prefix == "wct" and c.free_model == "qwen3-4b-cluster1"


CHECKS = (check_signing, check_scrub, check_ids_complete, check_config_empty_env)


def main():
    failures = []
    for check in CHECKS:
        try:
            check()
            print(f"ok   {check.__name__}")
        except AssertionError as e:
            failures.append((check.__name__, str(e)))
            print(f"FAIL {check.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} self-check(s) failed")
        return 1
    print(f"\nall {len(CHECKS)} self-checks passed; {len(ALL_IDS)} contract ids registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
