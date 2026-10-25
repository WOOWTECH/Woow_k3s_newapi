"""Fixed, journaled rc.44 paid route addition from verified post-PR38 evidence."""
import hashlib
import json
import os
import re
import time
import urllib.parse

import free_pilot as fp
import newapi_admin as na

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = {
    "litellm-model-inventory.json": "8cc84059f6edb25c9df7da819da7a3656c2481ddd4465f731ae4be2fd37c11d4",
    "policy-state.json": "eb597b6317d0e2b01b7431b23d7e4687c956232c1fa6bde1aa1c5bbe9b565b06",
    "paid-model-inventory.json": "d6d1fe517582791212b3dba784d7d93800ff9ffef468be0258199d5a2ac633c4",
}
CHATGPT = (
    "chatgpt/gpt-5.5", "chatgpt/gpt-5.6-luna", "chatgpt/gpt-5.6-sol",
    "chatgpt/gpt-5.6-terra", "chatgpt/gpt-6-astra", "chatgpt/gpt-6-luna",
    "chatgpt/gpt-6-sol", "chatgpt/gpt-6.1-sol",
)
DEEPSEEK = "deepseek/deepseek-v4-pro"
MODELS = CHATGPT + (DEEPSEEK,)
EXISTING = (
    "anthropic/claude-fable-5.1", "anthropic/claude-haiku-5.5",
    "anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5",
    "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4.1-flash",
)
CHANNEL = {
    "name": "LiteLLM svc203 · 付費試行（ChatGPT / DeepSeek v4 Pro）",
    "type": 62, "status": 1, "base_url": fp.CHANNEL["base_url"],
    "group": "default", "models": ",".join(MODELS), "test_model": CHATGPT[0],
    "param_override": "", "auto_ban": 1, "priority": 0, "weight": 0,
}
# The channel POST API accepts status implicitly; the desired readback requires 1.
CHANNEL_POST = {k: v for k, v in CHANNEL.items() if k != "status"}
OPTION_KEYS = ("ModelRatio", "CompletionRatio", "CacheRatio")
PRICING_WAIT_SECONDS = 75
PRICING_POLL_SECONDS = 5


def reject(message):
    raise na.Fatal("paid-pilot: " + message)


