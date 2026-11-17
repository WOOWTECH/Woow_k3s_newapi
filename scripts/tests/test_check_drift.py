"""scripts/check-drift.sh file selection and hints, against a fake kubectl (no cluster)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WOOW = ['deploy/litellm-from-newapi-ingress.yaml', 'deploy/litellm-from-newapi-stg-ingress.yaml']
APPORO = ['deploy/litellm-from-newapi-apporo-ingress.yaml', 'deploy/apun-odoo-to-newapi-apporo-egress.yaml']
# `kubectl diff` exits 1 for a file listed in FAKE_DRIFT (an object that differs
# or was never applied: the server-side dry run would create it), 0 otherwise.
FAKE_KUBECTL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" diff -f "*)
    all="$*"
    f=${all##* -f }
    case " $FAKE_DRIFT " in
      *" $f "*) echo "+  kind: NetworkPolicy (would be created)"; exit 1 ;;
    esac
    exit 0 ;;
  *) exit 3 ;;
esac
'''


class CheckDriftTests(unittest.TestCase):
    def run_drift(self, *args, drift=()):
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / 'kubectl'
            kubectl.write_text(FAKE_KUBECTL)
            kubectl.chmod(0o755)
            log = Path(tmp) / 'calls'
            env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', FAKE_LOG=str(log),
                       FAKE_DRIFT=' '.join(drift))
            proc = subprocess.run(['bash', str(ROOT / 'scripts/check-drift.sh'), *args],
                                  text=True, capture_output=True, env=env)
            calls = log.read_text().splitlines() if log.exists() else []
        if '--context' in args:
            self.assertTrue(all(call.startswith('--context woow-k3s ') for call in calls), calls)
        # Read-only: nothing but `kubectl diff`, one call per file.
        self.assertTrue(all(' diff -f ' in f' {call}' for call in calls), calls)
        return proc, [call.split('diff -f ', 1)[1] for call in calls]

    def test_default_run_checks_only_the_woow_files(self):
        proc, diffed = self.run_drift('--context', 'woow-k3s')
        self.assertEqual((proc.returncode, diffed), (0, WOOW), proc.stdout)
        self.assertEqual(proc.stdout, ''.join(f'ok     {f}\n' for f in WOOW))

    def test_default_run_ignores_apporo_objects_that_are_not_applied_yet(self):
        # new-api-apporo installed, apun egress still waiting for the apun switch:
        # the WOOW triage run stays green and never asks for an apun change.
        proc, diffed = self.run_drift('--context', 'woow-k3s', drift=APPORO)
        self.assertEqual((proc.returncode, diffed), (0, WOOW), proc.stdout)
        self.assertNotIn('apporo', proc.stdout)

    def test_woow_drift_keeps_the_re_apply_hint(self):
        proc, _ = self.run_drift('--context', 'woow-k3s', drift=[WOOW[0]])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f'DRIFT  {WOOW[0]} (cluster differs from git; re-apply with: kubectl apply -f {WOOW[0]})',
                      proc.stdout)

    def test_apporo_flag_checks_the_apporo_files(self):
        for args in (('--context', 'woow-k3s', '--apporo'), ('--apporo', '--context', 'woow-k3s')):
            with self.subTest(args=args):
                proc, diffed = self.run_drift(*args)
                self.assertEqual((proc.returncode, diffed), (0, APPORO), proc.stdout)

    def test_missing_apporo_object_is_drift_without_a_bare_re_apply(self):
        proc, _ = self.run_drift('--context', 'woow-k3s', '--apporo', drift=[APPORO[1]])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f'DRIFT  {APPORO[1]} (cluster differs from git, or not applied yet; Apporo object: '
                      'apply only as docs/APPORO-K3S.md', proc.stdout)
        self.assertNotIn('re-apply with', proc.stdout)

    def test_explicit_files_only_and_apporo_hint_applies_there_too(self):
        proc, diffed = self.run_drift('--context', 'woow-k3s', APPORO[0], drift=[APPORO[0]])
        self.assertEqual((proc.returncode, diffed), (1, [APPORO[0]]), proc.stdout)
        self.assertIn('Apporo object: apply only as docs/APPORO-K3S.md', proc.stdout)
        self.assertNotIn('re-apply with', proc.stdout)


if __name__ == '__main__':
    unittest.main()
