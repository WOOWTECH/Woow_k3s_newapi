"""CI: an image bump needs a green stg contract report for exactly those digests.

When a PR changes ``chart/values.yaml`` ``newapi.image`` or the proxy image in
``deploy/woow-k3s-stg.yaml`` / ``deploy/woow-k3s.yaml``, it must also commit
``tests/contract/reports/<newapi tag>_<proxy sha[:12]>.json``: the merged
report of the stg gate run (docs/OPERATIONS.md 〈升版〉) whose env block names
those exact tags and digests, with fail = skip = partial = missing = 0, no test
user left behind, verify-settings exit 0 after the flips and no secret
residue (report.gate_problems). Nothing changed -> nothing required.

    python3 tests/contract/gate_check.py --base-ref origin/main   # exit 0 / 1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tests.contract import report
else:
    from . import report

FILES = {"chart": "chart/values.yaml", "stg": "deploy/woow-k3s-stg.yaml", "prod": "deploy/woow-k3s.yaml"}


def _at(ref, path):
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=report.REPO, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _now(path):
    with open(os.path.join(report.REPO, path)) as f:
        return f.read()


def required(base_ref):
    """[(why, expected_pins)] the PR must carry a green report for."""
    old = {k: _at(base_ref, p) for k, p in FILES.items()}
    new = {k: _now(p) for k, p in FILES.items()}
    newapi_old, newapi_new = report.image_pin(old["chart"], "newapi"), report.image_pin(new["chart"], "newapi")
    stg_old, stg_new = report.image_pin(old["stg"], "proxy"), report.image_pin(new["stg"], "proxy")
    prod_old, prod_new = report.image_pin(old["prod"], "proxy"), report.image_pin(new["prod"], "proxy")
    out = []
    if newapi_old != newapi_new or stg_old != stg_new:
        out.append(("chart newapi.image / stg proxy.image changed", {"newapi": newapi_new, "proxy": stg_new}))
    if prod_old != prod_new and prod_new.get("tag"):
        out.append(("prod proxy.image changed", {"newapi": newapi_new, "proxy": prod_new}))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-ref", required=True)
    a = ap.parse_args(argv)
    if subprocess.run(["git", "rev-parse", "--verify", "-q", a.base_ref], cwd=report.REPO,
                      capture_output=True).returncode != 0:
        print(f"gate-check: base ref {a.base_ref} not found (fetch it with full history)")
        return 1
    needs = required(a.base_ref)
    if not needs:
        print("gate-check: no image pin changed; no contract report required")
        return 0
    failed = 0
    for why, want in needs:
        name = report.report_name(want)
        path = os.path.join(report.REPORTS_DIR, name)
        if not os.path.exists(path):
            print(f"FAIL {why}: commit tests/contract/reports/{name} (the merged stg gate report for these digests)")
            failed += 1
            continue
        with open(path) as f:
            problems = report.gate_problems(json.load(f), want)
        for p in problems:
            print(f"FAIL reports/{name}: {p}")
        failed += bool(problems)
        if not problems:
            print(f"ok   {why}: reports/{name} is green for these digests")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
