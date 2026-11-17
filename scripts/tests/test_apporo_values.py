"""Apporo new-api (deploy/apporo-k3s.yaml) stays a 1:1 copy of WOOW prod.

Offline: helm template only; no cluster, credentials or network. The Apporo
render must equal the WOOW prod render (deploy/woow-k3s.yaml) with only the
documented Apporo differences layered on top (docs/APPORO-K3S.md): publicUrl,
newapi.env.DEFAULT_LANGUAGE, proxy.clients and proxy.ap/opExpiresAt, plus
proxy.image, which may lag prod during an upgrade (owner decision D7) but needs
its own green stg contract report. Any other WOOW prod values change has to be
mirrored into deploy/apporo-k3s.yaml in the same PR, or listed here and in the
runbook as a new documented difference.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.contract import report

ROOT = Path(__file__).resolve().parents[2]
APPORO = ROOT / 'deploy/apporo-k3s.yaml'
PROD = ROOT / 'deploy/woow-k3s.yaml'
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


def proxy_scalar(text, key):
    """Top-level ``proxy.<key>`` from simple YAML text ('' when unset)."""
    section = None
    for line in text.splitlines():
        body = line.split('#', 1)[0].rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        name, _, value = body.strip().partition(':')
        if indent == 0:
            section = name
        elif section == 'proxy' and indent == 2 and name == key:
            return value.strip().strip('\'"')
    return ''


def render(values, extra=None):
    args = ['helm', 'template', 'newapi', str(ROOT / 'chart'), '-n', 'new-api-apporo', '-f', str(values)]
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as tmp:
        if extra is not None:
            json.dump(extra, tmp)
            tmp.flush()
            args += ['-f', tmp.name]
        proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    # Offline Helm generates random Secret bytes (templates/secret.yaml): keep
    # the key names, drop the values.
    return re.sub(rf'(?m)^(  (?:{SECRET_KEYS}):).*$', r'\1 <redacted>', proc.stdout)


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

    def test_render_is_prod_plus_documented_differences(self):
        pin = report.image_pin(self.text, 'proxy')
        documented = {
            'publicUrl': 'https://newapi.apporo.ai',
            'newapi': {'env': {'DEFAULT_LANGUAGE': 'en'}},
            'proxy': {
                'clients': APUN_CLIENTS,
                'apExpiresAt': proxy_scalar(self.text, 'apExpiresAt'),
                'opExpiresAt': proxy_scalar(self.text, 'opExpiresAt'),
                'image': {'tag': pin['tag'] or '', 'digest': pin['digest'] or ''},
            },
        }
        self.assertEqual(render(APPORO), render(PROD, documented),
                         'deploy/apporo-k3s.yaml must equal deploy/woow-k3s.yaml plus the documented '
                         'Apporo differences (docs/APPORO-K3S.md): mirror the WOOW prod change')

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

    def test_pat_expiry_is_placeholder_or_unix_seconds(self):
        for key in ('apExpiresAt', 'opExpiresAt'):
            with self.subTest(key=key):
                self.assertRegex(proxy_scalar(self.text, key), r'^(|[0-9]{10})$')

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
