"""The per-id contract report: aggregate, merge staged runs, check the gate.

Stdlib only: conftest.py writes it, ``gate_check.py`` (CI) and the operator
read it. Usage::

    python3 -m tests.contract.report merge OUT RUN1.json RUN2.json ...
    python3 -m tests.contract.report check REPORT.json      # gate verdict, exit 0/1

Id status (worst wins): ``fail`` > ``partial`` > ``skip`` > ``pass``.
``partial`` = some of the id's tests passed and others skipped, or a test
marked itself partial (it checked only part of the contract). ``skip`` = every
test of the id skipped; ``missing`` = no test ran at all. An id in
``ids.KNOWN_PARTIAL`` that is partial for exactly that documented reason is
``known_partial`` and does not fail the gate.

A gate run needs ``fail == skip == partial == missing == 0`` after merging the
staged runs (main, C23 CT exhaust, X06 upstream-down, the disruptive window),
the env block naming the pinned digests, no test user left behind, and
verify-settings exit 0 after any settings flip.
"""
from __future__ import annotations

import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tests.contract import scrub
    from tests.contract.ids import CONTRACT_IDS, KNOWN_PARTIAL
else:
    from . import scrub
    from .ids import CONTRACT_IDS, KNOWN_PARTIAL

SCHEMA = 2
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS_DIR = os.path.join(REPO, "tests", "contract", "reports")
_RANK = {"fail": 3, "pass": 2, "skip": 1}


# --------------------------------------------------------------------------- #
#  aggregation                                                                #
# --------------------------------------------------------------------------- #
def id_status(tests):
    """Status of one id from its test records (dicts with outcome/partial)."""
    if not tests:
        return "missing"
    outcomes = [t["outcome"] for t in tests]
    if "fail" in outcomes:
        return "fail"
    if all(o == "skip" for o in outcomes):
        return "skip"
    if "skip" in outcomes or any(t.get("partial") for t in tests):
        return "partial"
    return "pass"


def aggregate(tests):
    """{nodeid: record} -> (ids, summary)."""
    by_id = {cid: [] for cid in CONTRACT_IDS}
    for rec in tests.values():
        if rec.get("id") in by_id:
            by_id[rec["id"]].append(rec)
    ids = {}
    summary = {"pass": 0, "fail": 0, "skip": 0, "partial": 0, "known_partial": 0, "missing": 0}
    for cid in sorted(CONTRACT_IDS):
        recs = by_id[cid]
        status = id_status(recs)
        notes = sorted({t["partial"] for t in recs if t.get("partial")})
        skipped = sorted({t.get("skip_reason") or "" for t in recs if t["outcome"] == "skip"} - {""})
        if status == "partial" and cid in KNOWN_PARTIAL and not skipped:
            status = "known_partial"
        summary[status] += 1
        ids[cid] = {"status": status, "tests": len(recs), "notes": notes, "skipped": skipped}
    return ids, summary


# --------------------------------------------------------------------------- #
#  merge                                                                      #
# --------------------------------------------------------------------------- #
def _better(a, b):
    """The record to keep when two runs ran the same test."""
    if a["outcome"] == "fail" or b["outcome"] == "fail":
        return a if a["outcome"] == "fail" else b
    if _RANK[a["outcome"]] != _RANK[b["outcome"]]:
        return a if _RANK[a["outcome"]] > _RANK[b["outcome"]] else b
    if a["outcome"] == "pass" and bool(a.get("partial")) != bool(b.get("partial")):
        return b if a.get("partial") else a
    return b


def merge(reports):
    if not reports:
        raise ValueError("nothing to merge")
    envs = {json.dumps(r.get("env", {}).get("declared"), sort_keys=True) for r in reports}
    if len(envs) != 1:
        raise ValueError("the runs were against different pinned images (env.declared differs)")
    out = json.loads(json.dumps(reports[0]))
    tests = {}
    for r in reports:
        for nodeid, rec in (r.get("tests") or {}).items():
            tests[nodeid] = _better(tests[nodeid], rec) if nodeid in tests else rec
    out["tests"] = tests
    out["ids"], out["summary"] = aggregate(tests)
    out["runs"] = [r.get("run", {}) for r in reports]
    out["run"] = {
        "invocation": "merged",
        "wall_seconds": round(sum((r.get("run") or {}).get("wall_seconds") or 0 for r in reports), 1),
        "started": min((r.get("run") or {}).get("started") or 0 for r in reports),
        "finished": max((r.get("run") or {}).get("finished") or 0 for r in reports),
    }
    metrics, notes = {}, {}
    cleanup = {"created": 0, "deleted": 0, "left": [], "soft_deleted": [], "errors": []}
    flipped, verify_exit = [], None
    for r in reports:
        metrics.update(r.get("metrics") or {})
        notes.update(r.get("notes") or {})
        c = r.get("cleanup") or {}
        cleanup["created"] += c.get("created", 0)
        cleanup["deleted"] += c.get("deleted", 0)
        cleanup["left"] = sorted(set(cleanup["left"]) | set(c.get("left", [])))
        cleanup["soft_deleted"] = sorted(set(cleanup["soft_deleted"]) | set(c.get("soft_deleted", [])))
        cleanup["errors"] += c.get("errors", [])
        s = r.get("settings") or {}
        flipped += s.get("flipped", [])
        if s.get("verify_settings_exit") is not None:
            verify_exit = max(verify_exit or 0, s["verify_settings_exit"])
        elif s.get("flipped"):
            verify_exit = max(verify_exit or 0, 99)
    out["metrics"], out["notes"], out["cleanup"] = metrics, notes, cleanup
    out["settings"] = {"flipped": flipped, "verify_settings_exit": verify_exit}
    return out


