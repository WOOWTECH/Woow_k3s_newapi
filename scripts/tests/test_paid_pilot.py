"""HTTP fixture tests of bounded paid-pilot plan and executor."""
import copy
import json
import os
import stat
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import test_newapi_admin as common
sys.path.insert(0, os.path.join(REPO, "scripts"))
import newapi_admin as na
import paid_pilot as pp
import free_pilot as fp


class TestPaidPilot(common.Base):
    def setUp(self):
        super().setUp()
        self.fake.status["server_address"] = common.PROD
        prior, _ = pp.verified_evidence()
        self.fake.options["GroupRatio"] = na.encode({"default": 1, "internal": 1})
        self.fake.options["GroupGroupRatio"] = "{}"
        self.fake.options["UserUsableGroups"] = na.encode({"default": "預設"})
        for key in pp.OPTION_KEYS:
            current = json.loads(self.fake.options[key])
            current.update(prior[key])
            current.pop(pp.DEEPSEEK, None)
            self.fake.options[key] = na.encode(current)

    def plan(self):
        rc, out, err = self.run_tool("paid-pilot", target="prod")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def args(self, plan, journal, keyfile=None):
        args = ["paid-pilot", "--apply", "--expected-plan", plan["plan_sha256"], "--journal", journal]
        if keyfile:
            args += ["--upstream-key-file", keyfile]
        return args

    def test_plan_is_read_only_and_exact_scope(self):
        before = copy.deepcopy(self.fake.channels)
        report = self.plan()
        steps = report["plan"]["steps"]
        self.assertEqual([s["key"] for s in steps[:3]], list(pp.OPTION_KEYS))
        self.assertEqual(steps[3]["kind"], "create-channel")
        self.assertEqual(steps[3]["channel"]["models"].split(","), list(pp.MODELS))
        self.assertEqual(report["plan"]["scope"]["default_multiplier"], 1)
        self.assertEqual(self.fake.channels, before)
        self.assertEqual(self.writes(), [])

    def test_apply_and_noop_preserve_existing_channels_and_prices(self):
        before_channels = copy.deepcopy(self.fake.channels)
        before_prices = {k: copy.deepcopy(json.loads(self.fake.options[k])) for k in pp.OPTION_KEYS}
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        report = self.plan()
        journal = os.path.join(self.tmp, "paid-intent.jsonl")
        rc, out, err = self.run_tool(*self.args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(json.loads(out)["state"], "configured-not-inference-tested")
        self.assertEqual(self.fake.channels[:-1], before_channels)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)
        for key in pp.OPTION_KEYS:
            after = json.loads(self.fake.options[key])
            self.assertEqual({k: v for k, v in after.items() if k != pp.DEEPSEEK}, before_prices[key])
            self.assertEqual(after[pp.DEEPSEEK], pp.verified_evidence()[1][key])
        self.assertEqual(json.loads(self.fake.options["GroupRatio"])["default"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(journal).st_mode), 0o600)
        self.assertNotIn(common.UPSTREAM_KEY, self.read(journal))
        noop = self.plan()
        self.assertEqual(noop["plan"]["steps"], [])
        second = os.path.join(self.tmp, "noop.jsonl")
        rc, out, err = self.run_tool(*self.args(noop, second), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(self.writes()[-1][0], "POST")

    def test_internal_route_is_untouched(self):
        internal = na.wanted_channels(common.SPEC, common.PLAN, "stg", "alias", True)[-1]
        self.fake.channels.append(dict(internal, id=3, status=1, key=common.UPSTREAM_KEY))
        before = copy.deepcopy(self.fake.channels)
        report = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        rc, out, err = self.run_tool(*self.args(report, os.path.join(self.tmp, "internal.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(self.fake.channels[:-1], before)

    def test_rc44_reserialized_ratio_maps_apply_and_noop(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        original = fp.checked_request
        def reserialize(c, method, path, body=None, auth=True):
            answer = original(c, method, path, body, auth)
            if method == "PUT" and body["key"] in pp.OPTION_KEYS:
                key = body["key"]
                values = json.loads(self.fake.options[key])
                values = {k: int(v) if isinstance(v, float) and v.is_integer() else v
                          for k, v in values.items()}
                self.fake.options[key] = json.dumps(values, sort_keys=True, separators=(",", ":"))
            return answer
        with mock.patch.object(fp, "checked_request", side_effect=reserialize):
            rc, out, err = self.run_tool(*self.args(plan, os.path.join(self.tmp, "rc44.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(json.loads(out)["confirmed"], [0, 1, 2, 3])
        self.assertEqual(self.plan()["plan"]["steps"], [])

    def test_ratio_comparison_rejects_value_type_members_and_unrelated_changes(self):
        expected = {"ModelRatio": '{"a":1,"b":0.1}', "other": "unchanged"}
        self.assertTrue(pp.same_options({**expected, "ModelRatio": '{"b": 0.1, "a": 1.0}'}, expected))
        for value in ('{"a":true,"b":0.1}', '{"a":1,"b":0.10000000000001}',
                      '{"a":1}', '{"a":1,"b":0.1,"c":2}'):
            with self.subTest(value=value):
                self.assertFalse(pp.same_options({**expected, "ModelRatio": value}, expected))
        self.assertFalse(pp.same_options({**expected, "other": "changed"}, expected))
        self.assertEqual(fp.digest(pp._numeric_json({"price": 1.0})), fp.digest({"price": 1}))
        self.assertNotEqual(fp.digest(pp._numeric_json({"price": True})), fp.digest({"price": 1}))
        for value in ('{"a":1,"a":1,"b":0.1}', '{"a":1,"b":NaN}'):
            with self.subTest(value=value), self.assertRaises(na.Fatal):
                pp.same_options({**expected, "ModelRatio": value}, expected)

    def test_ratio_member_drift_after_write_stops_before_next_write(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        original = fp.checked_request
        def drift(c, method, path, body=None, auth=True):
            answer = original(c, method, path, body, auth)
            if method == "PUT":
                values = json.loads(self.fake.options["ModelRatio"])
                values[pp.EXISTING[0]] += 0.00000000000001
                self.fake.options["ModelRatio"] = json.dumps(values)
            return answer
        with mock.patch.object(fp, "checked_request", side_effect=drift):
            rc, out, _ = self.run_tool(*self.args(plan, os.path.join(self.tmp, "map-drift.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 0)
        self.assertEqual(json.loads(out)["confirmed"], [])

    def test_pricing_cache_version_can_move_but_price_or_membership_cannot(self):
        before = {"group_ratio": {"default": 1}, "usable_group": {"default": "default"},
                  "data": [{"model_name": pp.EXISTING[0], "model_ratio": 1.0,
                            "enable_groups": ["default"], "pricing_version": 10},
                           {"model_name": pp.EXISTING[1], "model_ratio": 2,
                            "enable_groups": ["default"]}]}
        after = copy.deepcopy(before)
        del after["data"][0]["pricing_version"]
        after["data"][1]["pricing_version"] = 11
        signature = fp.digest(pp._unrelated_pricing(before))
        self.assertEqual(fp.digest(pp._unrelated_pricing(after)), signature)
        after["data"][0]["model_ratio"] = 1.00000000000001
        self.assertNotEqual(fp.digest(pp._unrelated_pricing(after)), signature)
        after["data"][0]["model_ratio"] = 1
        after["data"][0]["enable_groups"] = ["internal"]
        self.assertNotEqual(fp.digest(pp._unrelated_pricing(after)), signature)

    def cached_create_response(self, remaining_reads):
        original = fp.checked_request
        state = {"created": False, "remaining": remaining_reads}
        def cached(c, method, path, body=None, auth=True):
            answer = original(c, method, path, body, auth)
            if method == "POST" and path == "/api/channel/":
                state["created"] = True
            if method == "GET" and path == "/api/pricing" and state["created"] and state["remaining"]:
                state["remaining"] -= 1
                answer = copy.deepcopy(answer)
                answer["data"] = [row for row in answer["data"] if row["model_name"] not in pp.MODELS]
            return answer
        return cached

    def test_confirmed_create_waits_for_pricing_cache_without_repeating_writes(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        with mock.patch.object(fp, "checked_request", side_effect=self.cached_create_response(2)), \
                mock.patch.object(pp.time, "sleep") as sleep:
            rc, out, err = self.run_tool(*self.args(plan, os.path.join(self.tmp, "cache.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(json.loads(out)["confirmed"], [0, 1, 2, 3])
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 3)

    def test_pricing_cache_timeout_stops_without_repeating_create(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        with mock.patch.object(fp, "checked_request", side_effect=self.cached_create_response(10)), \
                mock.patch.object(pp.time, "monotonic", side_effect=[0, 76]), \
                mock.patch.object(pp.time, "sleep") as sleep:
            rc, out, _ = self.run_tool(*self.args(plan, os.path.join(self.tmp, "cache-timeout.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 2)
        sleep.assert_not_called()
        self.assertEqual(json.loads(out)["confirmed"], [0, 1, 2])
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)

    def test_option_drift_during_cache_wait_stops_immediately(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        def drift(_):
            self.fake.options["UnrelatedOption"] = "changed"
        with mock.patch.object(fp, "checked_request", side_effect=self.cached_create_response(10)), \
                mock.patch.object(pp.time, "sleep", side_effect=drift) as sleep:
            rc, out, _ = self.run_tool(*self.args(plan, os.path.join(self.tmp, "cache-drift.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 2)
        sleep.assert_called_once()
        self.assertEqual(json.loads(out)["confirmed"], [0, 1, 2])
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)

    def test_conflict_disabled_and_drift_reject_before_writes(self):
        self.fake.channels.append(dict(pp.CHANNEL_POST, id=4, status=0, key=common.UPSTREAM_KEY))
        rc, _, err = self.run_tool("paid-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("disabled", err)
        self.assertEqual(self.writes(), [])
        self.fake.channels.pop()
        report = self.plan()
        self.fake.options["ModelRatio"] = na.encode({**json.loads(self.fake.options["ModelRatio"]), "other/paid": 2})
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "drift.jsonl")
        rc, _, err = self.run_tool(*self.args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("plan changed", err)
        self.assertFalse(os.path.exists(journal))
        self.assertEqual(self.writes(), [])

    def test_conflicting_price_and_existing_route_rejected(self):
        ratios = json.loads(self.fake.options["ModelRatio"])
        ratios[pp.DEEPSEEK] = 0.1
        self.fake.options["ModelRatio"] = na.encode(ratios)
        rc, _, err = self.run_tool("paid-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting DeepSeek", err)
        self.fake.options["ModelRatio"] = na.encode({k: v for k, v in ratios.items() if k != pp.DEEPSEEK})
        self.fake.channels[0]["models"] += "," + pp.CHATGPT[0]
        rc, _, err = self.run_tool("paid-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting", err)
        self.assertEqual(self.writes(), [])

    def test_unknown_write_stops_and_never_retries(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "unknown.jsonl")
        original = fp.checked_request
        def uncertain(c, method, path, body=None, auth=True):
            result = original(c, method, path, body, auth)
            if method == "PUT":
                raise OSError("response lost")
            return result
        with mock.patch.object(fp, "checked_request", side_effect=uncertain):
            rc, out, err = self.run_tool(*self.args(plan, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("stopped", err)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 0)
        events = [json.loads(line) for line in self.read(journal).splitlines()]
        self.assertEqual(events[-1]["state"], "stopped-reconcile-read-only")
        self.assertEqual(events[-1]["attempted"], [0])
        self.assertEqual(events[-1]["confirmed"], [])
        self.assertNotIn(common.UPSTREAM_KEY, self.read(journal))
        self.assertNotIn(common.UPSTREAM_KEY, out + err)

    def test_lost_channel_create_response_is_not_retried(self):
        plan = self.plan()
        keyfile = self.private("approved-upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "lost-create.jsonl")
        original = fp.checked_request
        def uncertain(c, method, path, body=None, auth=True):
            result = original(c, method, path, body, auth)
            if method == "POST" and path == "/api/channel/":
                raise OSError("response lost")
            return result
        with mock.patch.object(fp, "checked_request", side_effect=uncertain):
            rc, _, err = self.run_tool(*self.args(plan, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("stopped", err)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)
        events = [json.loads(line) for line in self.read(journal).splitlines()]
        self.assertEqual(events[-1]["attempted"], [0, 1, 2, 3])
        self.assertEqual(events[-1]["confirmed"], [0, 1, 2])
        self.assertEqual(events[-1]["state"], "stopped-reconcile-read-only")

    def test_evidence_gate_and_private_key(self):
        with mock.patch.dict(pp.EVIDENCE, {"policy-state.json": "0" * 64}):
            rc, _, err = self.run_tool("paid-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("evidence digest", err)
        self.assertEqual(self.fake.requests, [])
        plan = self.plan()
        rc, _, err = self.run_tool(*self.args(plan, os.path.join(self.tmp, "no-key.jsonl")), target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("private upstream key", err)
        self.assertEqual(self.writes(), [])
