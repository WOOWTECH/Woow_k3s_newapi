"""Build new-api pricing (ratio 1 = US$2 per 1M tokens) from LiteLLM svc203 costs.
Model ratio = LiteLLM cost (no markup); markup goes on group ratios later.

Usage: build_pricing.py deploy/pricing/litellm-models-YYYYMMDD.json deploy/pricing/pricing-plan.json

API models whose LiteLLM input cost is 0 (OpenRouter ":free" and the like)
are left out of "api" and listed under "unpriced": with
quota_setting.enable_free_model_pre_consume false, new-api bills ModelRatio 0
as a free model for everyone in any group (relay/helper/price.go), outside the
free group's rate limit and the PaaS daily meter. To sell one, give it a
nonzero price here first (an owner decision); verify-settings refuses a paid
group serving a model with ratio 0.
"""
import json, sys
models = json.load(open(sys.argv[1]))
by = {m["name"]: m for m in models}
SUB = ("chatgpt/", "toypark1234/", "apporo/")
LOCAL = {"qwen3-4b-aicoder", "qwen3-4b-cluster1"}
# Self-hosted placeholder price (US$/token) until the business sets one.
LOCAL_PRICE = (0.05e-6, 0.20e-6)
api, internal, local, unpriced = [], [], [], []
ratio, comp, cache, src = {}, {}, {}, {}
def setp(name, i, o, cr, origin):
    i = i or 0.0; o = o or 0.0
    ratio[name] = round(i * 1e6 / 2, 6)
    comp[name] = round(o / i, 6) if i else 1
    if cr and i:
        cache[name] = round(cr / i, 6)
    src[name] = origin
for m in models:
    n = m["name"]
    if n.startswith(SUB):
        internal.append(n)
        base = n.split("/", 1)[1]
        eq = by.get("openai/" + base) or by["openai/gpt-6-sol"]
        setp(n, eq["in"], eq["out"], eq["cache_read"], "subscription; priced as " + eq["name"])
    elif n in LOCAL:
        local.append(n)
        setp(n, LOCAL_PRICE[0], LOCAL_PRICE[1], None, "self-hosted placeholder")
    elif not m["in"]:
        unpriced.append(n)
        src[n] = "LiteLLM cost 0: not sold (ratio 0 would be free for everyone)"
    else:
        api.append(n)
        setp(n, m["in"], m["out"], m["cache_read"], "LiteLLM cost")
json.dump({"api": sorted(api), "local": sorted(local), "internal": sorted(internal), "unpriced": sorted(unpriced),
           "ModelRatio": ratio, "CompletionRatio": comp, "CacheRatio": cache, "source": src},
          open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
print(len(api), "api", len(local), "local", len(internal), "internal", len(unpriced), "unpriced")