def verified_evidence():
    docs = {}
    for name, expected in EVIDENCE.items():
        path = os.path.join(HERE, "evidence", name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if hashlib.sha256(raw).hexdigest() != expected:
                reject("evidence digest mismatch: " + name)
            docs[name] = json.loads(raw)
        except (OSError, ValueError):
            reject("missing or invalid evidence: " + name)
    lit, policy, paid = (docs[k] for k in EVIDENCE)
    if (policy.get("options_ok") is not True or policy.get("pilot_ids") != "3,10"
            or policy.get("pricing_options", {}).get("GroupRatio", {}).get("default") != 1
            or paid.get("upstream_ok") is not True or paid.get("group_ratio", {}).get("default") != 1):
        reject("evidence does not establish the paid pilot baseline")
    info = lit.get("live_info")
    if not isinstance(info, list) or len({m.get("model") for m in info if isinstance(m, dict)}) != len(info):
        reject("ambiguous LiteLLM inventory")
    by_name = {m["model"]: m for m in info}
    if not set(MODELS) <= by_name.keys():
        reject("requested models missing from LiteLLM inventory")
    for name in CHATGPT:
        row = by_name[name]
        if row.get("upstream_model") != name or row.get("costs", {}).get("mode") != "responses":
            reject("unexpected ChatGPT upstream model")
    pro = by_name[DEEPSEEK]
    costs = pro.get("costs", {})
    if (pro.get("upstream_model") != "openrouter/" + DEEPSEEK
            or costs.get("input_cost_per_token") != 9.5526e-7
            or costs.get("output_cost_per_token") != 1.91052e-6
            or costs.get("cache_read_input_token_cost") != 7.0412e-8
            or costs.get("mode") != "chat"):
        reject("DeepSeek cost evidence changed")
    expected = {
        "ModelRatio": round(costs["input_cost_per_token"] * 1e6 / 2, 6),
        "CompletionRatio": round(costs["output_cost_per_token"] / costs["input_cost_per_token"], 6),
        "CacheRatio": round(costs["cache_read_input_token_cost"] / costs["input_cost_per_token"], 6),
    }
    if expected != {"ModelRatio": 0.47763, "CompletionRatio": 2, "CacheRatio": 0.07371}:
        reject("DeepSeek computed ratios changed")
    prior = policy["pricing_options"]
    for key in OPTION_KEYS:
        if (not isinstance(prior.get(key), dict) or DEEPSEEK in prior[key]
                or any(type(prior[key].get(name)) not in (int, float) or prior[key][name] <= 0
                       for name in CHATGPT + EXISTING)):
            reject("paid ratio evidence incomplete or conflicting")
    paid_rows = {m.get("model_name"): m for m in paid.get("models", []) if isinstance(m, dict)}
    if DEEPSEEK in paid_rows:
        reject("DeepSeek Pro was already exposed in evidence")
    if not set(EXISTING) <= paid_rows.keys():
        reject("existing paid routes absent from evidence")
    return prior, expected


def option_maps(opts):
    return {key: fp.object_option(opts, key) for key in OPTION_KEYS}


def same_options(actual, expected):
    """rc.44 re-serializes ratio maps; compare exact numbers, not JSON layout."""
    if actual.keys() != expected.keys():
        return False
    for key in expected:
        if actual[key] == expected[key]:
            continue
        if key not in OPTION_KEYS:
            return False
        left, right = fp.object_option(actual, key), fp.object_option(expected, key)
        if left.keys() != right.keys() or any(
                type(left[name]) not in (int, float)
                or type(right[name]) not in (int, float)
                or left[name] != right[name] for name in left):
            return False
    return True


def _route_models(ch):
    models = na._norm_set(ch["models"])
    if len(models) != len(set(models)) or len(na._norm_set(ch["group"])) != len(set(na._norm_set(ch["group"]))):
        reject("duplicate channel route member")
    return set(models)


def _target_channel(channels):
    candidates = [ch for ch in channels if ch.get("name") == CHANNEL["name"]
                  or (_route_models(ch) & set(MODELS)
                      and na._norm_set(ch["group"]) != ["internal"])]
    if not candidates:
        return None
    if len(candidates) != 1:
        reject("ambiguous requested model routes")
    ch = candidates[0]
    if (ch.get("name") != CHANNEL["name"] or ch.get("status") != 1
            or na.channel_diff(CHANNEL_POST, ch)
            or any(ch.get(k) not in (None, "", "{}")
                   for k in ("model_mapping", "status_code_mapping", "header_override"))):
        reject("conflicting or disabled requested route; existing channels are never edited")
    return ch


def _runtime_rows(pricing):
    rows = {}
    for row in pricing["data"]:
        name = row.get("model_name")
        if not isinstance(name, str) or name in rows:
            reject("ambiguous runtime pricing rows")
        rows[name] = row
    return rows


def _numeric_json(value):
    # Go emits integral float64 prices as JSON integers. Preserve exact values
    # and booleans while making these two numeric spellings hash identically.
    if type(value) is float and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _numeric_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numeric_json(item) for item in value]
    return value


def _unrelated_pricing(pricing):
    # rc.44 places the cache version marker on whichever row comes first after
    # Go map iteration. A rebuild may move it without changing any price.
    rows = [{key: value for key, value in row.items() if key != "pricing_version"}
            for row in pricing["data"] if row["model_name"] not in MODELS]
    return _numeric_json({"group_ratio": pricing["group_ratio"], "usable_group": pricing["usable_group"],
                          "rows": sorted(rows, key=lambda row: row["model_name"])})


