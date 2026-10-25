"""Bounded rc.44 free-pilot transition; general production writers stay denied.

The plan is read-only. Apply requires its exact digest and an exclusive durable
journal. There are no retry, resume, arbitrary settings, update-channel or
rollback switches. An uncertain write stops for read-only reconciliation.
"""
import copy
import hashlib
import json
import os
import re
import urllib.parse

import newapi_admin as na

MODELS = ("qwen3-4b-aicoder", "qwen3-4b-cluster1")
CHANNEL = {
    "name": "LiteLLM svc203 · 免費試用（自架 Qwen）", "type": 62,
    "base_url": "http://svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000",
    "group": "free", "models": ",".join(MODELS), "test_model": MODELS[0],
    "param_override": '{"max_tokens": 2048}', "auto_ban": 1,
    "priority": 0, "weight": 0,
}
SPECIAL = "group_ratio_setting.group_special_usable_group"
PRECONSUME = "quota_setting.enable_free_model_pre_consume"
RATE = "ModelRequestRateLimit"
ALLOWED_OPTIONS = frozenset(("GroupRatio", "UserUsableGroups", SPECIAL, PRECONSUME,
                           RATE + "Enabled", RATE + "Count", RATE + "SuccessCount", RATE + "Group"))
VOLATILE_CHANNEL = frozenset(("key", "test_time", "response_time", "used_quota"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reject(message):
    raise na.Fatal("free-pilot: " + message)


def object_option(opts, key):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                reject("duplicate JSON member in " + key)
            result[k] = v
        return result
    try:
        value = json.loads(opts[key], object_pairs_hook=unique,
                           parse_constant=lambda _: reject("non-finite JSON in " + key))
    except (KeyError, ValueError, TypeError):
        reject("missing or invalid object option " + key)
    if not isinstance(value, dict):
        reject("expected object option " + key)
    return value


def checked_request(c, method, path, body=None, auth=True):
    # Upstream error messages can echo credentials or the POST body.
    try:
        result = c.req(method, path, body, auth=auth)
    except Exception:
        reject(method + " " + path.split("?")[0] + " failed; upstream details withheld")
    if not isinstance(result, dict) or result.get("success") is not True:
        reject(method + " " + path.split("?")[0] + " did not confirm success")
    return result


def read_state(c, target):
    status = checked_request(c, "GET", "/api/status", auth=False).get("data")
    if (not isinstance(status, dict) or status.get("version") != na.INVARIANTS_FOR_VERSION
            or status.get("server_address") not in (na.TARGET_URLS[target], na.TARGET_URLS[target] + "/")):
        reject("requires the known target and pinned rc.44 server")
    if any(problem for _, _, problem in na.check_status(status)):
        reject("non-pilot status invariants must already pass")
    rows = checked_request(c, "GET", "/api/option/").get("data")
    if not isinstance(rows, list):
        reject("invalid option response")
    opts = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str)
                or not isinstance(row.get("value"), str) or row["key"] in opts):
            reject("requires unambiguous string-valued options")
        if row["key"] not in na.NEVER_WRITE:
            opts[row["key"]] = row["value"]
    channels, total = [], None
    for page in range(1, 102):
        data = checked_request(c, "GET", f"/api/channel/?p={page}&page_size=100").get("data")
        if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                or type(data.get("total")) is not int or not 0 <= data["total"] <= 10000):
            reject("invalid channel pagination")
        if total is not None and data["total"] != total:
            reject("channel count changed during pagination")
        total = data["total"]
        channels.extend(data["items"])
        if len(channels) >= total:
            break
        if not data["items"]:
            reject("truncated channel response")
    ids = [ch.get("id") for ch in channels if isinstance(ch, dict)]
    if (len(channels) != total or len(ids) != total or len(set(ids)) != total
            or any(type(i) is not int or i < 1 for i in ids)):
        reject("ambiguous channel inventory")
    if any(not isinstance(ch.get(field), str) for ch in channels for field in ("group", "models", "name")):
        reject("invalid channel routing fields")
    pricing = checked_request(c, "GET", "/api/pricing")
    if (not isinstance(pricing.get("data"), list) or not isinstance(pricing.get("group_ratio"), dict)
            or not isinstance(pricing.get("usable_group"), dict)):
        reject("invalid runtime pricing response")
    return opts, channels, pricing


def channel_state(channels):
    # Never journal channel keys. Include every other non-metric field in the
    # hash so a concurrent route, mapping, status or credential-policy edit stops us.
    return sorted(({k: v for k, v in ch.items() if k not in VOLATILE_CHANNEL}
                   for ch in channels), key=lambda c: c["id"])


