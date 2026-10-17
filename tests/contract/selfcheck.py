"""Offline self-checks for the contract suite — run in CI with plain python3.

CI (.gitea/workflows/ci.yml) cannot reach new-api-stg, so it cannot run the
C/F/X tests. It runs this instead (stdlib only, no pytest), which proves the
suite itself is sound:

  1. the signing copy matches the committed golden vector. The vector ties
     this copy to itself; it matches the proxy's signer today, but a signing
     change in the platform repo does NOT turn CI red here — only a live X04
     run notices. ``NEWAPI_CONTRACT_PROXY_SRC=<woow_paas_platform>/extra/newapi-proxy``
     also loads the proxy's own ``signing.py`` and checks it against the same
     vector (and its reason codes against proxy_reasons.py);
  2. the scrubber removes every secret shape it claims to, and every committed
     fixture passes the scrubber's residue scan *and* the literal acceptance
     grep (``sk-``, ``nap_``, a ``password`` value) with a known proxy reason;
  3. every id C01-C27, F01-F12, X01-X09 is pinned, and every ``test_*``
     function carries exactly one ``contract_id`` (or ``offline``) marker —
     what ``pytest --collect-only`` would show, without pytest;
  4. config, redaction, the CT budget, the settings guard and the report
     aggregation behave (fakes, no network);
  5. any committed gate report under ``reports/`` is green for its pins.

Run: ``python3 tests/contract/selfcheck.py`` (exit 0 = ok). ``test_offline.py``
runs the same checks under pytest.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from tests.contract import budget, proxy_reasons, redact, report, scrub, signing
    from tests.contract.config import Config
    from tests.contract.ids import ALL_IDS, CONTRACT_IDS, KNOWN_PARTIAL
else:
    from . import budget, proxy_reasons, redact, report, scrub, signing
    from .config import Config
    from .ids import ALL_IDS, CONTRACT_IDS, KNOWN_PARTIAL

FIXTURES = os.path.join(HERE, "fixtures")
REPORTS = os.path.join(HERE, "reports")

# Golden HMAC vector. Inputs are fixed; the digest changes only when the
# signing scheme deliberately changes (then the proxy image changes too).
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

# The acceptance-5 literal grep, as a reviewer would run it on the fixture tree.
LITERAL_GREPS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"nap_[A-Za-z0-9]"),
    re.compile(r'"password"\s*:\s*"(?!<redacted>")[^"]+"'),
)


def _golden_sig(mod):
    cq = mod.canonical_query(GOLDEN["query"])
    msg = mod.string_to_sign(GOLDEN["method"], GOLDEN["path"], cq, GOLDEN["body"], GOLDEN["ts"],
                             GOLDEN["nonce"], GOLDEN["kid"], GOLDEN["identity"], GOLDEN["client_ip"])
    return cq, mod.sign(GOLDEN_SECRET, msg) if hasattr(mod, "sign") else None


def check_signing():
    cq, sig = _golden_sig(signing)
    assert cq == GOLDEN_CANONICAL_QUERY, f"canonical_query changed: {cq!r}"
    assert sig == GOLDEN_SIG, f"signing scheme changed: {sig} != {GOLDEN_SIG}"
    kid, secret = signing.first_key(" web-x:" + "a" * 40 + " , cron-y:" + "b" * 40)
    assert kid == "web-x" and secret == b"a" * 40, "first_key parse"
    src = os.environ.get("NEWAPI_CONTRACT_PROXY_SRC")
    if src:
        path = os.path.join(src, "newapi_proxy", "signing.py")
        spec = importlib.util.spec_from_file_location("_proxy_signing", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import hashlib
        import hmac

        cq2 = mod.canonical_query(GOLDEN["query"])
        msg = mod.string_to_sign(GOLDEN["method"], GOLDEN["path"], cq2, GOLDEN["body"], GOLDEN["ts"],
                                 GOLDEN["nonce"], GOLDEN["kid"], GOLDEN["identity"], GOLDEN["client_ip"])
        sig2 = hmac.new(GOLDEN_SECRET, msg, hashlib.sha256).hexdigest()
        assert sig2 == GOLDEN_SIG, f"the proxy's signing.py ({path}) disagrees with the golden vector"
        pairs, policy, canonical = proxy_reasons.source_literals(src)
        unknown = sorted(f"{k}/{r}" for k, r in pairs if k != "auth" and not proxy_reasons.known(k, r))
        unknown += sorted(f"policy/{p}" for p in policy if p not in proxy_reasons.REASONS["policy"])
        unknown += sorted(f"bad_request/{c}" for c in canonical if c not in proxy_reasons.REASONS["bad_request"])
        assert not unknown, f"proxy_reasons.py lacks codes the proxy source has: {unknown}"


def check_scrub():
    sample = {
        "success": True,
        "data": {
            "token": "nap_" + "A" * 43,
            "item": {"token_ref": "f" * 64, "token_hint": "ab12", "last_used_ip": "198.18.0.1"},
            "key": "sk-" + "B" * 40,
            "nested": ["Bearer eyJabc.eyJdef.sigsigsig", {"password": "hunter2hunter2hunter2hunter2hunter2"}],
            "note": "a sentence mentioning sk-" + "C" * 30 + " inline",
            "keys": {"12": "D" * 48},
        },
    }
    assert scrub.residue(sample), "residue must see the planted secrets first"
    cleaned = scrub.scrub(sample)
    left = scrub.residue(cleaned)
    assert not left, f"scrub left secrets behind: {left}"
    assert cleaned["success"] is True
    assert cleaned["data"]["item"]["last_used_ip"] == "198.18.0.1"
    assert cleaned["data"]["keys"]["12"] == scrub.REDACTED, "batch-reveal keys survived the scrubber"


def _fixture_files():
    return sorted(os.path.join(FIXTURES, n) for n in os.listdir(FIXTURES) if n.endswith(".json"))


def check_fixtures():
    files = _fixture_files()
    assert files, "no committed fixtures"
    for path in files:
        name = os.path.basename(path)
        with open(path) as f:
            text = f.read()
        obj = json.loads(text)
        leak = scrub.residue(obj)
        assert not leak, f"{name}: secret residue {leak}"
        for rx in LITERAL_GREPS:
            assert not rx.search(text), f"{name}: matches the acceptance grep {rx.pattern!r}"
        assert obj.get("_source") in ("live", "contract-shape"), f"{name}: _source"
        if "_transport" in obj:
            assert {"after_send", "route"} <= set(obj), f"{name}: transport fixture fields"
            continue
        assert isinstance(obj.get("_http_status"), int) and isinstance(obj.get("_headers"), dict), \
            f"{name}: _http_status / _headers"
        body = obj.get("body") or {}
        kind = obj.get("_proxy_error") or (obj["_headers"].get("X-Woow-Proxy-Error"))
        if kind:
            assert body.get("proxy_error") == kind, f"{name}: body.proxy_error != {kind}"
            assert proxy_reasons.known(kind, body.get("reason")), \
                f"{name}: reason {body.get('reason')!r} is not one the proxy sends for {kind}"
            assert obj["_http_status"] == proxy_reasons.STATUS[kind], f"{name}: status for {kind}"


def _test_functions():
    for name in sorted(os.listdir(HERE)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(HERE, name)) as f:
            tree = ast.parse(f.read(), filename=name)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                ids, offline = [], False
                for dec in node.decorator_list:
                    src = ast.unparse(dec)
                    m = re.fullmatch(r"""pytest\.mark\.contract_id\(['"]([CFX]\d\d)['"]\)""", src)
                    if m:
                        ids.append(m.group(1))
                    offline = offline or src == "pytest.mark.offline"
                yield name, node.name, ids, offline


def check_ids_complete():
    registry = set(CONTRACT_IDS)
    assert registry == set(ALL_IDS), "CONTRACT_IDS and ALL_IDS disagree"
    assert set(KNOWN_PARTIAL) <= registry, "KNOWN_PARTIAL names an unknown id"
    collected = set()
    for fname, func, ids, offline in _test_functions():
        assert len(ids) + offline == 1, f"{fname}::{func} needs exactly one contract_id (or offline) marker"
        collected.update(ids)
    missing = registry - collected
    assert not missing, f"no test pins these ids: {sorted(missing)}"
    stray = collected - registry
    assert not stray, f"tests pin unknown ids: {sorted(stray)}"


def check_config():
    c = Config(environ={})
    for flag in ("live", "have_kube", "have_newapi", "have_proxy_cron", "have_proxy_web", "have_paid_model",
                 "have_admin_creds", "have_settings_pat"):
        assert getattr(c, flag) is False, f"{flag} should be False with an empty env"
    assert c.user_prefix == "wct" and c.proxy_prefix == "wsct" and c.free_model == "qwen3-4b-cluster1"
    assert c.problems() == [], f"empty env problems: {c.problems()}"
    old = Config(environ={"NEWAPI_CONTRACT_PSQL": "kubectl -n new-api exec x -- psql"})
    assert any("NEWAPI_CONTRACT_PSQL" in p for p in old.problems()), "a removed env command must be refused"
    bad = Config(environ={"NEWAPI_CONTRACT_USER_PREFIX": "wp", "NEWAPI_CONTRACT_PROXY_PREFIX": "wp12"})
    assert len(bad.problems()) >= 2, "tenant-like prefixes must be refused"
    from tests.contract.config import SUITE_TENANT_IPS  # noqa: PLC0415

    assert SUITE_TENANT_IPS[0] == "198.19.255.216" and SUITE_TENANT_IPS[-1] == "198.19.255.255"


def check_redaction():
    s = redact.Secret("nap_" + "Z" * 43)
    for shown in (repr(s), str(s), f"{s}", "%s" % s, repr({"t": s})):
        assert "Z" * 10 not in shown, "a Secret leaked through repr/str/format"
    assert ("Bearer " + s).endswith("Z" * 43) and json.dumps({"t": s}).count("Z") == 43, "wire value lost"
    wrapped = redact.wrap_secrets({"data": {"access_token": "eyJ" + "x" * 30, "items": [{"key": "Opt", "value": 1}],
                                            "keys": {"7": "k" * 48}}})
    assert isinstance(wrapped["data"]["access_token"], redact.Secret)
    assert not isinstance(wrapped["data"]["items"][0]["key"], redact.Secret), "option rows keep their key"
    assert isinstance(wrapped["data"]["keys"]["7"], redact.Secret)
    try:
        redact.assert_secret_shape(redact.Secret("nap_" + "Q" * 40), prefix="nap_", length=47, what="t")
    except AssertionError as e:
        assert "Q" * 10 not in str(e), "assert_secret_shape put the value in its message"
    else:
        raise AssertionError("a 44-char token passed a 47-char shape check")


def check_budget():
    state = {"count": 18, "ttl": 30}
    slept = []

    def counter(key):
        return state["count"], state["ttl"]

    def sleep(s):
        slept.append(s)
        state["count"], state["ttl"] = 0, 1200

    b = budget.CtBudget(counter, reserve=2, sleep=sleep, max_wait=100)
    b.take("k")  # 18 + 1 > 18 -> waits 32 s, then fits
    assert slept == [32] and b.spent == {"k": 1} and b.waits == 1, f"budget wait {slept} {b.spent}"
    state.update(count=19, ttl=1100)
    try:
        b.take("k")
    except budget.BudgetExceeded:
        pass
    else:
        raise AssertionError("a wait longer than max_wait must raise")
    assert budget.is_ct_route("POST", "/api/token/12/key") and budget.is_ct_route("POST", "/api/user/login")
    assert budget.is_ct_route("DELETE", "/api/user/access_tokens/5") and budget.is_ct_route("POST", "/api/verify")
    assert not budget.is_ct_route("GET", "/api/user/access_tokens") and not budget.is_ct_route("GET", "/api/verify/methods")
    assert not budget.is_ct_route("DELETE", "/api/user/sessions/x") and not budget.is_ct_route("POST", "/api/token/")


class _FakeOptionClient:
    def __init__(self, live, ok=True):
        self.live, self.ok, self.puts = dict(live), ok, []

    def get(self, path):
        class R:
            pass

        r = R()
        r.status, r.code = (200, None) if self.ok else (403, "ACCESS_TOKEN_SCOPE_DENIED")
        r.success = self.ok
        r.data = [{"key": k, "value": v} for k, v in self.live.items()] if self.ok else None
        return r

    def put(self, path, body):
        from tests.contract import settings as st  # noqa: PLC0415

        self.puts.append(body)
        self.live[body["key"]] = st.NA.encode(json.loads(body["value"]) if isinstance(body["value"], str)
                                              and body["value"][:1] in "[{" else body["value"])

        class R:
            success = True
            status = 200
            message_key = None
            code = None

        return R()


def check_settings_guard():
    from tests.contract import settings as st  # noqa: PLC0415

    declared = st.NA.load_layers(list(st.SETTINGS_FILES))
    live = {k: st.NA.encode(v) for k, v in declared.items()}
    quiet = {"notify": lambda m: None}
    # GET fails -> refuse before any PUT
    c = _FakeOptionClient(live, ok=False)
    sa = st.SettingsAdmin(c, **quiet)
    try:
        with sa.temporary("quota_setting.enable_free_model_pre_consume", True):
            pass
    except st.SettingsRefused:
        pass
    else:
        raise AssertionError("flipped although GET /api/option/ failed")
    assert not c.puts, "a PUT went out after a failed GET"
    # drifted -> refuse
    drift = dict(live, GroupRatio=json.dumps({"default": 1.3, "free": 0}))
    c = _FakeOptionClient(drift)
    try:
        with st.SettingsAdmin(c, **quiet).temporary_json_member("GroupRatio", "free", 1):
            pass
    except st.SettingsRefused:
        pass
    else:
        raise AssertionError("flipped a drifted option")
    assert not c.puts
    # clean -> flip, then the declared value comes back (never {})
    c = _FakeOptionClient(live)
    sa = st.SettingsAdmin(c, **quiet)
    with sa.temporary_json_member("GroupRatio", "free", 1):
        assert json.loads(c.live["GroupRatio"])["free"] == 1
        assert json.loads(c.live["GroupRatio"])["default"] == 1.3, "the member flip dropped the other groups"
    assert st.NA.same(declared["GroupRatio"], c.live["GroupRatio"]), "not restored to the declared value"
    assert sa.flipped == ["GroupRatio"]


def check_report():
    p = lambda o, partial=None: {"id": "C01", "outcome": o, "partial": partial}  # noqa: E731
    assert report.id_status([p("pass"), p("skip")]) == "partial", "pass + skip must be partial"
    assert report.id_status([p("skip"), p("pass")]) == "partial", "order must not matter"
    assert report.id_status([p("skip")]) == "skip" and report.id_status([]) == "missing"
    assert report.id_status([p("pass"), p("fail"), p("skip")]) == "fail"
    assert report.id_status([p("pass", "half")]) == "partial"
    tests = {"a": dict(p("pass"), id="F06", partial=KNOWN_PARTIAL["F06"]), "b": dict(p("pass"), id="C01")}
    ids, summary = report.aggregate(tests)
    assert ids["F06"]["status"] == "known_partial" and ids["C01"]["status"] == "pass"
    assert summary["missing"] == len(CONTRACT_IDS) - 2
    run1 = {"schema": 2, "env": {"declared": {"x": 1}}, "tests": {"t1": dict(p("skip"), id="C23"),
                                                                  "t2": dict(p("pass"), id="C23")}}
    run2 = {"schema": 2, "env": {"declared": {"x": 1}}, "tests": {"t1": dict(p("pass"), id="C23")}}
    merged = report.merge([run1, run2])
    assert merged["ids"]["C23"]["status"] == "pass", "a staged pass must fill the main run's skip"
    run3 = {"schema": 2, "env": {"declared": {"x": 2}}, "tests": {}}
    try:
        report.merge([run1, run3])
    except ValueError:
        pass
    else:
        raise AssertionError("merged runs against different images")
    pins = report.pins()
    assert pins["newapi"]["tag"] and pins["newapi"]["digest"].startswith("sha256:"), "chart newapi pin"
    assert re.fullmatch(r"[0-9a-f]{40}", pins["proxy"]["tag"] or ""), "stg proxy pin"


def check_committed_reports():
    if not os.path.isdir(REPORTS):
        return
    pins = report.pins()
    for name in sorted(os.listdir(REPORTS)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(REPORTS, name)) as f:
            rep = json.load(f)
        declared = (rep.get("env") or {}).get("declared")
        if declared == pins:
            problems = report.gate_problems(rep, pins)
            assert not problems, f"reports/{name}: {problems}"
        assert not scrub.residue(rep), f"reports/{name}: secret residue"


CHECKS = (check_signing, check_scrub, check_fixtures, check_ids_complete, check_config, check_redaction,
          check_budget, check_settings_guard, check_report, check_committed_reports)


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