# --------------------------------------------------------------------------- #
#  pinned images                                                              #
# --------------------------------------------------------------------------- #
def yaml_scalar(text, section, sub, key):
    """``section.sub.key`` from simple YAML text (no YAML library in CI)."""
    cur_section = cur_sub = None
    for line in (text or "").splitlines():
        body = line.split("#", 1)[0].rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        k = body.strip().split(":", 1)[0]
        if indent == 0:
            cur_section, cur_sub = k, None
        elif cur_section == section and indent == 2:
            cur_sub = k
        elif cur_section == section and cur_sub == sub and indent == 4 and k == key:
            return body.split(":", 1)[1].strip().strip("'\"") or None
    return None


def image_pin(text, section):
    return {"tag": yaml_scalar(text, section, "image", "tag"), "digest": yaml_scalar(text, section, "image", "digest")}


def _read(path):
    with open(path) as f:
        return f.read()


def pins(repo=REPO):
    """The images a gate report must name: chart new-api + stg proxy."""
    return {
        "newapi": image_pin(_read(os.path.join(repo, "chart", "values.yaml")), "newapi"),
        "proxy": image_pin(_read(os.path.join(repo, "deploy", "woow-k3s-stg.yaml")), "proxy"),
    }


def report_name(p):
    return f"{p['newapi']['tag']}_{(p['proxy']['tag'] or 'none')[:12]}.json"


# --------------------------------------------------------------------------- #
#  gate verdict                                                               #
# --------------------------------------------------------------------------- #
def gate_problems(report, expected_pins=None):
    """Why this report does not open the upgrade gate ([] = it does)."""
    out = []
    if report.get("schema") != SCHEMA:
        out.append(f"schema {report.get('schema')!r}, expected {SCHEMA}")
    s = report.get("summary") or {}
    for k in ("fail", "skip", "partial", "missing"):
        if s.get(k):
            bad = sorted(cid for cid, v in (report.get("ids") or {}).items() if v.get("status") == k)
            out.append(f"{s[k]} id(s) {k}: {', '.join(bad)}")
    env = report.get("env") or {}
    if env.get("target") != "stg":
        out.append("env.target is not stg")
    if expected_pins is not None and env.get("declared") != expected_pins:
        out.append("env.declared does not match chart/values.yaml newapi.image and deploy/woow-k3s-stg.yaml proxy.image")
    live = env.get("live") or {}
    declared = env.get("declared") or {}
    for part, field in (("newapi", "newapi_images"), ("proxy", "proxy_images")):
        dg = (declared.get(part) or {}).get("digest")
        if not dg or dg not in (live.get(field) or ""):
            out.append(f"live {part} image on new-api-stg is not the declared digest")
    c = report.get("cleanup") or {}
    if c.get("left"):
        out.append(f"test users left on stg: {', '.join(c['left'])}")
    if c.get("errors"):
        out.append(f"{len(c['errors'])} cleanup error(s)")
    st = report.get("settings") or {}
    if st.get("flipped") and st.get("verify_settings_exit") != 0:
        out.append("settings were flipped but verify-settings did not exit 0")
    leak = scrub.residue(report)
    if leak:
        out.append(f"secret residue in the report: {leak}")
    return out


def _main(argv):
    if len(argv) >= 3 and argv[0] == "merge":
        out_path, inputs = argv[1], argv[2:]
        reports = []
        for p in inputs:
            with open(p) as f:
                reports.append(json.load(f))
        merged = merge(reports)
        with open(out_path, "w") as f:
            json.dump(merged, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        print(f"merged {len(inputs)} run(s) -> {out_path}: {merged['summary']}")
        return 0
    if len(argv) == 2 and argv[0] == "check":
        with open(argv[1]) as f:
            rep = json.load(f)
        problems = gate_problems(rep, pins())
        for p in problems:
            print(f"FAIL {p}")
        print("gate: " + ("open" if not problems else f"closed ({len(problems)} problem(s))"))
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
