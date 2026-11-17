"""Apporo new-api (deploy/apporo-k3s.yaml) stays a 1:1 copy of WOOW prod.

Offline: helm template only; no cluster, credentials or network. WOOW prod
renders in new-api and Apporo in new-api-apporo, each against its own
namespace-bound evidence. The Apporo render must equal the WOOW prod render
with only the documented Apporo differences layered on top
(docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉):

- publicUrl, newapi.env.DEFAULT_LANGUAGE and proxy.clients;
- proxy.enabled stays false (dormant) until Apporo's own activation PR, which
  also sets proxy.apExpiresAt/opExpiresAt to the unix-seconds expiry of
  Apporo's PATs. While dormant, the proxy settings are still compared with
  proxy.enabled forced on, so they cannot fall behind WOOW before activation;
- proxy.image may lag WOOW prod (owner decision D7) but never lead it: it must
  be a pin deploy/woow-k3s.yaml has or had, with a green stg contract report;
- per-instance evidence, which is never copied from WOOW:
  encryptedBackup.capacityVerification and restoreVerification (the chart
  binds both to the release namespace) and ageRecipient come from
  deploy/apporo-k3s.yaml: Apporo's own capacity review, restore drill and age
  key. The rendered recipient is the only output they change.

Any other WOOW prod values change has to be mirrored into deploy/apporo-k3s.yaml
in the same PR, or listed here and in the runbook as a new documented difference.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.contract import gate_check, report

ROOT = Path(__file__).resolve().parents[2]
APPORO = ROOT / 'deploy/apporo-k3s.yaml'
PROD = ROOT / 'deploy/woow-k3s.yaml'
NAMESPACE = {APPORO: 'new-api-apporo', PROD: 'new-api'}
# The apun Odoo web pod (component application, live) and its cron pod.
APUN_POD_SELECTOR = {
    'matchLabels': {'app.kubernetes.io/name': 'apun'},
    'matchExpressions': [{'key': 'app.kubernetes.io/component', 'operator': 'In',
                          'values': ['application', 'cron']}],
}
APUN_CLIENTS = [{
    'name': 'apun-odoo',
    'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'apun'}},
    'podSelector': APUN_POD_SELECTOR,
}]
SECRET_KEYS = 'SESSION_SECRET|CRYPTO_SECRET|POSTGRES_PASSWORD|REDIS_PASSWORD|PROXY_VALKEY_PASSWORD'
EVIDENCE_HINT = ('per-instance evidence (encryptedBackup.capacityVerification, restoreVerification, '
                 'ageRecipient) must be Apporo\'s own: its own capacity review and restore drill, bound '
                 'to namespace new-api-apporo; never copy WOOW\'s (docs/APPORO-K3S.md)')


def helm(*args):
    proc = subprocess.run(['helm', *args], text=True, capture_output=True)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return proc.stdout


def values(path):
    """The values a file sets, parsed by helm itself (no YAML library in CI)."""
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp)
        (chart / 'templates').mkdir()
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: values-dump\nversion: 0.0.0\n')
        (chart / 'templates/values.json').write_text('{{ toJson .Values }}\n')
        out = helm('template', 'v', str(chart), '-f', str(path))
    return json.loads(out.split('# Source: values-dump/templates/values.json\n', 1)[1])


def render(path, extra):
    """``helm template`` of ``path`` (plus ``extra``) in the file's own namespace."""
    namespace = NAMESPACE[path]
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as tmp:
        json.dump(extra, tmp)
        tmp.flush()
        try:
            out = helm('template', 'newapi', str(ROOT / 'chart'), '-n', namespace, '-f', str(path), '-f', tmp.name)
        except AssertionError as err:
            hint = f'\n{EVIDENCE_HINT}' if path == APPORO else ''
            raise AssertionError(f'{path.relative_to(ROOT)} does not render in {namespace}: {err}{hint}') from None
    # Offline Helm generates random Secret bytes (templates/secret.yaml): keep
    # the key names, drop the values.
    out = re.sub(rf'(?m)^(  (?:{SECRET_KEYS}):).*$', r'\1 <redacted>', out)
    # Per-instance output: the age recipient and the namespace in PromQL.
    out = re.sub(r'(?m)^(\s+- name: AGE_RECIPIENT\n\s+value: ).*$', r'\1<per-instance>', out)
    return out.replace(f'namespace="{namespace}"', 'namespace="<release namespace>"')


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
        self.values = values(APPORO)
        self.proxy = self.values.get('proxy') or {}

    def documented(self, enabled):
        """The Apporo differences, layered on the WOOW prod values."""
        image = self.proxy.get('image') or {}
        return {
            'publicUrl': 'https://newapi.apporo.ai',
            'newapi': {'env': {'DEFAULT_LANGUAGE': 'en'}},
            'proxy': {
                'enabled': enabled,
                'clients': APUN_CLIENTS,
                'apExpiresAt': self.proxy.get('apExpiresAt', ''),
                'opExpiresAt': self.proxy.get('opExpiresAt', ''),
                'image': {'tag': image.get('tag', ''), 'digest': image.get('digest', '')},
            },
        }

    def test_render_is_prod_plus_documented_differences(self):
        # As committed, and (while dormant) with the proxy switched on.
        for enabled in dict.fromkeys([self.proxy.get('enabled'), True]):
            with self.subTest(proxy_enabled=enabled):
                self.assertEqual(
                    render(APPORO, {'proxy': {'enabled': enabled}}), render(PROD, self.documented(enabled)),
                    'deploy/apporo-k3s.yaml must equal deploy/woow-k3s.yaml plus the documented Apporo '
                    'differences (docs/APPORO-K3S.md): mirror the WOOW prod change; ' + EVIDENCE_HINT)

    def test_per_instance_evidence_is_not_copied_from_prod(self):
        prod = values(PROD).get('encryptedBackup') or {}
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

    def test_litellm_ingress_mirrors_prod(self):
        swap = {'name: litellm-from-newapi-ingress': 'name: litellm-from-newapi-apporo-ingress',
                'kubernetes.io/metadata.name: new-api': 'kubernetes.io/metadata.name: new-api-apporo'}
        prod = manifest(ROOT / 'deploy/litellm-from-newapi-ingress.yaml')
        expected = [line.replace(line.strip(), swap[line.strip()]) if line.strip() in swap else line
                    for line in prod]
        self.assertEqual(expected, manifest(ROOT / 'deploy/litellm-from-newapi-apporo-ingress.yaml'))

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


if __name__ == '__main__':
    unittest.main()
