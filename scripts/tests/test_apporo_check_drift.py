"""scripts/apporo_check_drift.sh, Apporo's drift check, against a fake kubectl (no cluster).

Run inside the "values" check of scripts/apporo_checks.py (APPORO_CHECKS=1),
like the other test_apporo_*.py modules; skipped anywhere else, such as WOOW's
"Admin tool unit tests" step: a WOOW change to scripts/check-drift.sh that
breaks the wrapper only gets a warning (owner decision 2026-11-17), and an
Apporo PR follows.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

if os.environ.get('APPORO_CHECKS') != '1':
    raise unittest.SkipTest('Apporo checks run in their own CI step, scripts/apporo_checks.py (strict for a change '
                            'to an Apporo file, warn-only otherwise); locally: scripts/apporo_checks.py --only values')

ROOT = Path(__file__).resolve().parents[2]
WOOW = ['deploy/litellm-from-newapi-ingress.yaml', 'deploy/litellm-from-newapi-stg-ingress.yaml']
APPORO = ['deploy/litellm-from-newapi-apporo-ingress.yaml', 'deploy/apun-odoo-to-newapi-apporo-egress.yaml']
APPORO_HINT = ('(cluster differs from git, or not applied yet; Apporo object: apply only as docs/APPORO-K3S.md '
               '〈手動套用的物件〉 says — order and owner consent)')
# `kubectl diff` exits 1 for a file listed in FAKE_DRIFT (an object that differs
# or was never applied: the server-side dry run would create it), 2 for one in
# FAKE_ERROR, 0 otherwise.
FAKE_KUBECTL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" diff -f "*)
    all="$*"
    f=${all##* -f }
    case " $FAKE_ERROR " in
      *" $f "*) echo "error: the server could not find the requested resource"; exit 2 ;;
    esac
    case " $FAKE_DRIFT " in
      *" $f "*) echo "+  kind: NetworkPolicy (would be created)"; exit 1 ;;
    esac
    exit 0 ;;
  *) exit 3 ;;
esac
'''


class ApporoCheckDriftTests(unittest.TestCase):
    def run_drift(self, script, *args, drift=(), error=()):
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / 'kubectl'
            kubectl.write_text(FAKE_KUBECTL)
            kubectl.chmod(0o755)
            log = Path(tmp) / 'calls'
            env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', FAKE_LOG=str(log),
                       FAKE_DRIFT=' '.join(drift), FAKE_ERROR=' '.join(error))
            proc = subprocess.run(['bash', str(ROOT / script), *args], text=True, capture_output=True, env=env)
            calls = log.read_text().splitlines() if log.exists() else []
        if '--context' in args:
            self.assertTrue(all(call.startswith('--context woow-k3s ') for call in calls), calls)
        # Read-only: nothing but `kubectl diff`, one call per file.
        self.assertTrue(all(' diff -f ' in f' {call}' for call in calls), calls)
        return proc, [call.split('diff -f ', 1)[1] for call in calls]

    def apporo(self, *args, **kw):
        return self.run_drift('scripts/apporo_check_drift.sh', *args, **kw)

    def test_default_run_checks_the_two_apporo_files(self):
        proc, diffed = self.apporo('--context', 'woow-k3s')
        self.assertEqual((proc.returncode, diffed), (0, APPORO), proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, ''.join(f'ok     {f}\n' for f in APPORO))

    def test_missing_apporo_object_is_drift_without_a_bare_re_apply(self):
        proc, _ = self.apporo('--context', 'woow-k3s', drift=[APPORO[1]])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f'ok     {APPORO[0]}\n', proc.stdout)
        self.assertIn(f'DRIFT  {APPORO[1]} {APPORO_HINT}\n', proc.stdout)
        self.assertIn('  +  kind: NetworkPolicy (would be created)', proc.stdout)  # the diff, as check-drift.sh prints it
        self.assertNotIn('re-apply', proc.stdout)
        self.assertNotIn('kubectl apply', proc.stdout)

    def test_explicit_file_only_and_with_the_apporo_hint(self):
        # Mid-install: only the object already applied.
        proc, diffed = self.apporo('--context', 'woow-k3s', APPORO[0], drift=[APPORO[0]])
        self.assertEqual((proc.returncode, diffed), (1, [APPORO[0]]), proc.stdout)
        self.assertIn(f'DRIFT  {APPORO[0]} {APPORO_HINT}\n', proc.stdout)
        self.assertNotIn('re-apply', proc.stdout)

    def test_kubectl_errors_pass_through(self):
        proc, _ = self.apporo('--context', 'woow-k3s', drift=[APPORO[0]], error=[APPORO[1]])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn(f'DRIFT  {APPORO[0]} {APPORO_HINT}\n', proc.stdout)
        self.assertIn(f'ERROR  {APPORO[1]} (kubectl diff exit 2)', proc.stdout)

    def test_woow_default_run_never_looks_at_apporo(self):
        # The WOOW triage run (docs/OPERATIONS.md) stays WOOW's whatever state Apporo's
        # install is in: new-api-apporo installed, apun egress not applied yet.
        proc, diffed = self.run_drift('scripts/check-drift.sh', '--context', 'woow-k3s', drift=APPORO)
        self.assertEqual((proc.returncode, diffed), (0, WOOW), proc.stdout)
        self.assertNotIn('apporo', proc.stdout)


if __name__ == '__main__':
    unittest.main()
