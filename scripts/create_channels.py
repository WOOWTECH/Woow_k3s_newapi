#!/usr/bin/env python3
"""Create the vLLM-type channels that point a new-api at LiteLLM svc203.
The LiteLLM key is read from stdin (pipe it straight from the LiteLLM pod;
it never touches disk here).

Usage: ... | create_channels.py --base URL --creds FILE --label stg|prod [--with-internal]
Channels (DESIGN §7.2): paid (group default, plan["api"]), free trial
(group free, plan["local"] Qwen, max_tokens capped by param_override) and
optionally internal (group internal, plan["internal"]).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newapi_admin as na  # noqa: E402

LITELLM = "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000"
ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--creds", required=True)
ap.add_argument("--label", required=True)
ap.add_argument("--key-alias", required=True)
ap.add_argument("--with-internal", action="store_true")
ap.add_argument("--free-max-tokens", type=int, default=2048)
ap.add_argument("--plan", default=os.path.join(os.path.dirname(__file__), "..", "deploy", "pricing", "pricing-plan.json"))
a = ap.parse_args()

key = sys.stdin.read().strip()
if not (key.startswith("sk-") and len(key) > 20):
    sys.exit("no LiteLLM key on stdin")
plan = json.load(open(a.plan))
c = na.Client(a.base, na.read_creds(a.creds))
specs = [
    (f"LiteLLM svc203 · API（可販售）", plan["api"], "default", "openai/gpt-6-luna", None),
    (f"LiteLLM svc203 · 免費試用（自架 Qwen）", plan["local"], "free", plan["local"][0],
     json.dumps({"max_tokens": a.free_max_tokens})),
]
if a.with_internal:
    specs.append((f"LiteLLM svc203 · ChatGPT 訂閱（內部測試）", plan["internal"], "internal", "chatgpt/gpt-6-luna", None))
for name, models, group, test, override in specs:
    ch = {"type": 62, "name": name, "key": key, "base_url": LITELLM, "models": ",".join(models),
          "group": group, "auto_ban": 1, "test_model": test, "priority": 0, "weight": 0,
          "remark": f"{a.label}; LiteLLM key alias {a.key_alias}"}
    if override:
        ch["param_override"] = override
    d = c.req("POST", "/api/channel/", {"mode": "single", "channel": ch})
    print(f"{name}: group={group} models={len(models)} -> {d.get('success')} {d.get('message') or ''}")
