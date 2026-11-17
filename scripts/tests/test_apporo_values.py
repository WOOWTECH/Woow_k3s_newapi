"""Apporo new-api (deploy/apporo-k3s.yaml): Apporo's own values, blocking.

Offline: helm template only; no cluster, credentials or network.

Blocking (ApporoValuesTests): deploy/apporo-k3s.yaml renders in new-api-apporo
as committed; the documented Apporo differences hold the decided values
(docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉): publicUrl,
newapi.env.DEFAULT_LANGUAGE, proxy.clients and proxy.addQuotaHourlyCapPoints
20000, which still lets the largest conversion through; the backup and
retention periods Apporo's AI terms promise; proxy.enabled is a literal
boolean and the AP/OP expiries match it; the proxy pin is one WOOW prod has or
had and has a green stg report; per-instance evidence is never copied from
WOOW; the two manual NetworkPolicies match Apporo's own values. These read
Apporo's own file (with chart defaults only where it says so), so a WOOW values
change cannot turn them red.

Not blocking: whether everything else still equals WOOW prod, including
whether the dormant proxy settings would render switched on and whether the
pin equals WOOW prod's current one. That is scripts/apporo_parity.py, which CI
runs warn-only (owner decision 2026-11-17: it never blocks a WOOW PR; nothing
syncs the values automatically yet, docs/APPORO-K3S.md 〈跟著 WOOW 改〉).
ParityCheckTests pin that behaviour on fixtures: a WOOW-only change, a new
proxy value the chart requires for WOOW, and a WOOW pin change warn and exit 0;
--strict exits non-zero; an Apporo file that does not render as committed fails
in every mode.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.contract import gate_check, report

ROOT = Path(__file__).resolve().parents[2]
APPORO = ROOT / 'deploy/apporo-k3s.yaml'
PROD = ROOT / 'deploy/woow-k3s.yaml'
LITELLM_PROD = ROOT / 'deploy/litellm-from-newapi-ingress.yaml'
LITELLM_APPORO = ROOT / 'deploy/litellm-from-newapi-apporo-ingress.yaml'
TOOL = ROOT / 'scripts/apporo_parity.py'
_spec = importlib.util.spec_from_file_location('apporo_parity', TOOL)
parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parity)
# The labels of the new-api pods that serve /v1 (chart/templates/newapi.yaml).
RELAY_LABELS = {'app.kubernetes.io/name': 'newapi', 'app.kubernetes.io/instance': 'newapi',
                'newapi.woowtech.io/serves-relay': 'true'}
EVIDENCE_HINT = ('per-instance evidence (encryptedBackup.capacityVerification, restoreVerification, '
                 'ageRecipient) must be Apporo\'s own: its own capacity review and restore drill, bound '
                 'to namespace new-api-apporo; never copy WOOW\'s (docs/APPORO-K3S.md)')
TERMS_HINT = ('Apporo\'s AI terms promise these periods to customers (usage records and audit logs in the AI '
              'gateway kept 180 days and cleared by daily jobs; gateway backups rotate over 7 days). Change the '
              'terms first (owner decision), then this file and this test (docs/APPORO-K3S.md '
              '〈和 WOOW 正式站的差異〉)')


def prod_pins():
    """Proxy pins deploy/woow-k3s.yaml has now or had in a commit reachable from HEAD."""
    pins = [report.image_pin(PROD.read_text(), 'proxy')]
    log = subprocess.run(['git', 'log', '--format=%H', '--', 'deploy/woow-k3s.yaml'],
                         cwd=ROOT, text=True, capture_output=True)
    for sha in log.stdout.split() if log.returncode == 0 else []:
        old = subprocess.run(['git', 'show', f'{sha}:deploy/woow-k3s.yaml'], cwd=ROOT, text=True, capture_output=True)
        if old.returncode == 0:
            pins.append(report.image_pin(old.stdout, 'proxy'))
    return list({(pin['tag'], pin['digest']): pin for pin in pins if pin['tag'] and pin['digest']}.values())


def block(text, start):
    """Stripped, non-comment lines of the YAML block whose key line is ``start``."""
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if line.strip() == start)
    indent = len(lines[i]) - len(lines[i].lstrip())
    out = []
    for line in lines[i + 1:]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line.strip())
    return out


def manifest(path):
    return [line.rstrip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


class ApporoValuesTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.text = APPORO.read_text()
        self.values = parity.values(APPORO)
        self.proxy = self.values.get('proxy') or {}

    def test_renders_as_committed(self):
        # What helm would deploy: the shared chart must render it, like every
        # committed values file. While the proxy is dormant, whether it would
        # also render switched on is scripts/apporo_parity.py (warn-only: a new
        # proxy value the chart requires for WOOW must not block the WOOW PR).
        enabled = self.proxy.get('enabled')
        try:
            out = parity.render(APPORO, parity.APPORO_NAMESPACE, {'proxy': {'enabled': enabled}})
        except parity.RenderError as err:
            self.fail(f'deploy/apporo-k3s.yaml does not render in {parity.APPORO_NAMESPACE} as committed '
                      f'(proxy.enabled={enabled}): {err}\n{EVIDENCE_HINT}')
        self.assertIn('newapi.woowtech.io/serves-relay: "true"', out)
        if enabled:
            self.assertIn(f'PROXY_ADD_QUOTA_HOURLY_CAP_POINTS: "{parity.HOURLY_CAP_POINTS}"', out)

    def test_documented_differences_hold_the_decided_values(self):
        env = (self.values.get('newapi') or {}).get('env') or {}
        self.assertEqual(self.values.get('publicUrl'), parity.PUBLIC_URL)
        self.assertEqual(env.get('DEFAULT_LANGUAGE'), parity.DEFAULT_LANGUAGE)
        self.assertEqual(self.proxy.get('clients'), parity.APUN_CLIENTS)
        self.assertEqual(self.proxy.get('addQuotaHourlyCapPoints'), parity.HOURLY_CAP_POINTS,
                         'proxy.addQuotaHourlyCapPoints is an owner decision (2026-11-17): 20000 for Apporo '
                         '(docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉)')

    def test_hourly_cap_lets_the_largest_conversion_through(self):
        # newapi-proxy gives every replica cap * 15,625 // replicas quota an
        # hour; a smaller share answers the largest single conversion with 429.
        proxy = parity.merge(parity.values(ROOT / 'chart/values.yaml'), self.values)['proxy']
        share = proxy['addQuotaHourlyCapPoints'] * parity.QUOTA_PER_POINT // proxy['replicas']
        self.assertGreaterEqual(share, parity.LARGEST_CONVERSION_POINTS * parity.QUOTA_PER_POINT,
                                f'with {proxy["replicas"]} replicas the cap must be at least '
                                f'{proxy["replicas"] * parity.LARGEST_CONVERSION_POINTS} points: '
                                'ask the owner for a new value')

    def test_values_promised_in_the_ai_terms(self):
        # Written out in deploy/apporo-k3s.yaml itself, not inherited from the
        # chart defaults: neither a WOOW values change nor a new chart default
        # can move them, and a WOOW change to them only warns (apporo_parity.py).
        retention = self.values.get('retention') or {}
        backup = self.values.get('backup') or {}
        encrypted = self.values.get('encryptedBackup') or {}
        self.assertEqual({k: retention.get(k) for k in ('enabled', 'suspend', 'days')},
                         {'enabled': True, 'suspend': False, 'days': 180}, TERMS_HINT)
        self.assertEqual({k: backup.get(k) for k in ('keepDays', 'suspend')},
                         {'keepDays': 7, 'suspend': False}, TERMS_HINT)
        self.assertIsNot(backup.get('enabled'), False, TERMS_HINT)
        if encrypted.get('enabled'):
            self.assertEqual(encrypted.get('keepDays'), 7, TERMS_HINT)

    def test_per_instance_evidence_is_not_copied_from_prod(self):
        prod = parity.values(PROD).get('encryptedBackup') or {}
        mine = self.values.get('encryptedBackup') or {}
        for section, key in (('capacityVerification', 'evidenceRef'), ('restoreVerification', 'evidenceRef'),
                             ('restoreVerification', 'archiveSha256')):
            theirs = (prod.get(section) or {}).get(key)
            with self.subTest(field=f'{section}.{key}'):
                if theirs:
                    self.assertNotEqual((mine.get(section) or {}).get(key), theirs, EVIDENCE_HINT)

    def test_proxy_switch_is_a_literal_boolean(self):
        # CI tells dormant from active with gate_check.proxy_enabled (fail-closed).
        self.assertIsInstance(self.proxy.get('enabled'), bool)
        self.assertEqual(gate_check.proxy_enabled(self.text), self.proxy['enabled'],
                         'write proxy.enabled as a literal true/false directly under proxy:')

    def test_pat_expiry_matches_the_proxy_state(self):
        active = gate_check.proxy_enabled(self.text)
        want = r'^[0-9]{10}$' if active else r'^(|[0-9]{10})$'
        for key in ('apExpiresAt', 'opExpiresAt'):
            value = self.proxy.get(key, '')
            with self.subTest(key=key):
                self.assertIsInstance(value, str, f'quote proxy.{key}')
                self.assertRegex(value, want, 'the activation PR sets proxy.enabled: true together with the '
                                 'unix-seconds expiry of the Apporo PATs (docs/APPORO-K3S.md 〈打開 proxy〉)')

    def test_proxy_pin_lags_woow_prod_never_leads(self):
        pin = report.image_pin(self.text, 'proxy')
        self.assertIn(pin, prod_pins(),
                      'deploy/apporo-k3s.yaml proxy.image must be a pin deploy/woow-k3s.yaml has or had: '
                      'Apporo follows WOOW prod and never runs ahead of it (owner decision D7)')

    def test_proxy_pin_has_a_green_stg_report(self):
        want = {'newapi': report.image_pin((ROOT / 'chart/values.yaml').read_text(), 'newapi'),
                'proxy': report.image_pin(self.text, 'proxy')}
        self.assertRegex(want['proxy']['tag'] or '', r'^[0-9a-f]{40}$')
        self.assertRegex(want['proxy']['digest'] or '', r'^sha256:[0-9a-f]{64}$')
        self.assertNotEqual(want['proxy']['digest'], 'sha256:' + '0' * 64)
        path = Path(report.REPORTS_DIR) / report.report_name(want)
        self.assertTrue(path.exists(), f'no green stg report tests/contract/reports/{path.name} '
                                       'for the Apporo proxy pin (docs/OPERATIONS.md 〈升版〉)')
        self.assertEqual(report.gate_problems(json.loads(path.read_text()), want), [])

    def test_unique_top_level_keys(self):
        keys = re.findall(r'^([A-Za-z][A-Za-z0-9_]*):', self.text, re.M)
        self.assertEqual(len(keys), len(set(keys)), 'A later YAML key silently overrides an earlier one')

    def test_litellm_ingress_admits_exactly_the_apporo_relay(self):
        # Matches Apporo's own upstream entry. Whether it still mirrors WOOW's
        # deploy/litellm-from-newapi-ingress.yaml is scripts/apporo_parity.py.
        upstream = next((u for u in (self.values.get('networkPolicy') or {}).get('upstreams') or []
                         if u.get('name') == 'litellm-svc203'), None)
        self.assertIsNotNone(upstream, 'networkPolicy.upstreams needs the litellm-svc203 entry')
        policy = parity.values(LITELLM_APPORO)
        self.assertEqual((policy.get('apiVersion'), policy.get('kind')), ('networking.k8s.io/v1', 'NetworkPolicy'))
        self.assertEqual(policy.get('metadata'), {'name': 'litellm-from-newapi-apporo-ingress',
                                                  'namespace': upstream['namespace'],
                                                  'labels': {'app.kubernetes.io/part-of': 'woow-k3s-newapi'}})
        self.assertEqual(policy.get('spec'), {
            'podSelector': {'matchLabels': upstream['podLabels']},
            'policyTypes': ['Ingress'],
            'ingress': [{
                'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': parity.APPORO_NAMESPACE}},
                          'podSelector': {'matchLabels': RELAY_LABELS}}],
                'ports': [{'protocol': 'TCP', 'port': upstream['port']}],
            }],
        })

    def test_apun_egress_selects_exactly_the_proxy_clients(self):
        egress = (ROOT / 'deploy/apun-odoo-to-newapi-apporo-egress.yaml').read_text()
        client_pods = block(self.text, 'podSelector:')
        self.assertEqual(block(egress, 'podSelector:'), client_pods)
        self.assertEqual(client_pods, ['matchLabels:', 'app.kubernetes.io/name: apun', 'matchExpressions:',
                                       '- key: app.kubernetes.io/component', 'operator: In',
                                       'values: [application, cron]'])
        lines = manifest(ROOT / 'deploy/apun-odoo-to-newapi-apporo-egress.yaml')
        for want in ('  namespace: apun', '  policyTypes: ["Egress"]',
                     '              kubernetes.io/metadata.name: new-api-apporo',
                     '              app.kubernetes.io/name: newapi',
                     '              app.kubernetes.io/instance: newapi',
                     '              app.kubernetes.io/component: proxy',
                     '          port: 8080', '          port: 8081'):
            self.assertIn(want, lines)
        self.assertEqual(sum(line.strip().startswith('port:') for line in lines), 2)


class ParityCheckTests(unittest.TestCase):
    """scripts/apporo_parity.py: warn-only for WOOW changes, never for a broken Apporo file.

    Fixtures are the committed files' values plus the change under test, with
    Apporo's proxy state set explicitly, written as JSON (valid YAML): these
    hold whether or not the committed pair is in sync, and whether or not the
    committed Apporo proxy settings would render switched on.
    """

    @classmethod
    def setUpClass(cls):
        # The fixtures start from the committed Apporo values. When that file
        # does not render as committed, CI is red already (Lint and render,
        # ApporoValuesTests.test_renders_as_committed): skip instead of
        # repeating that failure in every test below.
        try:
            enabled = (parity.values(APPORO).get('proxy') or {}).get('enabled')
            parity.render(APPORO, parity.APPORO_NAMESPACE, {'proxy': {'enabled': enabled}})
        except (parity.RenderError, OSError) as err:
            first = str(err).splitlines()[0] if str(err) else repr(err)
            raise unittest.SkipTest(f'deploy/apporo-k3s.yaml does not render as committed ({first}); '
                                    'see ApporoValuesTests.test_renders_as_committed')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def fixture(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return path

    def run_tool(self, *args):
        proc = subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT, text=True, capture_output=True)
        return proc.returncode, proc.stdout + proc.stderr

    def woow(self, change, name='woow.json'):
        return self.fixture(name, parity.merge(parity.values(PROD), change))

    def apporo(self, enabled=False, change=None, name='apporo.json'):
        proxy = {'enabled': enabled}
        if enabled:  # an activation PR sets both expiries (ApporoValuesTests)
            proxy.update(apExpiresAt='1808450171', opExpiresAt='1808450171')
        return self.fixture(name, parity.merge(parity.values(APPORO), {'proxy': proxy}, change or {}))

    def woow_only_change(self):
        return self.woow({'newapi': {'env': {'PARITY_SELFTEST': 'woow-only'}}})

    def test_woow_only_change_warns_and_exits_0(self):
        rc, out = self.run_tool('--woow', self.woow_only_change(), '--apporo', self.apporo())
        self.assertEqual(rc, 0, out)
        self.assertIn('::warning file=', out)
        self.assertIn('WARNING (not blocking)', out)
        self.assertNotIn('::error', out)
        if 'cannot compare' not in out:
            self.assertIn('newapi.env.PARITY_SELFTEST', out)

    def test_strict_mode_fails_when_apporo_differs(self):
        rc, out = self.run_tool('--strict', '--woow', self.woow_only_change(), '--apporo', self.apporo())
        self.assertNotEqual(rc, 0, out)
        self.assertIn('FAILED', out)
        self.assertNotIn('::warning', out)

    def test_apporo_file_that_does_not_parse_fails_in_every_mode(self):
        broken = self.fixture('apporo.yaml', 'publicUrl: [\n')
        for args in ((), ('--strict',)):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, '--apporo', broken)
                self.assertEqual(rc, 2, out)
                self.assertIn('::error file=', out)

    def wide_clients(self):
        # The chart refuses a client without a component selector, but only
        # when it renders the proxy.
        return {'proxy': {'clients': [dict(parity.APUN_CLIENTS[0],
                                           podSelector={'matchLabels': {'app.kubernetes.io/name': 'apun'}})]}}

    def test_dormant_settings_that_fail_only_switched_on_warn_and_strict_fails(self):
        apporo = self.apporo(change=self.wide_clients())
        rc, out = self.run_tool('--apporo', apporo)
        self.assertEqual(rc, 0, out)
        self.assertIn('proxy switched on', out)
        self.assertIn('::warning', out)
        self.assertNotIn('::error', out)
        rc, out = self.run_tool('--strict', '--apporo', apporo)
        self.assertEqual(rc, 2, out)
        self.assertIn('ERROR', out)

    def test_active_apporo_that_does_not_render_fails_in_every_mode(self):
        apporo = self.apporo(enabled=True, change=self.wide_clients())
        for args in ((), ('--strict',)):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, '--apporo', apporo)
                self.assertEqual(rc, 2, out)
                self.assertIn('::error file=', out)

    def test_new_proxy_value_the_chart_requires_for_woow_only_warns_while_dormant(self):
        # A WOOW chart PR: the chart now refuses a proxy without a new value,
        # and the PR sets it for WOOW only. Reproduced on a copy of the chart.
        chart = self.tmp / 'chart'
        shutil.copytree(ROOT / 'chart', chart)
        (chart / 'templates/zz-parity-selftest.yaml').write_text(
            '{{- if and .Values.proxy.enabled (not .Values.proxy.paritySelftest) }}\n'
            '{{- fail "proxy.paritySelftest is required when proxy.enabled" }}\n'
            '{{- end }}\n')
        woow = self.woow({'proxy': {'paritySelftest': 'woow'}})
        rc, out = self.run_tool('--chart', chart, '--woow', woow, '--apporo', self.apporo())
        self.assertEqual(rc, 0, out)
        # Which guard helm reports first depends on its template order, so look
        # for the warning itself and the value-level line.
        self.assertIn(f'would not render in {parity.APPORO_NAMESPACE} with the proxy switched on', out)
        self.assertIn('values that differ', out)
        self.assertRegex(out, r'\n\s+proxy\.paritySelftest: "woow" \| <unset>\n')
        self.assertIn('::warning', out)
        self.assertNotIn('::error', out)
        rc, out = self.run_tool('--strict', '--chart', chart, '--woow', woow, '--apporo', self.apporo())
        self.assertEqual(rc, 2, out)
        # Once Apporo is active, its committed render is what helm deploys:
        # the chart PR has to set the value for Apporo too (as for WOOW stg).
        rc, out = self.run_tool('--chart', chart, '--woow', woow, '--apporo', self.apporo(enabled=True))
        self.assertEqual(rc, 2, out)
        self.assertIn('::error file=', out)

    def test_proxy_pin_that_differs_from_woow_prod_warns_and_strict_fails(self):
        # WOOW prod moved on (or rolled back); Apporo keeps its pin.
        woow = self.woow({'proxy': {'image': {'tag': 'a' * 40, 'digest': 'sha256:' + 'b' * 64}}})
        rc, out = self.run_tool('--woow', woow, '--apporo', self.apporo())
        self.assertEqual(rc, 0, out)
        self.assertIn('DIFFERS proxy pin', out)
        self.assertIn('::warning', out)
        rc, out = self.run_tool('--strict', '--woow', woow, '--apporo', self.apporo())
        self.assertNotEqual(rc, 0, out)
        self.assertIn('FAILED', out)
        self.assertIn('proxy pin', out.rsplit('FAILED', 1)[1])

    def test_litellm_copy_that_falls_behind_only_warns(self):
        woow_np = self.fixture('litellm-woow.yaml', LITELLM_PROD.read_text() + 'selftest: woow-only\n')
        rc, out = self.run_tool('--litellm-woow', woow_np, '--apporo', self.apporo())
        self.assertEqual(rc, 0, out)
        self.assertIn('no longer mirrors', out)
        self.assertIn('::warning', out)

    def test_in_sync_prints_ok_and_no_warning(self):
        np = 'metadata:\n  name: {}\nfrom:\n  matchLabels:\n    kubernetes.io/metadata.name: {}\n'
        woow_np = self.fixture('w.yaml', np.format('litellm-from-newapi-ingress', 'new-api'))
        apporo_np = self.fixture('a.yaml', np.format('litellm-from-newapi-apporo-ingress', 'new-api-apporo'))
        out = io.StringIO()
        with mock.patch.object(parity, 'values', return_value={'proxy': {'enabled': False}}), \
                mock.patch.object(parity, 'render', return_value='same render\n'), \
                contextlib.redirect_stdout(out):
            rc = parity.check(False, PROD, APPORO, woow_np, apporo_np)
        self.assertEqual(rc, 0)
        self.assertIn('in sync', out.getvalue())
        self.assertNotIn('::warning', out.getvalue())


if __name__ == '__main__':
    unittest.main()