def build_plan(opts, channels, pricing, prior, pro):
    maps = option_maps(opts)
    groups = fp.object_option(opts, "GroupRatio")
    if (type(groups.get("default")) not in (int, float) or groups["default"] != 1
            or pricing["group_ratio"].get("default") != 1):
        reject("default multiplier must remain production 1")
    if fp.object_option(opts, "GroupGroupRatio") != {}:
        reject("group overrides require separate review")
    if "default" not in fp.object_option(opts, "UserUsableGroups"):
        reject("default group must be usable")
    rows = _runtime_rows(pricing)
    for name in EXISTING:
        matches = [ch for ch in channels if name in _route_models(ch)
                   and "default" in na._norm_set(ch["group"])]
        if len(matches) != 1 or matches[0].get("status") != 1:
            reject("known existing paid route missing, disabled, or ambiguous")
    for name in EXISTING:
        row = rows.get(name)
        if (not row or "default" not in row.get("enable_groups", [])
                or row.get("quota_type") != 0 or row.get("model_ratio") != maps["ModelRatio"][name]):
            reject("existing paid runtime pricing drift")
    for key in OPTION_KEYS:
        for name in CHATGPT + EXISTING:
            if maps[key].get(name) != prior[key][name]:
                reject("known paid ratio baseline drift: " + key + " / " + name)
    fixed = fp.object_option(opts, "ModelPrice")
    if any(name in fixed for name in MODELS):
        reject("fixed price conflicts with requested ratio billing")
    billing = fp.object_option(opts, "billing_setting.billing_mode")
    if any(name in billing and billing[name] != "ratio" for name in MODELS):
        reject("billing mode conflicts with requested ratio billing")
    for key in OPTION_KEYS:
        if DEEPSEEK in maps[key] and maps[key][DEEPSEEK] != pro[key]:
            reject("conflicting DeepSeek ratio: " + key)
    target = _target_channel(channels)
    if target is None:
        if DEEPSEEK in rows:
            reject("DeepSeek runtime route exists outside fixed channel")
        for name in CHATGPT:
            if name in rows and rows[name].get("enable_groups") != ["internal"]:
                reject("ChatGPT route exists outside fixed channel")
    if target is not None:
        for name in MODELS:
            row = rows.get(name)
            if (not row or "default" not in row.get("enable_groups", [])
                    or set(row["enable_groups"]) - {"default", "internal"}
                    or row.get("quota_type") != 0 or row.get("model_ratio") != maps["ModelRatio"][name]):
                reject("requested route runtime pricing drift")
    steps = [{"kind": "option", "key": key, "member": DEEPSEEK, "value": pro[key],
              "before_sha256": fp.digest(opts[key])}
             for key in OPTION_KEYS if DEEPSEEK not in maps[key]]
    if target is None:
        steps.append({"kind": "create-channel", "channel": CHANNEL_POST})
    return {"schema": 1, "target": "prod", "version": na.INVARIANTS_FOR_VERSION,
            "server_address": na.TARGET_URLS["prod"],
            "evidence_sha256": EVIDENCE,
            "baseline": {"options": fp.digest(opts), "channels": fp.digest(fp.channel_state(channels)),
                         "runtime_pricing": fp.digest(pricing)},
            "steps": steps, "scope": {"models": MODELS, "existing_paid_routes": EXISTING,
                                  "default_multiplier": 1, "existing_channels_unchanged": True}}


