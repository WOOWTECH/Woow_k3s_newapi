"""scripts/apporo_checks.py: which changes get Apporo's checks strictly, and what warn-only means.

Run inside the "values" check of scripts/apporo_checks.py (APPORO_CHECKS=1),
like test_apporo_values.py; skipped anywhere else. Offline: git in temporary
repositories, helm template of the committed files.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.environ.get('APPORO_CHECKS') != '1':
    raise unittest.SkipTest('Apporo checks run in their own CI step, scripts/apporo_checks.py (strict for a change '
                            'to an Apporo file, warn-only otherwise); locally: scripts/apporo_checks.py --only values')

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'scripts/apporo_checks.py'
_spec = importlib.util.spec_from_file_location('apporo_checks', TOOL)
checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checks)

# Paths of this repository that are WOOW's (or shared): a change to them alone is warn-only.
WOOW_PATHS = ('chart/values.yaml', 'chart/templates/proxy.yaml', 'chart/files/maintenance/retention.sh',
              'deploy/woow-k3s.yaml', 'deploy/woow-k3s-stg.yaml', 'deploy/litellm-from-newapi-ingress.yaml',
              'deploy/litellm-from-newapi-stg-ingress.yaml', 'deploy/settings.json', 'deploy/pricing/channels.json',
              '.gitea/workflows/ci.yml', 'README.md', 'docs/OPERATIONS.md', 'scripts/chart-guards.sh',
              'scripts/check-drift.sh', 'scripts/newapi_admin.py', 'scripts/tests/test_check_drift.py',
              'tests/contract/gate_check.py', 'tests/contract/reports/v1.0.0-rc.44_3947d2b08d08.json')


class Repo:
    """A throwaway git repository with a main branch."""

    def __init__(self, tmp):
        self.path = Path(tmp)
        self.git('init', '-q', '-b', 'main')
        for path in ('chart/values.yaml', 'deploy/woow-k3s.yaml', 'deploy/apporo-k3s.yaml', 'docs/APPORO-K3S.md',
                     'README.md'):
            self.write(path, f'{path}\n')
        self.commit('base')

    def git(self, *args):
        proc = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@localhost', '-c', 'commit.gpgsign=false',
                               *args], cwd=self.path, text=True, capture_output=True)
        if proc.returncode:
            raise AssertionError(f'git {args}: {proc.stderr}')
        return proc.stdout.strip()

    def write(self, path, text):
        (self.path / path).parent.mkdir(parents=True, exist_ok=True)
        (self.path / path).write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)

    def changed(self, base_ref):
        return checks.changed_paths(base_ref, cwd=self.path)[1]

    def strict(self, base_ref):
        args = type('Args', (), {'strict': False, 'warn_only': False, 'base_ref': base_ref})
        return checks.mode_line(args, cwd=self.path)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(tmp.name)
        self.repo.git('checkout', '-q', '-b', 'pr')

    def test_apporo_paths_are_the_ones_containing_apporo(self):
        files = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=ROOT,
                               text=True, capture_output=True).stdout.split()
        apporo = sorted(path for path in files if checks.is_apporo(path))
        for want in ('deploy/apporo-k3s.yaml', 'deploy/litellm-from-newapi-apporo-ingress.yaml',
                     'deploy/apun-odoo-to-newapi-apporo-egress.yaml', 'docs/APPORO-K3S.md',
                     'scripts/apporo_checks.py', 'scripts/apporo_parity.py', 'scripts/tests/test_apporo_checks.py',
                     'scripts/tests/test_apporo_values.py'):
            self.assertIn(want, apporo)
        for path in WOOW_PATHS:
            with self.subTest(path=path):
                self.assertFalse(checks.is_apporo(path))
        self.assertTrue(checks.is_apporo('deploy/apporo-sync/env.example'))  # PR #45's files count too

    def test_woow_only_change_is_warn_only(self):
        self.repo.write('chart/values.yaml', 'a chart change\n')
        self.repo.write('deploy/woow-k3s.yaml', 'a WOOW change\n')
        self.repo.commit('woow')
        strict, why = self.repo.strict('main')
        self.assertFalse(strict)
        self.assertIn('none of the 2 files this change touches', why)
        self.assertIn('Apporo never fails such a change', why)

    def test_apporo_values_change_is_strict(self):
        self.repo.write('chart/values.yaml', 'a chart change\n')
        self.repo.write('deploy/apporo-k3s.yaml', 'an Apporo change\n')
        self.repo.commit('both')
        strict, why = self.repo.strict('main')
        self.assertTrue(strict)
        self.assertIn('touches Apporo files', why)
        self.assertIn('deploy/apporo-k3s.yaml', why)
        self.assertNotIn('chart/values.yaml', why)

    def test_apporo_docs_alone_are_strict(self):
        self.repo.write('docs/APPORO-K3S.md', 'a typo fixed\n')
        self.repo.commit('docs')
        self.assertTrue(self.repo.strict('main')[0])

    def test_deleting_or_renaming_an_apporo_file_is_strict(self):
        self.repo.git('rm', '-q', 'deploy/apporo-k3s.yaml')
        self.repo.commit('delete')
        self.assertEqual(self.repo.changed('main'), ['deploy/apporo-k3s.yaml'])
        self.assertTrue(self.repo.strict('main')[0])
        self.repo.git('reset', '-q', '--hard', 'main')
        self.repo.git('mv', 'docs/APPORO-K3S.md', 'docs/OTHER.md')
        self.repo.commit('rename')
        self.assertEqual(self.repo.changed('main'), ['docs/APPORO-K3S.md', 'docs/OTHER.md'])
        self.assertTrue(self.repo.strict('main')[0])

    def test_uncommitted_and_untracked_apporo_files_count_locally(self):
        self.repo.write('README.md', 'edited, not committed\n')
        self.assertFalse(self.repo.strict('main')[0])
        self.repo.write('scripts/apporo_new_tool.py', 'untracked\n')
        self.assertEqual(self.repo.changed('main'), ['README.md', 'scripts/apporo_new_tool.py'])
        self.assertTrue(self.repo.strict('main')[0])

    def test_changes_merged_to_the_base_after_the_branch_point_are_not_this_change(self):
        # main moves on with an Apporo change; this branch, started earlier,
        # changes only WOOW: compared with main's tip it would look like an
        # Apporo change, compared with the merge base it is not.
        self.repo.write('chart/values.yaml', 'a chart change\n')
        self.repo.commit('woow on the branch')
        self.repo.git('checkout', '-q', 'main')
        self.repo.write('deploy/apporo-k3s.yaml', 'merged to main meanwhile\n')
        self.repo.commit('apporo on main')
        self.repo.git('checkout', '-q', 'pr')
        self.assertIn('deploy/apporo-k3s.yaml', self.repo.git('diff', '--name-only', 'main').split())
        self.assertEqual(self.repo.changed('main'), ['chart/values.yaml'])
        self.assertFalse(self.repo.strict('main')[0])
        # The branch merges main in: still only its own change.
        self.repo.git('merge', '-q', '--no-edit', 'main')
        self.assertEqual(self.repo.changed('main'), ['chart/values.yaml'])

    def test_push_to_main_compares_with_the_previous_main_commit(self):
        # CI on a push to main uses base HEAD~1: the merged change decides.
        self.repo.write('chart/values.yaml', 'a chart change\n')
        self.repo.commit('woow')
        self.repo.git('checkout', '-q', 'main')
        self.repo.git('merge', '-q', '--no-ff', '--no-edit', 'pr')
        self.assertFalse(self.repo.strict('HEAD~1')[0])
        self.repo.git('checkout', '-q', '-b', 'pr2')
        self.repo.write('deploy/apporo-k3s.yaml', 'an Apporo change\n')
        self.repo.commit('apporo')
        self.repo.git('checkout', '-q', 'main')
        self.repo.git('merge', '-q', '--no-ff', '--no-edit', 'pr2')
        self.assertTrue(self.repo.strict('HEAD~1')[0])

    def test_missing_base_ref_is_an_error_not_a_mode(self):
        with self.assertRaises(checks.UsageError) as caught:
            self.repo.strict('origin/main')
        self.assertIn('not found', str(caught.exception))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = checks.main(['--base-ref', 'refs/heads/no-such-branch-for-apporo-checks'], checks=())
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn('::error title=Apporo checks::', out.getvalue())

    def test_flags_decide_without_a_base(self):
        args = type('Args', (), {'strict': True, 'warn_only': False, 'base_ref': 'ignored-ref'})
        self.assertTrue(checks.mode_line(args, cwd=self.repo.path)[0])
        args.strict, args.warn_only = False, True
        self.assertFalse(checks.mode_line(args, cwd=self.repo.path)[0])
        args.warn_only, args.base_ref = False, None
        self.assertFalse(checks.mode_line(args, cwd=self.repo.path)[0])


def fake(status, message):
    return lambda state: (status, [message])


def crash(state):
    raise RuntimeError('boom')


class ModeTests(unittest.TestCase):
    def main(self, *argv, which=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = checks.main(list(argv), checks=which)
        return rc, out.getvalue()

    def test_warn_only_failure_is_a_warning_and_exit_0(self):
        rc, out = self.main('--warn-only', which=(('a', 'A', fake('ok', 'fine')), ('b', 'B', fake('fail', 'broken'))))
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING (not blocking) Apporo checks failed: b.', out)
        self.assertRegex(out, r'(?m)^::warning file=deploy/apporo-k3s\.yaml,title=Apporo b \(not blocking\)::broken')
        self.assertNotIn('::error', out)

    def test_strict_failure_is_an_error_and_exit_1(self):
        rc, out = self.main('--strict', which=(('a', 'A', fake('ok', 'fine')), ('b', 'B', fake('fail', 'broken'))))
        self.assertEqual(rc, 1, out)
        self.assertIn('FAILED  Apporo checks: b.', out)
        self.assertRegex(out, r'(?m)^::error file=deploy/apporo-k3s\.yaml,title=Apporo b::broken$')
        self.assertNotIn('::warning', out)

    def test_a_crashing_check_never_turns_warn_only_red(self):
        rc, out = self.main('--warn-only', which=(('c', 'C', crash),))
        self.assertEqual(rc, 0, out)
        self.assertIn('the check itself crashed', out)
        self.assertIn('RuntimeError: boom', out)
        rc, out = self.main('--strict', which=(('c', 'C', crash),))
        self.assertEqual(rc, 1, out)

    def test_all_passed_prints_no_annotation(self):
        rc, out = self.main('--strict', which=(('a', 'A', fake('ok', 'fine')), ('s', 'S', fake('skip', 'n/a'))))
        self.assertEqual(rc, 0, out)
        self.assertIn('all Apporo checks passed', out)
        self.assertNotIn('::', out)

    def test_sub_tool_annotations_are_defused(self):
        quoted = checks.quoted('ok\n::error file=x,title=t::boom\n  ::warning::w\n')
        self.assertFalse(any(line.lstrip().startswith('::') for line in quoted.splitlines()), quoted)
        self.assertIn(': :error file=x,title=t::boom', quoted)
        rc, out = self.main('--warn-only', which=(('p', 'P', fake('fail', 'parity\n' + quoted)),))
        self.assertEqual(rc, 0, out)
        self.assertEqual(re.findall(r'(?m)^::(\w+)', out), ['warning'])

    def test_only_runs_a_subset_and_rejects_unknown_names(self):
        rc, out = self.main('--strict', '--only', 'a', which=(('a', 'A', fake('ok', 'fine')),
                                                              ('b', 'B', fake('fail', 'broken'))))
        self.assertEqual(rc, 0, out)
        self.assertNotIn('-- b:', out)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.main('--only', 'nope', which=(('a', 'A', fake('ok', 'fine')),))

    def test_strict_and_warn_only_exclude_each_other(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.main('--strict', '--warn-only')

    def test_values_check_never_recurses(self):
        with mock.patch.dict(os.environ, {checks.NESTED_ENV: '1'}):
            status, messages = checks.Checks(strict=True).values_check()
        self.assertEqual(status, 'skip', messages)


class ProxyObjectsTests(unittest.TestCase):
    """proxy_problems(): the newapi-proxy check WOOW's renders get, on synthetic renders."""

    PIN = 'jcr-prod.woowtech.io/woow-paas-docker-local/newapi-proxy:' + 'a' * 40 + '@sha256:' + 'b' * 64

    def render(self, kinds=checks.PROXY_KINDS, images=(PIN,)):
        docs = [f'kind: {kind}\nmetadata:\n  name: newapi-proxy\n' for kind in kinds]
        docs += [f'kind: Deployment\nmetadata:\n  name: other\nspec:\n  containers:\n    - name: c\n'
                 f'      image: {image}\n' for image in images]
        return '---\n'.join(docs)

    def test_complete_proxy(self):
        self.assertEqual(checks.proxy_problems(self.render()), [])

    def test_missing_kind_or_bad_image(self):
        self.assertEqual(checks.proxy_problems(self.render(kinds=checks.PROXY_KINDS[:-1])),
                         ['no newapi-proxy NetworkPolicy'])
        self.assertTrue(checks.proxy_problems(self.render(images=())))
        self.assertTrue(checks.proxy_problems(self.render(images=(self.PIN, self.PIN))))
        self.assertTrue(checks.proxy_problems(self.render(images=(self.PIN.replace('a' * 40, 'latest'),))))
        zero = self.PIN.replace('b' * 64, '0' * 64)
        self.assertEqual(checks.proxy_problems(self.render(images=(zero,))), ['renders the placeholder proxy digest'])


