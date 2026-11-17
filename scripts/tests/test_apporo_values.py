"""Apporo new-api (deploy/apporo-k3s.yaml): Apporo's own values and the parity tool.

Run by CI's "Apporo checks" step (scripts/apporo_checks.py, check "values"),
which sets APPORO_CHECKS=1: strictly for a change that touches an Apporo file,
warn-only for any other change (owner decision 2026-11-17: Apporo never fails a
WOOW pull request). Anywhere else, such as WOOW's "Admin tool unit tests"
step, this module skips itself. Locally: scripts/apporo_checks.py --only values.

Offline: helm template only; no cluster, credentials or network.

ApporoValuesTests: the documented Apporo differences hold the decided values
(docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉), written out in DECIDED below and
held against scripts/apporo_parity.py's list too: publicUrl,
newapi.env.DEFAULT_LANGUAGE, proxy.clients and proxy.addQuotaHourlyCapPoints
20000, which still lets the largest conversion through; proxy.enabled is a
literal boolean and the AP/OP expiries match it; the proxy pin is one WOOW prod
has or had and has a green stg report with the chart's new-api image;
per-instance evidence is never copied from WOOW; the two manual
NetworkPolicies match Apporo's own values and the rendered relay pods.

AiTermsTests: what Apporo's AI terms (v2.2) state about the gateway is
written out in deploy/apporo-k3s.yaml, never left to a chart default, and holds
in its render (docs/APPORO-K3S.md 〈AI 條款寫的事〉): usage records and audit
logs kept 180 days and cleared every day; the daily plaintext backup on an
in-cluster volume, rotated over 7 days; the Cloudflare edge; New API
unmodified, the chart's upstream image.

ParityCheckTests: scripts/apporo_parity.py on fixtures and chart copies with
guards of their own: a WOOW-only change, a new proxy value the chart requires
for WOOW and a WOOW pin change warn and exit 0; --strict exits non-zero; an
Apporo file that does not render as committed fails in every mode.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.environ.get('APPORO_CHECKS') != '1':
    raise unittest.SkipTest('Apporo checks run in their own CI step, scripts/apporo_checks.py (strict for a change '
                            'to an Apporo file, warn-only otherwise); locally: scripts/apporo_checks.py --only values')

from tests.contract import gate_check, report  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
APPORO = ROOT / 'deploy/apporo-k3s.yaml'
PROD = ROOT / 'deploy/woow-k3s.yaml'
LITELLM_PROD = ROOT / 'deploy/litellm-from-newapi-ingress.yaml'
LITELLM_APPORO = ROOT / 'deploy/litellm-from-newapi-apporo-ingress.yaml'
TOOL = ROOT / 'scripts/apporo_parity.py'
_spec = importlib.util.spec_from_file_location('apporo_parity', TOOL)
parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parity)
# The labels of the new-api pods that serve /v1 (chart/templates/newapi.yaml): the
# pods Apporo's LiteLLM ingress lets in.
RELAY_LABELS = {'app.kubernetes.io/name': 'newapi', 'app.kubernetes.io/instance': 'newapi',
                'newapi.woowtech.io/serves-relay': 'true'}
EVIDENCE_HINT = ('per-instance evidence (encryptedBackup.capacityVerification, restoreVerification, '
                 'ageRecipient) must be Apporo\'s own: its own capacity review and restore drill, bound '
                 'to namespace new-api-apporo; never copy WOOW\'s (docs/APPORO-K3S.md)')

# The owner's decisions for Apporo (docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉), written out
# here: scripts/apporo_parity.py's constants (its list of documented differences) must equal
# them, so a decision changes only by changing this test, in a PR the owner reviews for it,
# never by editing the parity tool's list together with the values.
DECIDED = {
    # Q&A round 2: the public URL.
    'PUBLIC_URL': 'https://newapi.apporo.ai',
    # Q&A round 4: what customers see is in English (and US$).
    'DEFAULT_LANGUAGE': 'en',
    # Q&A round 3: apun-odoo split into its web pod (component application) and a cron pod;
    # both call the proxy, each with its own signing key.
    'APUN_CLIENTS': [{
        'name': 'apun-odoo',
        'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'apun'}},
        'podSelector': {
            'matchLabels': {'app.kubernetes.io/name': 'apun'},
            'matchExpressions': [{'key': 'app.kubernetes.io/component', 'operator': 'In',
                                  'values': ['application', 'cron']}],
        },
    }],
    # Q&A round 10: proxy.addQuotaHourlyCapPoints 20000 (WOOW prod: the chart default 100000).
    'HOURLY_CAP_POINTS': 20000,
    # Q&A rounds 7-8: conversions of 50-10,000 points copied from WOOW, and R = 32 unchanged
    # (15,625 quota a point; the 30:1 rule divides the model prices instead).
    'LARGEST_CONVERSION_POINTS': 10000,
    'QUOTA_PER_POINT': 15625,
    # Q&A rounds 7-8, the defaults: namespace new-api-apporo.
    'APPORO_NAMESPACE': 'new-api-apporo',
}
DECIDED_HINT = ('an owner decision (2026-11-17; docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉): change it only when '
                'the owner decides again, here in DECIDED, in scripts/apporo_parity.py and in the values together')

# What Apporo's AI terms (v2.2) state about the gateway, as values deploy/apporo-k3s.yaml
# writes out itself (docs/APPORO-K3S.md 〈AI 條款寫的事〉). A chart default never counts:
# a new default would change it silently.
TERMS_VALUES = {
    # 6-3 "Per-request usage records and audit logs in Apporo's AI gateway are kept for
    # 180 days ... cleared by daily jobs."
    'retention.enabled': True,
    'retention.suspend': False,
    'retention.days': 180,
    # 6-3 "Backups of the AI gateway rotate over 7 days." Owner decision: the daily
    # plaintext pg_dump, on an in-cluster Longhorn volume.
    'backup.enabled': True,
    'backup.suspend': False,
    'backup.keepDays': 7,
    'backup.storageClassName': 'longhorn',
    # 6-1 "handled by Cloudflare, Inc., which provides our network edge".
    'cloudflared.enabled': True,
}
# Must run every day ("cleared by daily jobs"; one backup a day).
DAILY = ('retention.schedule', 'backup.schedule')
# 12-1 "We use an unmodified version" of New API: the upstream image the chart pins.
# (1-2 "Apporo's AI gateway at newapi.apporo.ai" is publicUrl, a decided value above.)
UPSTREAM_NEW_API = 'docker.io/calciumion/new-api'
TERMS_HINT =('Apporo\'s AI terms state this to customers. Change the terms first (owner decision), then '
              'deploy/apporo-k3s.yaml and this test together (docs/APPORO-K3S.md 〈AI 條款寫的事〉)')
CHART_HINT = ('If a chart change moved it, re-check the AI terms\' promise against the new chart and update this '
              'test in an Apporo PR (docs/APPORO-K3S.md 〈AI 條款寫的事〉)')


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


def runs_every_day(schedule):
    """A five-field cron schedule that fires on every day: 'M H * * *' (lists, ranges and steps allowed in M H)."""
    fields = str(schedule).split()
    return (len(fields) == 5 and fields[2:] == ['*', '*', '*']
            and all(re.fullmatch(r'[0-9*/,-]+', field) for field in fields[:2]))


def rendered_objects(render):
    """{(kind, metadata.name): document text} of a helm template output."""
    out = {}
    for doc in re.split(r'(?m)^---\s*$', render):
        kind, name = re.search(r'(?m)^kind: (\S+)$', doc), re.search(r'(?m)^  name: (\S+)$', doc)
        if kind and name:
            out[(kind.group(1), name.group(1))] = doc
    return out


def parsed(objects, kind, name):
    """One rendered object as data (helm parses the YAML; CI has no YAML library)."""
    doc = objects.get((kind, name))
    if doc is None:
        raise AssertionError(f'the render has no {kind} {name}. {CHART_HINT}')
    with tempfile.NamedTemporaryFile('w', suffix='.yaml') as tmp:
        tmp.write(doc)
        tmp.flush()
        return parity.values(tmp.name)


def pod_spec(obj):
    spec = obj['spec']
    if obj['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']['spec']


def container_env(obj, container=None):
    containers = pod_spec(obj)['containers']
    picked = next(c for c in containers if container in (None, c['name']))
    return {e['name']: e.get('value') for e in picked.get('env') or []}


class ApporoValuesTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.text = APPORO.read_text()
        self.values = parity.values(APPORO)
        self.proxy = self.values.get('proxy') or {}

    def test_renders_as_committed(self):
        # What helm would deploy (scripts/apporo_checks.py "render" checks it too).
        enabled = self.proxy.get('enabled')
        try:
            out = parity.render(APPORO, parity.APPORO_NAMESPACE, {'proxy': {'enabled': enabled}})
        except parity.RenderError as err:
            self.fail(f'deploy/apporo-k3s.yaml does not render in {parity.APPORO_NAMESPACE} as committed '
                      f'(proxy.enabled={enabled}): {err}\n{EVIDENCE_HINT}')
        if enabled:
            self.assertIn(f'PROXY_ADD_QUOTA_HOURLY_CAP_POINTS: "{DECIDED["HOURLY_CAP_POINTS"]}"', out)

    def test_parity_tool_holds_the_decided_values(self):
        # The parity tool lays these over WOOW prod; it must not carry a decision of its own.
        for name, want in DECIDED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(parity, name), want, f'scripts/apporo_parity.py {name} is {DECIDED_HINT}')

    def test_documented_differences_hold_the_decided_values(self):
        env = (self.values.get('newapi') or {}).get('env') or {}
        self.assertEqual(self.values.get('publicUrl'), DECIDED['PUBLIC_URL'], f'publicUrl is {DECIDED_HINT}')
        self.assertEqual(env.get('DEFAULT_LANGUAGE'), DECIDED['DEFAULT_LANGUAGE'],
                         f'newapi.env.DEFAULT_LANGUAGE is {DECIDED_HINT}')
        self.assertEqual(self.proxy.get('clients'), DECIDED['APUN_CLIENTS'], f'proxy.clients is {DECIDED_HINT}')
        self.assertEqual(self.proxy.get('addQuotaHourlyCapPoints'), DECIDED['HOURLY_CAP_POINTS'],
                         f'proxy.addQuotaHourlyCapPoints is {DECIDED_HINT}')

    def test_hourly_cap_lets_the_largest_conversion_through(self):
        # newapi-proxy gives every replica cap * 15,625 // replicas quota an
        # hour; a smaller share answers the largest single conversion with 429.
        proxy = parity.merge(parity.values(ROOT / 'chart/values.yaml'), self.values)['proxy']
        per_point, largest = DECIDED['QUOTA_PER_POINT'], DECIDED['LARGEST_CONVERSION_POINTS']
        share = proxy['addQuotaHourlyCapPoints'] * per_point // proxy['replicas']
        self.assertGreaterEqual(share, largest * per_point,
                                f'with {proxy["replicas"]} replicas the cap must be at least '
                                f'{proxy["replicas"] * largest} points: ask the owner for a new value')

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
        # With the chart's new-api image: when WOOW bumps it while Apporo's pin
        # lags, a WOOW-only change only warns here; follow WOOW prod's pin.
        want = {'newapi': report.image_pin((ROOT / 'chart/values.yaml').read_text(), 'newapi'),
                'proxy': report.image_pin(self.text, 'proxy')}
        self.assertRegex(want['proxy']['tag'] or '', r'^[0-9a-f]{40}$')
        self.assertRegex(want['proxy']['digest'] or '', r'^sha256:[0-9a-f]{64}$')
        self.assertNotEqual(want['proxy']['digest'], 'sha256:' + '0' * 64)
        path = Path(report.REPORTS_DIR) / report.report_name(want)
        self.assertTrue(path.exists(), f'no green stg report tests/contract/reports/{path.name} '
                                       'for the Apporo proxy pin with the chart\'s new-api image: follow WOOW '
                                       'prod\'s pin (docs/OPERATIONS.md 〈升版〉, docs/APPORO-K3S.md 〈跟著 WOOW 改〉)')
        self.assertEqual(report.gate_problems(json.loads(path.read_text()), want), [],
                         'follow WOOW prod\'s proxy pin (docs/APPORO-K3S.md 〈跟著 WOOW 改〉)')

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
        namespace = {'kubernetes.io/metadata.name': DECIDED['APPORO_NAMESPACE']}
        self.assertEqual(policy.get('spec'), {
            'podSelector': {'matchLabels': upstream['podLabels']},
            'policyTypes': ['Ingress'],
            'ingress': [{
                'from': [{'namespaceSelector': {'matchLabels': namespace},
                          'podSelector': {'matchLabels': RELAY_LABELS}}],
                'ports': [{'protocol': 'TCP', 'port': upstream['port']}],
            }],
        })

    def test_litellm_ingress_selects_the_rendered_relay_pods(self):
        # The pods that call LiteLLM (new-api master, and slaves when there are
        # any) carry every label the ingress above selects.
        enabled = self.proxy.get('enabled')
        try:
            objects = rendered_objects(parity.render(APPORO, parity.APPORO_NAMESPACE, {'proxy': {'enabled': enabled}}))
        except parity.RenderError:
            self.skipTest('deploy/apporo-k3s.yaml does not render as committed; see test_renders_as_committed')
        master = parsed(objects, 'Deployment', 'newapi-master')
        labels = master['spec']['template']['metadata']['labels']
        self.assertEqual({k: labels.get(k) for k in RELAY_LABELS}, RELAY_LABELS,
                         'deploy/litellm-from-newapi-apporo-ingress.yaml selects pods the chart no longer labels '
                         'that way: Apporo\'s relay would lose LiteLLM')

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


class AiTermsTests(unittest.TestCase):
    """What Apporo's AI terms (v2.2) state about the gateway: written out, and true in the render."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.values = parity.values(APPORO)
        cls.own = parity.flat(cls.values)  # Apporo's file only, no chart defaults
        enabled = (cls.values.get('proxy') or {}).get('enabled')
        try:
            cls.objects = rendered_objects(parity.render(APPORO, parity.APPORO_NAMESPACE,
                                                         {'proxy': {'enabled': enabled}}))
        except parity.RenderError as err:
            cls.objects = None
            cls.render_error = str(err).splitlines()[0] if str(err) else repr(err)

    def setUp(self):
        if self.objects is None and self._testMethodName.startswith('test_render'):
            # The render check and ApporoValuesTests.test_renders_as_committed fail
            # already; nothing to look at in the render.
            self.skipTest(f'deploy/apporo-k3s.yaml does not render as committed ({self.render_error}); '
                          'see ApporoValuesTests.test_renders_as_committed')

    def test_daily_schedule_reader(self):
        for schedule in ('30 4 * * *', '45 3 * * *', '0 */6 * * *', '15 1,13 * * *'):
            self.assertTrue(runs_every_day(schedule), schedule)
        for schedule in ('30 4 * * 0', '30 4 1 * *', '30 4 * 1 *', '30 4 */2 * *', '@weekly', '30 4 * *', '', None):
            self.assertFalse(runs_every_day(schedule), schedule)

    def test_terms_values_are_written_out_in_the_apporo_file(self):
        for key, want in TERMS_VALUES.items():
            with self.subTest(key=key):
                self.assertIn(key, self.own, f'write {key} out in deploy/apporo-k3s.yaml (the chart default does '
                                             f'not count: a new default would change it). {TERMS_HINT}')
                self.assertEqual(self.own[key], want, TERMS_HINT)
        for key in DAILY:
            with self.subTest(key=key):
                self.assertIn(key, self.own, f'write {key} out in deploy/apporo-k3s.yaml. {TERMS_HINT}')
                self.assertTrue(runs_every_day(self.own[key]),
                                f'{key} {self.own[key]!r} must run every day ("M H * * *"). {TERMS_HINT}')

    def test_encrypted_backups_follow_the_same_terms_if_turned_on(self):
        encrypted = self.values.get('encryptedBackup') or {}
        if encrypted.get('enabled'):
            self.assertEqual(encrypted.get('keepDays'), 7, TERMS_HINT)
            self.assertEqual(encrypted.get('storageClassName'), 'longhorn', TERMS_HINT)

    def test_new_api_is_the_charts_unmodified_upstream_image(self):
        self.assertNotIn('image', self.values.get('newapi') or {},
                         'Apporo runs the chart\'s new-api image: the chart\'s own files (the Valkey rate-limit '
                         'script, the rc.44 schema checks) belong to that version, and the AI terms call it New '
                         'API unmodified (section 12)')
        image = parity.merge(parity.values(ROOT / 'chart/values.yaml'), self.values)['newapi']['image']
        self.assertEqual(image['repository'], UPSTREAM_NEW_API,
                         f'Apporo\'s AI terms (section 12) say the gateway runs New API unmodified, the upstream '
                         f'{UPSTREAM_NEW_API} image. {TERMS_HINT}')

    def test_render_clears_usage_and_audit_logs_after_180_days_every_day(self):
        job = parsed(self.objects, 'CronJob', 'newapi-retention')
        self.assertIs(job['spec']['suspend'], False, CHART_HINT)
        self.assertTrue(runs_every_day(job['spec']['schedule']), f'{job["spec"]["schedule"]!r}. {CHART_HINT}')
        self.assertEqual(container_env(job).get('RETENTION_DAYS'), '180', CHART_HINT)
        script = parsed(self.objects, 'ConfigMap', 'newapi-retention-script')['data']['retention.sh']
        tables = re.search(r'(?m)^\s*for table in ([^;\n]+)', script)
        self.assertTrue(tables and {'logs', 'audit_logs'} <= set(tables.group(1).split()),
                        f'the retention job no longer shows that it clears logs and audit_logs. {CHART_HINT}')

    def test_render_keeps_7_days_of_daily_plaintext_backups_in_cluster(self):
        job = parsed(self.objects, 'CronJob', 'newapi-pg-backup')
        self.assertIs(job['spec']['suspend'], False, CHART_HINT)
        self.assertTrue(runs_every_day(job['spec']['schedule']), f'{job["spec"]["schedule"]!r}. {CHART_HINT}')
        self.assertEqual(container_env(job, 'pg-dump').get('KEEP_DAYS'), '7', CHART_HINT)
        command = '\n'.join(next(c for c in pod_spec(job)['containers'] if c['name'] == 'pg-dump')['command'])
        self.assertRegex(command, r'pg_dump -Fc ', f'a plaintext pg_dump. {CHART_HINT}')
        self.assertRegex(command, r'find /backups -name \'newapi-\*\.dump\' -mtime "\+\$KEEP_DAYS" -print -delete',
                         f'dumps older than KEEP_DAYS are deleted. {CHART_HINT}')
        claim = next(v['persistentVolumeClaim']['claimName'] for v in pod_spec(job)['volumes']
                     if v['name'] == 'backups')
        pvc = parsed(self.objects, 'PersistentVolumeClaim', claim)
        self.assertEqual(pvc['spec']['storageClassName'], 'longhorn', f'the in-cluster volume. {CHART_HINT}')

    def test_render_has_the_cloudflare_edge_and_the_public_url(self):
        tunnel = parsed(self.objects, 'Deployment', 'newapi-cloudflared')
        self.assertTrue(any('cloudflare/cloudflared:' in c['image'] for c in pod_spec(tunnel)['containers']),
                        CHART_HINT)
        master = parsed(self.objects, 'Deployment', 'newapi-master')
        self.assertEqual(container_env(master, 'new-api').get('SESSION_COOKIE_TRUSTED_URL'), DECIDED['PUBLIC_URL'])
        images = [c['image'] for c in pod_spec(master)['containers'] if c['name'] == 'new-api']
        self.assertEqual(len(images), 1, CHART_HINT)
        self.assertTrue(images[0].startswith(UPSTREAM_NEW_API + ':'), f'{images[0]}. {TERMS_HINT}')