def paid_pricing(pricing):
    result = {k: {g: v for g, v in pricing[k].items() if g != "free"}
              for k in ("group_ratio", "usable_group")}
    rows = []
    for row in pricing["data"]:
        if (not isinstance(row, dict) or not isinstance(row.get("model_name"), str)
                or not isinstance(row.get("enable_groups"), list)
                or any(not isinstance(g, str) for g in row["enable_groups"])):
            reject("invalid model pricing entry")
        groups = sorted(g for g in row["enable_groups"] if g != "free")
        if groups:
            rows.append({**row, "enable_groups": groups})
    result["data"] = sorted(rows, key=lambda row: row["model_name"])
    return result


def free_channel(channels):
    matches = [ch for ch in channels if "free" in na._norm_set(ch.get("group"))
               or ch.get("name") == CHANNEL["name"]]
    if not matches:
        return None
    if (len(matches) != 1 or matches[0].get("name") != CHANNEL["name"]
            or matches[0].get("status") != 1 or na.channel_diff(CHANNEL, matches[0])):
        reject("conflicting or disabled free channel; existing channels are never edited")
    # A model remapping/override could silently send Qwen to another model.
    for field in ("model_mapping", "status_code_mapping", "header_override"):
        if matches[0].get(field) not in (None, "", "{}"):
            reject("unexpected mapping/headers on the free channel")
    return matches[0]


def build_plan(opts, channels, pricing, target):
    groups = object_option(opts, "GroupRatio")
    if (any(not na._num(v) or v < 0 for v in groups.values())
            or not na._num(groups.get("default")) or groups["default"] <= 0
            or ("free" in groups and not na.close(groups["free"], 0))):
        reject("requires positive paid default and absent or zero free ratio")
    if not na.close(pricing["group_ratio"].get("default"), groups["default"]):
        reject("stored and effective default price disagree")
    if "free" in pricing["group_ratio"] and not na.close(pricing["group_ratio"]["free"], 0):
        reject("effective free ratio is nonzero")
    gg = object_option(opts, "GroupGroupRatio")
    if gg:
        reject("group-to-group overrides require separate review")
    usable = object_option(opts, "UserUsableGroups")
    if "default" not in usable or any(not isinstance(v, str) for v in usable.values()):
        reject("requires default usable group and string labels")
    special = object_option(opts, SPECIAL)
    if special not in ({}, {"free_blocked": {"-:free": ""}}):
        reject("special usable-group overrides require separate review")
    rates = object_option(opts, RATE + "Group")
    enabled = opts.get(RATE + "Enabled")
    if enabled not in ("true", "false") or opts.get(RATE + "DurationMinutes") != "1":
        reject("requires a known limiter state and one-minute interval")
    for key in (RATE + "Count", RATE + "SuccessCount"):
        if not re.fullmatch(r"0|[1-9][0-9]{0,8}", opts.get(key, "")):
            reject("invalid limiter count")
    for group, counts in rates.items():
        if (not isinstance(counts, list) or len(counts) != 2
                or any(type(n) is not int or n < 0 for n in counts)):
            reject("invalid group limiter")
    if enabled == "false" and set(rates) - {"free"}:
        reject("enabling dormant non-free rate overrides would change paid behavior")
    if opts.get(PRECONSUME) not in ("true", "false") or opts.get("RegisterEnabled") != "false":
        reject("requires known pre-consumption state and closed registration")
    if free_channel(channels) is not None and ("free" not in groups or "free" not in usable):
        reject("existing free route with incomplete group state requires reconciliation")
    if target == "prod" and free_channel(channels) is None:
        legacy = [ch for ch in channels if ch.get("status") == 1
                  and ch.get("type") == CHANNEL["type"] and ch.get("base_url") == CHANNEL["base_url"]
                  and "default" in na._norm_set(ch.get("group"))
                  and set(MODELS) <= set(na._norm_set(ch.get("models")))]
        if len(legacy) != 1:
            reject("requires the observed single legacy default Qwen route before production migration")

    new_rates = copy.deepcopy(rates)
    # Disabled -> enabled must NOT activate the dormant global 1000/min cap.
    # rc.44 skips both count and success limit when they are zero.
    success = (rates.get("free", [0, int(opts[RATE + "SuccessCount"])])[1]
               if enabled == "true" else 0)
    new_rates["free"] = [10, success]
    desired = [(RATE + "Count", int(opts[RATE + "Count"]) if enabled == "true" else 0),
               (RATE + "SuccessCount", int(opts[RATE + "SuccessCount"]) if enabled == "true" else 0),
               (RATE + "Group", new_rates), (RATE + "Enabled", True),
               (SPECIAL, {"free_blocked": {"-:free": ""}}),
               ("GroupRatio", {**groups, "free": 0}), (PRECONSUME, False)]
    steps = [{"kind": "option", "key": key, "before": opts[key], "after": value}
             for key, value in desired if not na.same(value, opts.get(key))]
    if free_channel(channels) is None:
        steps.append({"kind": "create-channel", "channel": CHANNEL})
    new_usable = {**usable, "free": usable.get("free", "免費試用")}
    if not na.same(new_usable, opts["UserUsableGroups"]):
        steps.append({"kind": "option", "key": "UserUsableGroups",
                      "before": opts["UserUsableGroups"], "after": new_usable})
    zero_paid = sorted(row["model_name"] for row in paid_pricing(pricing)["data"]
                       if row.get("billing_mode") != "tiered_expr"
                       and row.get("model_price" if row.get("quota_type") == 1 else "model_ratio") == 0)
    return {
        "schema": 1, "target": target, "server_address": na.TARGET_URLS[target],
        "version": na.INVARIANTS_FOR_VERSION,
        "baseline": {"options": digest(opts), "channels": digest(channel_state(channels)),
                     "paid_pricing": digest(paid_pricing(pricing))},
        "steps": steps, "default_ratio_preserved": groups["default"],
        "scope": {"zero_cost_paid_models_allow_zero_balance": zero_paid,
                  "preconsume_is_global": True, "existing_channels_unchanged": True,
                  "full_pricing_acceptance": False, "free_inference_tested": False},
    }