def cmd_paid_pilot(a):
    # Evidence gate comes before even a read-only HTTP request.
    prior, pro = verified_evidence()
    if a.target != "prod" or not a.token_file or a.creds:
        reject("requires --target prod and a PAT file; password sessions are forbidden")
    if a.apply and (not a.journal or not re.fullmatch(r"[0-9a-f]{64}", a.expected_plan or "")):
        reject("apply requires a new --journal and --expected-plan SHA256")
    c = na.client_from_args(a)
    if (na.origin(c.base) != na.TARGET_URLS["prod"]
            and urllib.parse.urlsplit(c.base).hostname not in na.LOOPBACK_HOSTS):
        reject("base must be production origin or loopback port-forward")
    opts, channels, pricing = fp.read_state(c, "prod")
    plan = build_plan(opts, channels, pricing, prior, pro)
    report = {"command": "paid-pilot", "plan": plan, "plan_sha256": fp.digest(plan),
              "state": "plan", "attempted": [], "confirmed": []}
    if not a.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if a.expected_plan != report["plan_sha256"]:
        reject("plan changed; inspect a new read-only plan")
    upstream = None
    if any(s["kind"] == "create-channel" for s in plan["steps"]):
        if not a.upstream_key_file or a.upstream_key_file == "-":
            reject("channel creation requires a user-approved private upstream key file")
        upstream = na.read_upstream_key(a.upstream_key_file, None, "paid-pilot")
    report["state"] = "intent"
    journal = fp.Journal(a.journal, report)
    expected_opts = dict(opts)
    expected_channels = fp.channel_state(channels)
    unrelated = fp.digest(_unrelated_pricing(pricing))

    def check():
        now_opts, now_channels, now_pricing = fp.read_state(c, "prod")
        if (not same_options(now_opts, expected_opts) or fp.channel_state(now_channels) != expected_channels
                or fp.digest(_unrelated_pricing(now_pricing)) != unrelated):
            reject("state drift or incomplete write; reconcile read-only")
        current = build_plan(now_opts, now_channels, now_pricing, prior, pro)
        return now_opts, now_channels, now_pricing, current

    try:
        for index, step in enumerate(plan["steps"]):
            check()
            report.update(state="write-outcome-unknown", next_step=index)
            report["attempted"].append(index)
            journal.save()
            if step["kind"] == "option":
                key = step["key"]
                if key not in OPTION_KEYS or step["member"] != DEEPSEEK or step["value"] != pro[key]:
                    reject("unapproved option step")
                merged = {**fp.object_option(expected_opts, key), DEEPSEEK: pro[key]}
                fp.checked_request(c, "PUT", "/api/option/", {"key": key, "value": na.wire(merged)})
                expected_opts[key] = na.encode(merged)
            else:
                body = {**CHANNEL_POST, "remark": "paid-pilot rc.44; fixed model route", "key": upstream}
                fp.checked_request(c, "POST", "/api/channel/", {"mode": "single", "channel": body})
                del body
                _, observed, _, _ = check_after_create(c, expected_opts, expected_channels, unrelated, prior, pro)
                expected_channels = fp.channel_state(observed)
            check()
            report["confirmed"].append(index)
            report["state"] = "step-confirmed"
            journal.save()
        _, after_channels, after_pricing, _ = check()
        target = _target_channel(after_channels)
        rows = _runtime_rows(after_pricing)
        if target is None or any(rows.get(name, {}).get("model_ratio", 0) <= 0 for name in MODELS):
            reject("final route or effective paid pricing absent")
        report["state"] = "configured-not-inference-tested"
        journal.save()
    except Exception:
        report["state"] = "stopped-reconcile-read-only"
        journal.save()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        reject("stopped; inspect journal and live state; no automatic retry or rollback")
    finally:
        upstream = None
        journal.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def check_after_create(c, expected_opts, old_channels, unrelated, prior, pro):
    # rc.44 AddChannel commits abilities without invalidating the one-minute
    # pricing cache. Only poll reads after a successful POST; never resend it.
    deadline = time.monotonic() + PRICING_WAIT_SECONDS
    observed_target = None
    while True:
        opts, channels, pricing = fp.read_state(c, "prod")
        target = _target_channel(channels)
        if target is None or not same_options(opts, expected_opts):
            reject("channel create not observable")
        target_state = fp.channel_state([target])
        if observed_target is not None and target_state != observed_target:
            reject("new channel changed while waiting for cache")
        observed_target = target_state
        old = [ch for ch in channels if ch["id"] != target["id"]]
        if (fp.channel_state(old) != old_channels or len(channels) != len(old_channels) + 1
                or fp.digest(_unrelated_pricing(pricing)) != unrelated):
            reject("channel inventory or unrelated pricing drift after create")
        rows = _runtime_rows(pricing)
        ratios = option_maps(opts)["ModelRatio"]
        pending = False
        for name in MODELS:
            row = rows.get(name)
            if row is None:
                pending = True
                continue
            groups = row.get("enable_groups")
            if (not isinstance(groups, list) or not groups
                    or any(type(group) is not str for group in groups)
                    or set(groups) - {"default", "internal"}
                    or row.get("quota_type") != 0 or row.get("model_ratio") != ratios[name]):
                reject("requested pricing drift while waiting for cache")
            if "default" not in groups:
                if name not in CHATGPT or groups != ["internal"]:
                    reject("unexpected requested group while waiting for cache")
                pending = True
        if not pending:
            build_plan(opts, channels, pricing, prior, pro)
            return opts, channels, pricing, target
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reject("requested pricing cache did not converge; reconcile read-only")
        time.sleep(min(PRICING_POLL_SECONDS, remaining))
