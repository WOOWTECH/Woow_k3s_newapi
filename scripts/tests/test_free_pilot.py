"""Safety and transition tests for the bounded free-pilot command."""
import copy
import json
import os
import stat
import subprocess
import sys
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import test_newapi_admin as common  # noqa: E402
sys.path.insert(0, os.path.join(REPO, "scripts"))
import free_pilot as fp  # noqa: E402
import newapi_admin as na  # noqa: E402


class TestFreePilot(common.Base):
    def setUp(self):
        super().setUp()
        self.original_options = copy.deepcopy(self.fake.options)
        self.original_channels = copy.deepcopy(self.fake.channels)

    def pilot_options(self, *, configured=False, dormant_global=False):
        """rc.44 prod baseline: paid ratio 1, registration closed, preconsume on."""
        self.fake.options = copy.deepcopy(self.original_options)
        self.fake.channels = copy.deepcopy(self.original_channels)
        if not configured:
            self.fake.channels = [ch for ch in self.fake.channels
                                  if "free" not in na._norm_set(ch.get("group"))
                                  and ch.get("name") != fp.CHANNEL["name"]]
            self.fake.channels[0]["models"] += "," + ",".join(fp.MODELS) + ",legacy/free-model"
            ratios = json.loads(self.fake.options["ModelRatio"])
            ratios["legacy/free-model"] = 0
            self.fake.options["ModelRatio"] = json.dumps(ratios)
        values = {
            "GroupRatio": {"default": 1, "internal": 1, **({"free": 0} if configured else {})},
            "GroupGroupRatio": {},
            "UserUsableGroups": {"default": "預設", **({"free": "免費試用"} if configured else {})},
            fp.SPECIAL: {"free_blocked": {"-:free": ""}} if configured else {},
            fp.RATE + "Enabled": configured,
            fp.RATE + "Count": 0,
            fp.RATE + "SuccessCount": 1000 if dormant_global else 0,
            fp.RATE + "DurationMinutes": 1,
            fp.RATE + "Group": {"free": [10, 10]} if configured else {},
            fp.PRECONSUME: not configured,
            "RegisterEnabled": False,
        }
        for key, value in values.items():
            self.fake.options[key] = na.encode(value)

    def prod(self):
        self.fake.status["server_address"] = common.PROD

    def plan(self, target="prod"):
        rc, out, err = self.run_tool("free-pilot", target=target)
        self.assertEqual(rc, 0, err)
        report = json.loads(out)
        self.assertEqual(report["state"], "plan")
        return report

    def apply_args(self, report, journal, keyfile=None):
        args = ["free-pilot", "--apply", "--expected-plan", report["plan_sha256"],
                "--journal", journal]
        if keyfile:
            args += ["--upstream-key-file", keyfile]
        return args

    def test_prod_plan_is_read_only_and_preserves_paid_pricing(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        # A paid channel anchors non-free effective pricing in the baseline.
        self.fake.channels = [dict(self.fake.channels[0], id=1, key=common.UPSTREAM_KEY)] if self.fake.channels else []
        report = self.plan()
        self.assertEqual(report["plan"]["default_ratio_preserved"], 1)
        self.assertEqual(report["plan"]["scope"]["zero_cost_paid_models_allow_zero_balance"], ["legacy/free-model"])
        self.assertTrue(report["plan"]["scope"]["existing_channels_unchanged"])
        self.assertEqual(self.writes(), [])
        self.assertTrue(all(r[0] == "GET" for r in self.fake.requests))
        self.assertNotIn(common.PAT, report.__repr__())
        self.assertNotIn(common.UPSTREAM_KEY, report.__repr__())

    def test_apply_changes_only_free_controls_and_does_not_activate_dormant_cap(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        before_group = self.fake.options["GroupRatio"]
        before_channels = copy.deepcopy(self.fake.channels)
        before_paid = fp.digest(fp.paid_pricing(self.fake.pricing()))
        keyfile = self.private("upstream", common.UPSTREAM_KEY + "\n")
        report = self.plan()
        journal = os.path.join(self.tmp, "intent.jsonl")

        rc, out, err = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")

        self.assertEqual(rc, 0, err + out)
        self.assertEqual(json.loads(out)["state"], "configured-not-inference-tested")
        self.assertEqual(json.loads(self.fake.options["GroupRatio"])["default"], 1)
        self.assertEqual(before_group, '{"default": 1, "internal": 1}')
        self.assertEqual(json.loads(self.fake.options["GroupRatio"])["internal"], 1)
        self.assertEqual(self.fake.options[fp.RATE + "Count"], "0")
        self.assertEqual(self.fake.options[fp.RATE + "SuccessCount"], "0")
        self.assertEqual(self.fake.options[fp.RATE + "Group"], '{"free": [10, 10]}')
        self.assertEqual(self.fake.options[fp.RATE + "Enabled"], "true")
        option_writes = [body for _, _, _, body in self.calls("PUT", "/api/option/")]
        enabled_at = next(i for i, body in enumerate(option_writes)
                          if body["key"] == fp.RATE + "Enabled")
        self.assertEqual({body["key"]: body["value"] for body in option_writes[:enabled_at]}, {
            fp.RATE + "SuccessCount": 0, fp.RATE + "Group": '{"free": [10, 10]}',
        })
        self.assertEqual(self.fake.options[fp.PRECONSUME], "false")
        self.assertEqual([c for c in self.fake.channels if c["group"] != "free"], before_channels)
        self.assertEqual(sum(c["group"] == "free" for c in self.fake.channels), 1)
        final_pricing = self.fake.pricing()
        self.assertEqual(fp.digest(fp.paid_pricing(final_pricing)), before_paid)
        self.assertEqual({row["model_name"] for row in final_pricing["data"]
                          if "free" in row["enable_groups"]}, set(fp.MODELS))
        self.assertTrue(os.path.exists(journal))
        self.assertEqual(stat.S_IMODE(os.stat(journal).st_mode), 0o600)
        self.assertNotIn(common.UPSTREAM_KEY, self.read(journal))
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)

    def test_pricing_version_movement_does_not_stop_apply(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        original = self.fake.pricing
        reads = 0

        def moving_metadata():
            nonlocal reads
            answer = original()
            answer["data"][reads % len(answer["data"])]["pricing_version"] = "same-cache-hash"
            reads += 1
            return answer

        with mock.patch.object(self.fake, "pricing", moving_metadata):
            report = self.plan()
            keyfile = self.private("metadata-key", common.UPSTREAM_KEY)
            rc, out, err = self.run_tool(*self.apply_args(
                report, os.path.join(self.tmp, "metadata.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(json.loads(out)["confirmed"], list(range(len(report["plan"]["steps"]))))

    def delayed_free_pricing(self, after_stale=None, remaining=3):
        original = self.fake.pricing
        state = {"remaining": remaining, "stale_reads": 0}

        def cached():
            answer = original()
            if "free" in json.loads(self.fake.options["UserUsableGroups"]) and state["remaining"]:
                state["remaining"] -= 1
                state["stale_reads"] += 1
                answer = copy.deepcopy(answer)
                answer["group_ratio"].pop("free", None)
                answer["usable_group"].pop("free", None)
                for row in answer["data"]:
                    row["enable_groups"] = [g for g in row["enable_groups"] if g != "free"]
                answer["data"] = [row for row in answer["data"] if row["enable_groups"]]
                if state["stale_reads"] == 2 and after_stale:
                    after_stale()
            return answer
        cached.state = state
        return cached

    def test_delayed_final_free_pricing_converges_without_extra_writes_and_replans_noop(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        report = self.plan()
        keyfile = self.private("cache-key", common.UPSTREAM_KEY)
        cached = self.delayed_free_pricing()
        with mock.patch.object(self.fake, "pricing", cached), \
                mock.patch.object(fp, "PRICING_POLL_SECONDS", 0.01):
            rc, out, err = self.run_tool(*self.apply_args(
                report, os.path.join(self.tmp, "cache.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(cached.state["stale_reads"], 3)
        self.assertEqual(len(self.writes()), len(report["plan"]["steps"]))
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)
        self.assertEqual(self.plan()["plan"]["steps"], [])

    def test_final_free_pricing_timeout_stops_with_confirmed_writes_and_safe_journal(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        report = self.plan()
        keyfile = self.private("timeout-key", common.UPSTREAM_KEY)
        journal = os.path.join(self.tmp, "cache-timeout.jsonl")
        cached = self.delayed_free_pricing(remaining=10000)
        with mock.patch.object(self.fake, "pricing", cached), \
                mock.patch.object(fp, "PRICING_WAIT_SECONDS", 0.3), \
                mock.patch.object(fp, "PRICING_POLL_SECONDS", 0.01):
            rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertGreaterEqual(cached.state["stale_reads"], 2)
        result = json.loads(out)
        self.assertEqual(result["state"], "stopped-reconcile-read-only")
        self.assertEqual(result["confirmed"], list(range(len(report["plan"]["steps"]))))
        self.assertEqual(len(self.writes()), len(report["plan"]["steps"]))
        self.assertEqual(json.loads(self.read(journal).splitlines()[-1]), result)
        self.assertNotIn(common.UPSTREAM_KEY, self.read(journal))

    def test_each_final_pricing_poll_checks_exact_options_channels_and_paid_prices(self):
        for kind in ("option-json", "channel", "paid-price", "paid-group-ratio", "paid-row-field", "paid-membership"):
            with self.subTest(kind=kind):
                self.prod()
                self.pilot_options(dormant_global=True)
                self.fake.requests.clear()
                report = self.plan()
                keyfile = self.private("drift-key-" + kind, common.UPSTREAM_KEY)
                original_pricing = self.fake.pricing
                drifted = []

                def drift():
                    drifted.append(kind)
                    if kind == "option-json":
                        groups = json.loads(self.fake.options["GroupRatio"])
                        self.fake.options["GroupRatio"] = json.dumps(groups, separators=(",", ":"))
                    elif kind == "channel":
                        self.fake.channels[0]["base_url"] = "http://drift.invalid"
                    elif kind in ("paid-price", "paid-row-field", "paid-membership"):
                        def changed():
                            answer = original_pricing()
                            if kind == "paid-price":
                                answer["data"][0]["model_ratio"] += 0.000001
                            elif kind == "paid-membership":
                                answer["data"][0]["enable_groups"] = ["internal"]
                            else:
                                answer["data"][0]["unknown_metadata"] = "must-not-ignore"
                            return answer
                        self.fake.pricing = changed
                    else:
                        self.fake.live_group_ratio = {"default": 1.01, "internal": 1}

                with mock.patch.object(self.fake, "pricing", self.delayed_free_pricing(after_stale=drift)), \
                        mock.patch.object(fp, "PRICING_POLL_SECONDS", 0.01):
                    rc, out, _ = self.run_tool(*self.apply_args(report,
                        os.path.join(self.tmp, "drift-" + kind + ".jsonl"), keyfile), target="prod")
                self.fake.live_group_ratio = None
                self.assertEqual(rc, 2)
                self.assertEqual(drifted, [kind])
                self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")
                self.assertEqual(len(self.writes()), len(report["plan"]["steps"]))

    def assert_final_http_deadline(self, mode):
        self.prod()
        self.pilot_options(dormant_global=True)
        report = self.plan()
        keyfile = self.private("stalled-key", common.UPSTREAM_KEY)
        journal = os.path.join(self.tmp, "stalled.jsonl")
        final = threading.Event()
        stalled = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_save = fp.Journal.save
        handler_class = self.server.srv.RequestHandlerClass
        original_send = handler_class._send
        original_popen = subprocess.Popen
        workers = []

        def save(current):
            original_save(current)
            if current.report["confirmed"] == list(range(len(report["plan"]["steps"]))):
                final.set()

        def send(handler, code, obj):
            if not final.is_set() or handler.path != "/api/pricing":
                return original_send(handler, code, obj)
            stalled.set()
            try:
                if mode == "stall":
                    release.wait(3)
                    return original_send(handler, code, obj)
                raw = json.dumps(obj).encode()
                handler.send_response(code)
                handler.send_header("Content-Length", str(len(raw) + 150))
                handler.end_headers()
                # Each byte arrives before a socket inactivity timeout; the
                # whole valid JSON response would only complete after 3s.
                for _ in range(150):
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    if release.wait(0.02):
                        return
                handler.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the parent kills the reader on expiry.
            finally:
                finished.set()

        def popen(*args, **kwargs):
            worker = original_popen(*args, **kwargs)
            workers.append(worker)
            return worker

        started = time.monotonic()
        try:
            with mock.patch.object(fp, "PRICING_WAIT_SECONDS", 0.5), \
                    mock.patch.object(fp.Journal, "save", save), \
                    mock.patch.object(handler_class, "_send", send), \
                    mock.patch.object(fp.subprocess, "Popen", popen):
                rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
            elapsed = time.monotonic() - started
        finally:
            release.set()
            finished.wait(1)
        self.assertTrue(stalled.is_set())
        self.assertEqual(rc, 2)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(workers[0].pid, os.WNOHANG)
        self.assertNotIn(common.PAT, repr(workers[0].args))
        self.assertNotIn(common.UPSTREAM_KEY, repr(workers[0].args))
        result = json.loads(out)
        self.assertEqual(result["state"], "stopped-reconcile-read-only")
        self.assertEqual(result["confirmed"], list(range(len(report["plan"]["steps"]))))
        self.assertEqual(len(self.writes()), len(report["plan"]["steps"]))
        self.assertEqual(json.loads(self.read(journal).splitlines()[-1]), result)
        self.assertNotIn(common.PAT, self.read(journal))
        self.assertNotIn(common.UPSTREAM_KEY, self.read(journal))

    def test_stalled_final_http_read_stops_at_deadline_with_confirmed_journal(self):
        self.assert_final_http_deadline("stall")

    def test_slow_drip_final_http_body_cannot_extend_deadline(self):
        self.assert_final_http_deadline("drip")

    def test_successful_worker_observed_after_deadline_is_rejected(self):
        report = self.plan("stg")
        original_popen = subprocess.Popen
        workers = []

        def popen(*args, **kwargs):
            worker = original_popen(*args, **kwargs)
            original_communicate = worker.communicate
            def delayed_observer(*args, **kwargs):
                answer = original_communicate(*args, **kwargs)
                time.sleep(0.6)  # Parent scheduling delay after a real success.
                return answer
            worker.communicate = delayed_observer
            workers.append(worker)
            return worker

        with mock.patch.object(fp, "PRICING_WAIT_SECONDS", 0.5), \
                mock.patch.object(fp.subprocess, "Popen", popen):
            rc, out, _ = self.run_tool(*self.apply_args(
                report, os.path.join(self.tmp, "late-success.jsonl")), target="stg")
        self.assertEqual(workers[0].returncode, 0)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")
        self.assertEqual(self.writes(), [])

    def test_worker_is_reaped_when_parent_communication_fails(self):
        report = self.plan("stg")
        original_popen = subprocess.Popen
        workers = []

        def popen(*args, **kwargs):
            worker = original_popen(*args, **kwargs)
            worker.communicate = mock.Mock(side_effect=OSError(common.PAT))
            workers.append(worker)
            return worker

        with mock.patch.object(fp.subprocess, "Popen", popen):
            rc, out, err = self.run_tool(*self.apply_args(
                report, os.path.join(self.tmp, "reader-error.jsonl")), target="stg")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")
        self.assertNotIn(common.PAT, out + err)
        self.assertIsNotNone(workers[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(workers[0].pid, os.WNOHANG)
        self.assertEqual(self.writes(), [])

    def test_worker_rejects_writes_and_suppresses_raw_input_errors(self):
        client = fp.ReadOnlyClient(self.base, token=common.PAT)
        with self.assertRaises(na.Fatal):
            client.req("POST", "/api/channel/", {"key": common.UPSTREAM_KEY})
        self.assertEqual(self.fake.requests, [])
        result = subprocess.run([sys.executable, fp.__file__], input=common.PAT,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout + result.stderr, "")

    def test_configured_staging_is_noop_and_existing_channel_is_byte_preserved(self):
        self.pilot_options(configured=True)
        channel = dict(fp.CHANNEL, id=1, key=common.UPSTREAM_KEY, status=1)
        self.fake.channels = [channel]
        before = copy.deepcopy(channel)
        report = self.plan("stg")
        self.assertEqual(report["plan"]["steps"], [])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.channels[0], before)

    def test_plan_hash_mismatch_and_existing_journal_stop_before_write(self):
        self.prod()
        self.pilot_options()
        report = self.plan()
        keyfile = self.private("upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "mismatch.jsonl")
        args = self.apply_args(report, journal, keyfile)
        args[args.index(report["plan_sha256"])] = "0" * 64
        rc, _, err = self.run_tool(*args, target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("plan changed", err)
        self.assertEqual(self.writes(), [])
        self.assertFalse(os.path.exists(journal))

        report = self.plan()
        journal = os.path.join(self.tmp, "existing.jsonl")
        with open(journal, "w") as f:
            f.write("keep\n")
        rc, _, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(self.read(journal), "keep\n")
        self.assertEqual(self.writes(), [])

    def test_duplicate_option_rows_are_rejected(self):
        self.prod()
        self.pilot_options()
        original = self.fake.option_list
        with mock.patch.object(self.fake, "option_list", lambda: original() + [original()[0]]):
            rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("unambiguous", err.lower())
        self.assertEqual(self.writes(), [])

    def test_real_staging_fixture_noop_apply_retains_existing_limits(self):
        before_options = copy.deepcopy(self.fake.options)
        before_channels = copy.deepcopy(self.fake.channels)
        report = self.plan("stg")
        self.assertEqual(report["plan"]["steps"], [])
        rc, out, err = self.run_tool(*self.apply_args(report, os.path.join(self.tmp, "noop.jsonl")), target="stg")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.options, before_options)
        self.assertEqual(self.fake.channels, before_channels)

    def test_success_without_effective_option_update_is_not_confirmed(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        report = self.plan()
        journal = os.path.join(self.tmp, "ignored.jsonl")
        keyfile = self.private("upstream", common.UPSTREAM_KEY)
        original = na.Client.req
        writes = []

        def ignored(client, method, path, body=None, **kwargs):
            if method == "PUT":
                writes.append(body["key"])
                return {"success": True}
            return original(client, method, path, body, **kwargs)

        with mock.patch.object(na.Client, "req", ignored):
            rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(json.loads(out)["confirmed"], [])

    def test_lost_channel_create_response_never_repeats_post(self):
        self.prod()
        self.pilot_options()
        report = self.plan()
        journal = os.path.join(self.tmp, "post-unknown.jsonl")
        keyfile = self.private("upstream", common.UPSTREAM_KEY)
        original = na.Client.req

        def lost(client, method, path, body=None, **kwargs):
            answer = original(client, method, path, body, **kwargs)
            if method == "POST":
                raise OSError("lost response " + common.UPSTREAM_KEY)
            return answer

        with mock.patch.object(na.Client, "req", lost):
            rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.calls("POST", "/api/channel/")), 1)
        self.assertEqual(len([c for c in self.fake.channels if c["group"] == "free"]), 1)
        self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")

    def test_route_drift_after_write_stops_without_repairing_existing_channel(self):
        self.prod()
        self.pilot_options(dormant_global=True)
        report = self.plan()
        keyfile = self.private("upstream", common.UPSTREAM_KEY)
        original = na.Client.req

        def drift(client, method, path, body=None, **kwargs):
            answer = original(client, method, path, body, **kwargs)
            if method == "PUT":
                self.fake.channels[0]["base_url"] = "http://changed.invalid"
            return answer

        with mock.patch.object(na.Client, "req", drift):
            rc, _, _ = self.run_tool(*self.apply_args(report, os.path.join(self.tmp, "route.jsonl"), keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)
        self.assertEqual(self.calls("PUT", "/api/channel/"), [])

    def test_runtime_and_stored_price_mismatch_is_refused(self):
        self.prod()
        self.pilot_options()
        self.fake.live_group_ratio = {"default": 1.3}
        rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("stored and effective default price disagree", err)
        self.assertEqual(self.writes(), [])

    def test_missing_or_duplicate_legacy_prod_route_is_refused(self):
        self.prod()
        self.pilot_options()
        original = copy.deepcopy(self.fake.channels)
        for channels in ([], [dict(original[0], models="paid-only")],
                         original + [dict(original[0], id=500)]):
            with self.subTest(channels=len(channels)):
                self.fake.channels = channels
                rc, _, err = self.run_tool("free-pilot", target="prod")
                self.assertEqual(rc, 2)
                self.assertIn("single legacy default Qwen route", err)
                self.assertEqual(self.writes(), [])

    def test_apply_writes_durable_intent_before_first_mutation(self):
        self.prod()
        self.pilot_options()
        keyfile = self.private("upstream", common.UPSTREAM_KEY + "\n")
        report = self.plan()
        journal = os.path.join(self.tmp, "ordered.jsonl")
        original = na.Client.req
        seen = []

        def observe(client, method, path, body=None, **kwargs):
            if method != "GET":
                with open(journal) as f:
                    events = [json.loads(line) for line in f]
                seen.append((method, path, events[-1]["state"], events[-1]["attempted"]))
            return original(client, method, path, body, **kwargs)

        with mock.patch.object(na.Client, "req", observe):
            rc, out, err = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 0, err + out)
        self.assertTrue(seen)
        self.assertTrue(all(state == "write-outcome-unknown" and attempted
                            for _, _, state, attempted in seen))

    def test_unknown_write_outcome_is_not_retried(self):
        self.prod()
        self.pilot_options()
        report = self.plan()
        keyfile = self.private("upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "unknown.jsonl")
        original = na.Client.req
        put_count = 0

        def uncertain(client, method, path, body=None, **kwargs):
            nonlocal put_count
            result = original(client, method, path, body, **kwargs)
            if method == "PUT" and path == "/api/option/":
                put_count += 1
                if put_count == 1:
                    raise OSError("simulated lost response " + common.PAT + common.UPSTREAM_KEY)
            return result

        with mock.patch.object(na.Client, "req", uncertain):
            rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(put_count, 1)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)
        self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")
        self.assertNotIn(common.PAT, out)
        self.assertNotIn(common.UPSTREAM_KEY, out)

    def test_readback_mismatch_stops_without_continuing_writes(self):
        self.prod()
        self.pilot_options()
        report = self.plan()
        keyfile = self.private("upstream", common.UPSTREAM_KEY + "\n")
        journal = os.path.join(self.tmp, "drift.jsonl")
        original = na.Client.req
        changed = False

        def drift(client, method, path, body=None, **kwargs):
            nonlocal changed
            result = original(client, method, path, body, **kwargs)
            if method == "PUT" and not changed:
                changed = True
                self.fake.options["RetryTimes"] = "drifted"
            return result

        with mock.patch.object(na.Client, "req", drift):
            rc, out, _ = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.calls("PUT", "/api/option/")), 1)
        self.assertEqual(json.loads(out)["state"], "stopped-reconcile-read-only")

    def test_wrong_target_password_and_bad_upstream_mode_are_rejected(self):
        self.pilot_options()
        rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("known target", err)

        self.prod()
        known_version = self.fake.status["version"]
        self.fake.status["version"] = "v0.0.0-unknown"
        rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("pinned rc.44", err)
        self.fake.status["version"] = known_version

        # Even a PAT plus --creds is rejected before either credential is used.
        rc, _, err = self.run_tool("--creds", self.creds(), "free-pilot")
        self.assertEqual(rc, 2)
        self.assertIn("PAT file", err)
        self.assertEqual(self.calls("POST", "/api/user/login"), [])

        report = self.plan()
        journal = os.path.join(self.tmp, "bad-key-mode.jsonl")
        keyfile = self.private("upstream-644", common.UPSTREAM_KEY + "\n", 0o644)
        rc, _, err = self.run_tool(*self.apply_args(report, journal, keyfile), target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("0600", err)
        self.assertFalse(os.path.exists(journal))
        self.assertEqual(self.writes(), [])

    def test_rejects_bad_maps_duplicate_members_stale_alias_and_dormant_overrides(self):
        cases = [
            ("duplicate JSON members", fp.RATE + "Group", '{"free":[10,0],"free":[1,2]}'),
            ("malformed rates", fp.RATE + "Group", '{"free":[10,"0"]}'),
            ("malformed usable map", "UserUsableGroups", "[]"),
        ]
        self.prod()
        for label, key, value in cases:
            with self.subTest(label=label):
                self.pilot_options()
                self.fake.options[key] = value
                rc, _, err = self.run_tool("free-pilot", target="prod")
                self.assertEqual(rc, 2)
                self.assertEqual(self.writes(), [])
                self.assertNotIn(common.PAT, err)

        self.pilot_options(dormant_global=True)
        self.fake.options[fp.RATE + "Group"] = '{"vip":[5,0]}'
        rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("dormant non-free", err)
        self.assertEqual(self.writes(), [])

        self.pilot_options()
        # rc.44 stale aliases must not enter the managed option set or be put.
        report = self.plan()
        self.assertNotIn("group_ratio_setting.group_ratio",
                         json.dumps(report["plan"]["steps"]))
        self.assertNotIn("group_ratio_setting.group_group_ratio",
                         json.dumps(report["plan"]["steps"]))

    def test_conflicting_free_channel_is_rejected(self):
        self.prod()
        self.pilot_options()
        self.fake.channels = [dict(fp.CHANNEL, id=1, key=common.UPSTREAM_KEY,
                                   status=1, base_url="https://wrong.example")]
        rc, _, err = self.run_tool("free-pilot", target="prod")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting or disabled free channel", err)
        self.assertEqual(self.writes(), [])

    def test_all_general_production_writers_remain_denied_through_script_entrypoint(self):
        self.prod()
        # Direct execution used to load newapi_admin twice and miss its Fatal class.
        proc = subprocess.run([
            sys.executable, os.path.join(REPO, "scripts", "newapi_admin.py"),
            "--base", self.base, "--token-file", self.pat_file, "--target", "prod",
            "call", "PUT", "/api/option/", '{"key":"RetryTimes","value":"1"}',
        ], text=True, capture_output=True, timeout=15)
        self.outputs.extend((proc.stdout, proc.stderr))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("production writes are disabled", proc.stderr)
        self.assertEqual(self.calls("PUT", "/api/option/"), [])

    def test_production_settings_pricing_and_channel_writers_remain_denied(self):
        self.prod()
        cases = [
            ("apply-settings", common.SETTINGS),
            ("apply-pricing",),
            ("apply-channels", "--key-alias", "pilot", "--key-file",
             self.private("paid-upstream", common.UPSTREAM_KEY + "\n")),
        ]
        for args in cases:
            with self.subTest(command=args[0]):
                rc, _, err = self.run_tool(*args, target="prod")
                self.assertEqual(rc, 2)
                self.assertIn("production writes are disabled", err)
                self.assertEqual(self.writes(), [])

