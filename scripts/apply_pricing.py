#!/usr/bin/env python3
"""Merge deploy/pricing/pricing-plan.json into a new-api's ModelRatio,
CompletionRatio and CacheRatio options (ratio 1 = US$2 per 1M tokens; model
ratios equal LiteLLM cost, markup lives in GroupRatio).

Usage: apply_pricing.py --base URL --creds FILE [--plan deploy/pricing/pricing-plan.json]
Writes a backup of the previous option values next to the plan unless --no-backup.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newapi_admin as na  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--creds", required=True)
ap.add_argument("--plan", default=os.path.join(os.path.dirname(__file__), "..", "deploy", "pricing", "pricing-plan.json"))
ap.add_argument("--backup-dir", default=os.path.expanduser("~/.config/newapi-backups"))
a = ap.parse_args()

plan = json.load(open(a.plan))
c = na.Client(a.base, na.read_creds(a.creds))
opts = {o["key"]: o["value"] for o in c.req("GET", "/api/option/")["data"]}
os.makedirs(a.backup_dir, mode=0o700, exist_ok=True)
host = a.base.split("//", 1)[-1].replace(":", "_").replace("/", "_")
with open(os.path.join(a.backup_dir, f"pricing-before-{host}.json"), "w") as f:
    json.dump({k: opts.get(k) for k in ("ModelRatio", "CompletionRatio", "CacheRatio")}, f, ensure_ascii=False)
failed = 0
for k in ("ModelRatio", "CompletionRatio", "CacheRatio"):
    cur = json.loads(opts.get(k) or "{}")
    cur.update(plan[k])
    d = c.req("PUT", "/api/option/", {"key": k, "value": json.dumps(cur, ensure_ascii=False)})
    failed += not d.get("success")
    print(f"{k}: {len(plan[k])} from plan, {len(cur)} total -> {d.get('success')} {d.get('message') or ''}")
sys.exit(1 if failed else 0)