class CommittedFilesTests(unittest.TestCase):
    def test_render_and_proxy_checks_pass_on_the_committed_files(self):
        proc = subprocess.run([sys.executable, str(TOOL), '--strict', '--only', 'render,proxy-on,images,proxy'],
                              cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('all Apporo checks passed', proc.stdout)

    def test_ci_runs_apporo_only_in_its_own_step(self):
        # WOOW's steps never look at Apporo; the Apporo step resolves the base
        # like the contract gate and passes it on.
        text = '\n'.join(line for line in (ROOT / '.gitea/workflows/ci.yml').read_text().splitlines()
                         if not line.lstrip().startswith('#'))
        steps = re.split(r'(?m)^      - (?=name: |uses: )', text)[1:]
        apporo = [step for step in steps if 'apporo' in step.lower()]
        self.assertEqual(len(apporo), 1, 'only one CI step may mention Apporo')
        step = apporo[0]
        self.assertTrue(step.startswith('name: Apporo checks'), step)
        self.assertIn('base="origin/${{ github.base_ref }}"', step)
        self.assertIn('base="HEAD~1"', step)
        self.assertIn('python3 scripts/apporo_checks.py --base-ref "$base"', step)
        self.assertNotIn('continue-on-error', step)


if __name__ == '__main__':
    unittest.main()
