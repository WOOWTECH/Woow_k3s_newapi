"""Differential test: the built-in pricing gate vs the platform's own llm_pricing.py.

  python3 tests/apporo_sync/platform_difftest.py <llm_pricing.py> <export --out snapshot of WOOW> [<version>]
Random catalogue mutations (pilot and GA profiles); every verdict must agree. <version>: the New API
version WOOW ran, for an export taken before the tool recorded it (as --source-version).
"""
import copy
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apporo_sync as aps  # noqa: E402
import apporo_newapi_content as nc  # noqa: E402
mod = aps.load_platform_module(sys.argv[1])
prof = nc.load_profile(os.path.join(REPO, "deploy", "apporo-sync", "profile.json"))
snap = aps.load_snapshot(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
desired, _ = nc.transform(snap, prof)
base = {"options": desired["options"], "channels": desired["channels"]}
pilot = prof["platform"]["paid_pilot_models"]
rng = random.Random(1117)
def state(opts, chans):
    return {"options": opts, "channels": chans, "abilities": nc.expected_abilities(chans)}
def mutate(opts, chans):
    o, c = dict(opts), copy.deepcopy(chans)
    for _ in range(rng.randint(1, 3)):
        r = rng.random()
        m = rng.choice(pilot)
        if r < 0.15:
            gr = json.loads(o["GroupRatio"]); gr["default"] = rng.choice([1, 1.3, 0.9, 1.0]); o["GroupRatio"] = json.dumps(gr)
        elif r < 0.3:
            k = rng.choice(["ModelRatio", "CompletionRatio", "CacheRatio"]); mp = json.loads(o[k]); mp[m] = rng.choice([0, 0.5, 2]); o[k] = nc.go_json(mp)
        elif r < 0.4:
            mp = json.loads(o["ModelPrice"]); mp[m] = rng.choice([0, 0.01]); o["ModelPrice"] = nc.go_json(mp)
        elif r < 0.5:
            c[4]["models"] += "," + m
        elif r < 0.6:
            uu = json.loads(o["UserUsableGroups"]); uu[rng.choice(["vip", "internal"])] = "x"; o["UserUsableGroups"] = json.dumps(uu)
        elif r < 0.7:
            bm = json.loads(o["billing_setting.billing_mode"]); be = json.loads(o["billing_setting.billing_expr"])
            bm[m] = "tiered_expr"; be[m] = rng.choice(["", "tokens * 2"]); o["billing_setting.billing_mode"] = nc.go_json(bm); o["billing_setting.billing_expr"] = nc.go_json(be)
        elif r < 0.8:
            for cid in (1, 3):
                c[cid]["models"] = ",".join(x for x in c[cid]["models"].split(",") if x not in pilot[:rng.randint(0, 15)])
        elif r < 0.9:
            c[1]["group"] = rng.choice(["default", "default,free", "internal"])
        else:
            mp = json.loads(o["ModelRatio"]); mp.pop(m, None); o["ModelRatio"] = nc.go_json(mp)
    return o, c
def real(payload):
    try:
        mod.normalize_pricing(payload, profile=prof["platform"]["pricing_profile"]); return None
    except mod.PricingInvalid as e:
        return str(e)
agree = disagree = 0
labels = {}
for i in range(3000):
    o, c = mutate(base["options"], base["channels"])
    p = aps.pricing_payload(state(o, c), prof)
    a, b = real(p), aps.platform_check(p, prof["platform"])
    labels[a] = labels.get(a, 0) + 1
    if a == b: agree += 1
    else:
        disagree += 1
        if disagree < 5: print("DISAGREE", a, b)
print("agree", agree, "disagree", disagree, "labels", labels)
# GA profile too
prof2 = copy.deepcopy(prof); prof2["platform"]["pricing_profile"] = ""
ga_agree = ga_dis = 0
for i in range(1500):
    o, c = mutate(base["options"], base["channels"])
    p = aps.pricing_payload(state(o, c), prof2)
    try:
        mod.normalize_pricing(p, profile=""); a = None
    except mod.PricingInvalid as e:
        a = str(e)
    b = aps.platform_check(p, prof2["platform"])
    if a == b: ga_agree += 1
    else:
        ga_dis += 1
        if ga_dis < 5: print("GA DISAGREE", a, b)
print("GA agree", ga_agree, "disagree", ga_dis)