class ParityCheckTests(unittest.TestCase):
    """scripts/apporo_parity.py: warn-only for WOOW changes, never for a broken Apporo file.

    Fixtures are the committed files' values plus the change under test, with
    Apporo's proxy state set explicitly, written as JSON (valid YAML): these
    hold whether or not the committed pair is in sync, and whether or not the
    committed Apporo proxy settings would render switched on. A render failure
    comes from a guard of the test's own in a copy of the chart, so the tests
    do not depend on WOOW's chart guards.
    """

    @classmethod
    def setUpClass(cls):
        # The fixtures start from the committed Apporo values. When that file
        # does not render as committed, the render check and
        # ApporoValuesTests.test_renders_as_committed report it: skip instead of
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

    def chart_with_guard(self, condition, message):
        """A copy of the chart that refuses to render when ``condition`` holds (a template expression)."""
        chart = self.tmp / 'chart'
        if not chart.exists():
            shutil.copytree(ROOT / 'chart', chart)
        (chart / 'templates/zz-parity-selftest.yaml').write_text(
            '{{- if ' + condition + ' }}\n{{- fail "' + message + '" }}\n{{- end }}\n')
        return chart

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

    def refused_switched_on(self):
        # A proxy setting the (copied) chart refuses, but only when it renders the proxy.
        chart = self.chart_with_guard('and .Values.proxy.enabled .Values.proxy.paritySelftestRefuse',
                                      'parity selftest: refused with the proxy on')
        return chart, {'proxy': {'paritySelftestRefuse': True}}

    def test_dormant_settings_that_fail_only_switched_on_warn_and_strict_fails(self):
        chart, change = self.refused_switched_on()
        apporo = self.apporo(change=change)
        rc, out = self.run_tool('--chart', chart, '--apporo', apporo)
        self.assertEqual(rc, 0, out)
        self.assertIn('proxy switched on', out)
        self.assertIn('parity selftest: refused with the proxy on', out)
        self.assertIn('::warning', out)
        self.assertNotIn('::error', out)
        rc, out = self.run_tool('--strict', '--chart', chart, '--apporo', apporo)
        self.assertEqual(rc, 2, out)
        self.assertIn('ERROR', out)

    def test_active_apporo_that_does_not_render_fails_in_every_mode(self):
        chart, change = self.refused_switched_on()
        apporo = self.apporo(enabled=True, change=change)
        for args in ((), ('--strict',)):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, '--chart', chart, '--apporo', apporo)
                self.assertEqual(rc, 2, out)
                self.assertIn('::error file=', out)

    def test_new_proxy_value_the_chart_requires_for_woow_only_warns_while_dormant(self):
        # A WOOW chart PR: the chart now refuses a proxy without a new value,
        # and the PR sets it for WOOW only. Reproduced on a copy of the chart.
        chart = self.chart_with_guard('and .Values.proxy.enabled (not .Values.proxy.paritySelftest)',
                                      'proxy.paritySelftest is required when proxy.enabled')
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
        # Once Apporo is active, its committed render no longer renders either:
        # the tool reports an error in every mode. Whether that blocks the PR is
        # scripts/apporo_checks.py's call (only for a change to an Apporo file).
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