class Journal:
    def __init__(self, path, report):
        # O_EXCL also rejects symlinks; do not overwrite or auto-resume an intent.
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.report = report
        self.save()
        parent = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def save(self):
        # Append-only durable events: a torn final line cannot erase the prior intent.
        raw = (json.dumps(self.report, ensure_ascii=False, sort_keys=True) + "\n").encode()
        view = memoryview(raw)
        while view:
            written = os.write(self.fd, view)
            view = view[written:]
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)


def cmd_free_pilot(a):
    if not a.target or not a.token_file or a.creds:
        reject("requires --target and a PAT file; password sessions are forbidden")
    if a.apply and (not a.journal or not re.fullmatch(r"[0-9a-f]{64}", a.expected_plan or "")):
        reject("apply requires --journal (new path) and --expected-plan from the read-only plan")
    c = na.client_from_args(a)
    if (na.origin(c.base) != na.TARGET_URLS[a.target]
            and urllib.parse.urlsplit(c.base).hostname not in na.LOOPBACK_HOSTS):
        reject("base must be the matching public origin or loopback port-forward")
    opts, channels, pricing = read_state(c, a.target)
    plan = build_plan(opts, channels, pricing, a.target)
    report = {"command": "free-pilot", "plan": plan, "plan_sha256": digest(plan),
              "state": "plan", "attempted": [], "confirmed": []}
    if not a.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if a.expected_plan != report["plan_sha256"]:
        reject("plan changed; inspect a new read-only plan before applying")
    upstream = None
    if any(step["kind"] == "create-channel" for step in plan["steps"]):
        if not a.upstream_key_file or a.upstream_key_file == "-":
            reject("creating the free channel requires an explicit private upstream key file")
        upstream = na.read_upstream_key(a.upstream_key_file, None, "free-pilot")
    report["state"] = "intent"
    journal = Journal(a.journal, report)
    expected_opts = dict(opts)
    expected_channels = channel_state(channels)
    paid_hash = digest(paid_pricing(pricing))

    def check():
        now_opts, now_channels, now_prices = read_state(c, a.target)
        if (now_opts != expected_opts or channel_state(now_channels) != expected_channels
                or digest(paid_pricing(now_prices)) != paid_hash):
            reject("state drift or incomplete write; stop and reconcile read-only")
        return now_opts, now_channels, now_prices

    try:
        for index, step in enumerate(plan["steps"]):
            check()
            report.update(state="write-outcome-unknown", next_step=index)
            report["attempted"].append(index)
            journal.save()  # intent reaches disk before the request leaves
            if step["kind"] == "option":
                if step["key"] not in ALLOWED_OPTIONS:
                    reject("unapproved option")
                checked_request(c, "PUT", "/api/option/", {"key": step["key"], "value": na.wire(step["after"])})
                expected_opts[step["key"]] = na.encode(step["after"])
            else:
                body = {**CHANNEL, "remark": "free-pilot rc.44; fixed Qwen route", "key": upstream}
                checked_request(c, "POST", "/api/channel/", {"mode": "single", "channel": body})
                del body
                _, observed, _ = read_state(c, a.target)
                created = free_channel(observed)
                if created is None:
                    reject("channel create not observable; never retry blindly")
                old = [ch for ch in observed if ch["id"] != created["id"]]
                if channel_state(old) != expected_channels:
                    reject("channel inventory drift after create")
                expected_channels = channel_state(observed)
            check()
            report["confirmed"].append(index)
            report["state"] = "step-confirmed"
            journal.save()
        _, after_channels, after_prices = check()
        if (free_channel(after_channels) is None or not na.close(after_prices["group_ratio"].get("free"), 0)
                or "free" not in after_prices["usable_group"]
                or {row["model_name"] for row in after_prices["data"] if "free" in row["enable_groups"]} != set(MODELS)):
            reject("final effective free routing check failed")
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
