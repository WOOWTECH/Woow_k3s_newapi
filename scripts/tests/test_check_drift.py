"""scripts/check-drift.sh default file list, against a fake kubectl (no cluster)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WOOW = ['deploy/litellm-from-newapi-ingress.yaml', 'deploy/litellm-from-newapi-stg-ingress.yaml']
APPORO = ['deploy/litellm-from-newapi-apporo-ingress.yaml', 'deploy/apun-odoo-to-newapi-apporo-egress.yaml']
FAKE_KUBECTL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" get namespace new-api-apporo "*)
    case "$FAKE_NS" in
      present) echo namespace/new-api-apporo ;;
      absent) echo 'Error from server (NotFound): namespaces "new-api-apporo" not found' >&2; exit 1 ;;
      *) echo 'The connection to the server was refused' >&2; exit 1 ;;
    esac ;;
  *" diff "*) exit 0 ;;
  *) exit 3 ;;
esac
'''


class CheckDriftDefaultsTests(unittest.TestCase):
    def run_drift(self, namespace, *files):
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / 'kubectl'
            kubectl.write_text(FAKE_KUBECTL)
            kubectl.chmod(0o755)
            log = Path(tmp) / 'calls'
            env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', FAKE_LOG=str(log), FAKE_NS=namespace)
            proc = subprocess.run(['bash', str(ROOT / 'scripts/check-drift.sh'), '--context', 'woow-k3s', *files],
                                  text=True, capture_output=True, env=env)
            calls = log.read_text().splitlines() if log.exists() else []
        self.assertTrue(all(call.startswith('--context woow-k3s ') for call in calls), calls)
        return proc, [call.split(' diff -f ', 1)[1] for call in calls if ' diff -f ' in call]

    def test_before_the_apporo_install_only_woow_files(self):
        proc, diffed = self.run_drift('absent')
        self.assertEqual((proc.returncode, diffed), (0, WOOW), proc.stdout)
        self.assertIn('skip   Apporo files', proc.stdout)

    def test_apporo_files_once_its_namespace_exists(self):
        proc, diffed = self.run_drift('present')
        self.assertEqual((proc.returncode, diffed), (0, WOOW + APPORO), proc.stdout)

    def test_namespace_lookup_error_still_checks_woow_and_exits_2(self):
        proc, diffed = self.run_drift('error')
        self.assertEqual((proc.returncode, diffed), (2, WOOW), proc.stdout)
        self.assertIn('ERROR  kubectl get namespace new-api-apporo', proc.stdout)

    def test_explicit_files_skip_the_namespace_lookup(self):
        proc, diffed = self.run_drift('error', APPORO[1])
        self.assertEqual((proc.returncode, diffed), (0, [APPORO[1]]), proc.stdout)
        self.assertNotIn('Apporo files', proc.stdout)


if __name__ == '__main__':
    unittest.main()
