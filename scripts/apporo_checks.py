#!/usr/bin/env python3
"""Every check of Apporo's new-api (deploy/apporo-k3s.yaml), in one place.

Owner decision 2026-11-17: Apporo never fails a WOOW pull request. CI runs
these checks in a step of their own ("Apporo checks", .gitea/workflows/ci.yml)
and none of WOOW's steps runs an Apporo check. Two of them read Apporo's files
as files ("Scripts parse" compiles scripts/apporo_*.py and the Apporo tests,
"No placeholder image digests" greps deploy/*.yaml), so a change that touches
no Apporo file leaves their verdict on those files as it was on the base; the
Apporo test modules skip themselves in "Admin tool unit tests".

- A change that touches no Apporo file runs them warn-only: every failure is
  printed with a ::warning:: and the step stays green. That covers a WOOW chart
  change after which Apporo's committed file no longer renders, or its dormant
  proxy settings no longer pass the chart's guards, and a new-api image bump
  without a green stg report for Apporo's (lagging) proxy pin.
- A change that adds, edits or deletes an Apporo file runs them strictly: any
  failure fails the step. Apporo's files are the paths that contain "apporo"
  (any case): deploy/apporo-k3s.yaml, deploy/litellm-from-newapi-apporo-ingress.yaml,
  deploy/apun-odoo-to-newapi-apporo-egress.yaml, scripts/apporo_* (the drift
  check scripts/apporo_check_drift.sh too), scripts/tests/test_apporo_*.py,
  docs/APPORO-*.md. Such a change also has to
  leave Apporo in step with WOOW prod (parity below): a gap a WOOW change left
  behind is closed in the next Apporo change, or written down as a documented
  difference.

What the change touches is found like tests/contract/gate_check.py finds an
image bump, by comparing the base's committed files with the working tree,
here from the merge base of --base-ref and HEAD: commits that reached the base
branch after this branch started are not this change.

  scripts/apporo_checks.py --base-ref origin/main   # CI: strict only for a change to Apporo files
  scripts/apporo_checks.py --strict                 # before an Apporo install or helm upgrade; the daily sync
  scripts/apporo_checks.py                          # report only (warn-only)

The checks (docs/APPORO-K3S.md 〈CI 與檢查〉):
  render    helm lint, and helm template in new-api-apporo, of the file as committed
  proxy-on  while the proxy is dormant: the same with proxy.enabled true (the chart's
            proxy guards and objects; the activation PR needs it)
  images    every image of the committed render is pinned by digest
  proxy     newapi-proxy objects: none while dormant; when on, all five kinds and
            one image pinned by commit tag and digest
  values    Apporo's own decided values and what its AI terms state about the
            gateway, and these tools' own tests (the drift check's too):
            scripts/tests/test_apporo_*.py
  parity    everything else equals WOOW prod: scripts/apporo_parity.py --strict

Offline: git, helm template and python only; no cluster, credentials or network.
Exit: 0 every check passed, or warn-only; 1 a check failed in strict mode;
2 usage, git or base ref error.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APPORO = 'deploy/apporo-k3s.yaml'
NAMESPACE = 'new-api-apporo'
# A path containing this (any case) is Apporo's: a change to it gets the strict checks.
APPORO_MARK = 'apporo'
PROXY_KINDS = ('ConfigMap', 'Service', 'Deployment', 'PodDisruptionBudget', 'NetworkPolicy')
PROXY_IMAGE = re.compile(r'^\s+image: .*woow-paas-docker-local/newapi-proxy:')
PINNED_PROXY_IMAGE = re.compile(r'newapi-proxy:[0-9a-f]{40}@sha256:[0-9a-f]{64}"?$')
# The values check runs scripts/tests/test_apporo_*.py with APPORO_CHECKS=1: those
# modules skip themselves anywhere else (WOOW's "Admin tool unit tests" step).
TESTS_ENV = 'APPORO_CHECKS'
# Set inside that run, so a test that starts this script never recurses into it.
NESTED_ENV = 'APPORO_CHECKS_NESTED'
DETAIL_LINES = 80
HINT = ('Bring Apporo up to date in a PR of its own (docs/APPORO-K3S.md 〈跟著 WOOW 改〉); '
        'scripts/apporo_checks.py --strict must pass before any Apporo install or helm upgrade.')
STRICT_HINT = ('Fix them in this change: a gap WOOW left behind is closed here too, or written down as a '
               'documented difference (docs/APPORO-K3S.md 〈CI 與檢查〉).')
INSTALL_HINT = ('Do not install or helm upgrade Apporo until they pass: follow WOOW prod in an Apporo PR first '
                '(docs/APPORO-K3S.md 〈跟著 WOOW 改〉).')


class UsageError(Exception):
    pass


def run(cmd, env=None, cwd=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, text=True, capture_output=True, env=env)


def git(*args, cwd=None):
    proc = run(['git', *args], cwd=cwd)
    if proc.returncode:
        raise UsageError(f'git {" ".join(args)}: {proc.stderr.strip() or "exit %d" % proc.returncode}')
    return proc.stdout


def is_apporo(path):
    return APPORO_MARK in path.lower()


def changed_paths(base_ref, cwd=None):
    """(merge base, paths added, edited or deleted since it), working tree included."""
    if run(['git', 'rev-parse', '--verify', '-q', base_ref + '^{commit}'], cwd=cwd).returncode:
        raise UsageError(f'base ref {base_ref} not found (fetch it with full history, as the contract gate does)')
    base = git('merge-base', base_ref, 'HEAD', cwd=cwd).strip()
    tracked = git('diff', '--name-only', '--no-renames', '-z', base, '--', cwd=cwd).split('\0')
    # Untracked files count too (a local run before the commit), except bytecode
    # caches: CI's earlier steps compile scripts/apporo_*.py, and a cache must
    # never turn a WOOW change into an Apporo one, .gitignore or not.
    untracked = [path for path in git('ls-files', '--others', '--exclude-standard', '-z', cwd=cwd).split('\0')
                 if '__pycache__/' not in path and not path.endswith('.pyc')]
    return base, sorted({path for path in tracked + untracked if path})


def first_line(text):
    lines = [line for line in str(text).splitlines() if line.strip()]
    return lines[0].strip() if lines else str(text)


def one_line(text):
    """A workflow-command message: one line, % CR LF escaped."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def quoted(text, limit=DETAIL_LINES):
    """Sub-tool output, indented. A workflow command in it (::error:: and the like)
    is defused: only this script decides what is an error and what a warning."""
    lines = text.rstrip('\n').splitlines()
    out = []
    for line in lines[:limit]:
        if line.lstrip().startswith('::'):
            line = line.replace('::', ': :', 1)
        out.append('   | ' + line)
    if len(lines) > limit:
        out.append(f'   | ... ({len(lines) - limit} more lines)')
    return '\n'.join(out)


def helm_template(*extra):
    return run(['helm', 'template', 'newapi', './chart', '-n', NAMESPACE, '-f', APPORO, *extra])


def documents(render):
    return re.split(r'(?m)^---\s*$', render)


def proxy_state(text):
    """(enabled, error): proxy.enabled as CI reads it (tests/contract/gate_check.proxy_enabled, fail-closed).

    Imported here, not at the top: the contract tools are WOOW's, and a change
    to them must leave a warn-only run able to report instead of crashing."""
    try:
        from tests.contract import gate_check
        return bool(gate_check.proxy_enabled(text)), None
    except Exception as err:
        return None, f'cannot tell dormant from active: tests/contract/gate_check.proxy_enabled failed ({err!r})'


def proxy_problems(render):
    """Why ``render`` lacks a working newapi-proxy ([] = it has one): the CI check WOOW's renders get."""
    out = []
    docs = documents(render)
    for kind in PROXY_KINDS:
        if not any(re.search(rf'(?m)^kind: {kind}$', doc) and re.search(r'(?m)^  name: newapi-proxy$', doc)
                   for doc in docs):
            out.append(f'no newapi-proxy {kind}')
    images = [line for line in render.splitlines() if PROXY_IMAGE.match(line)]
    if len(images) != 1 or not PINNED_PROXY_IMAGE.search(images[0]):
        out.append('must render one newapi-proxy image with a commit tag and full digest, got '
                   + (', '.join(line.strip() for line in images) or 'none'))
    elif '@sha256:' + '0' * 64 in images[0]:
        out.append('renders the placeholder proxy digest')
    return out


class Checks:
    """The checks, in order. Each returns (status, [messages]); status is ok, fail or skip."""

    def __init__(self, strict):
        self.strict = strict
        self.render = None
        try:
            text = (ROOT / APPORO).read_text()
        except OSError as err:
            self.enabled, self.enabled_error = None, f'{APPORO} cannot be read: {err}'
        else:
            self.enabled, self.enabled_error = proxy_state(text)

    def render_check(self):
        out = []
        lint = run(['helm', 'lint', './chart', '-f', APPORO])
        if lint.returncode:
            out.append(f'helm lint fails:\n{quoted(lint.stdout + lint.stderr)}')
        tpl = helm_template()
        if tpl.returncode:
            out.append(f'{APPORO} does not render in {NAMESPACE} as committed: {first_line(tpl.stderr)}')
        else:
            self.render = tpl.stdout
        if out:
            return 'fail', out
        return 'ok', [f'renders in {NAMESPACE} as committed ({len(re.findall(r"(?m)^kind:", self.render))} objects)']

    def proxy_on_check(self):
        if self.enabled is None:
            return 'skip', [self.enabled_error or 'proxy.enabled unknown']
        if self.enabled:
            return 'skip', ['the proxy is on: the committed render is checked below']
        if self.render is None:
            return 'skip', ['needs the committed render']
        with tempfile.NamedTemporaryFile('w', suffix='.yaml') as on:
            on.write('proxy:\n  enabled: true\n')
            on.flush()
            tpl = helm_template('-f', on.name)
        if tpl.returncode:
            return 'fail', [f'dormant now, but would not render with the proxy switched on (the activation PR '
                            f'needs it): {first_line(tpl.stderr)}']
        problems = proxy_problems(tpl.stdout)
        if problems:
            return 'fail', [f'with the proxy switched on: {p}' for p in problems]
        return 'ok', ['renders with the proxy switched on, all newapi-proxy objects and a pinned image']

    def images_check(self):
        if self.render is None:
            return 'skip', ['needs the committed render']
        bad = [line.strip() for line in self.render.splitlines()
               if re.match(r'^\s+image: ', line) and '@sha256:' not in line]
        if bad:
            return 'fail', ['images not pinned by digest: ' + ', '.join(bad)]
        return 'ok', ['every image pinned by digest']

    def proxy_check(self):
        if self.render is None:
            return 'skip', ['needs the committed render']
        if self.enabled is None:
            return 'fail', [self.enabled_error or 'proxy.enabled unknown']
        if self.enabled:
            problems = proxy_problems(self.render)
            if problems:
                return 'fail', problems
            return 'ok', ['proxy on: all newapi-proxy objects and a pinned image']
        if re.search(r'(?m)^  name: newapi-proxy$', self.render):
            return 'fail', [f'{APPORO} is dormant (proxy.enabled: false) but renders newapi-proxy objects']
        return 'ok', ['dormant until its activation PR: no newapi-proxy objects']

    def values_check(self):
        if os.environ.get(NESTED_ENV):
            return 'skip', ['already inside the values check']
        env = dict(os.environ, **{TESTS_ENV: '1', NESTED_ENV: '1'})
        proc = run([sys.executable, '-m', 'unittest', 'discover', '-s', 'scripts/tests',
                    '-p', 'test_apporo_*.py', '-v'], env=env)
        output = proc.stdout + proc.stderr
        ran = re.findall(r'(?m)^Ran (\d+) tests?', output)
        failed = re.findall(r'(?m)^(?:FAIL|ERROR): (\S+)', output)
        if proc.returncode or not ran:
            body = output.split('\n' + '=' * 70 + '\n', 1)
            details = ('=' * 70 + '\n' + body[1]) if len(body) == 2 else output
            return 'fail', [f'{len(failed)} of {ran[-1] if ran else "?"} tests failed: '
                            + (', '.join(failed) or first_line(output)) + '\n' + quoted(details, 200)]
        skipped = re.search(r'skipped=(\d+)', output.rsplit('Ran ', 1)[-1])
        return 'ok', [f'{ran[-1]} tests passed' + (f' ({skipped.group(1)} skipped)' if skipped else '')]

    def parity_check(self):
        proc = run([sys.executable, 'scripts/apporo_parity.py', '--strict'])
        output = (proc.stdout + proc.stderr).rstrip('\n')
        if proc.returncode:
            what = 'differs from WOOW prod + the documented differences' if proc.returncode == 1 else 'error'
            return 'fail', [f'scripts/apporo_parity.py --strict: {what} (exit {proc.returncode})\n{quoted(output)}']
        return 'ok', [first_line(output.splitlines()[-1]) if output else 'in sync']


CHECKS = (
    ('render', 'helm lint and helm template of deploy/apporo-k3s.yaml in new-api-apporo, as committed',
     Checks.render_check),
    ('proxy-on', 'while dormant: the same with the proxy switched on', Checks.proxy_on_check),
    ('images', 'images pinned by digest', Checks.images_check),
    ('proxy', 'newapi-proxy objects match proxy.enabled', Checks.proxy_check),
    ('values', "Apporo's own values, the AI terms' promises, these tools' tests (scripts/tests/test_apporo_*.py)",
     Checks.values_check),
    ('parity', 'everything else equals WOOW prod (scripts/apporo_parity.py --strict)', Checks.parity_check),
)


def mode_line(args, cwd=None):
    """(strict, why) from the arguments and, with --base-ref, from what the change touches."""
    if args.strict:
        return True, 'strict (--strict: before an Apporo install or helm upgrade, the daily sync)'
    if args.warn_only or not args.base_ref:
        return False, 'warn-only (report: --base-ref not given)' if not args.warn_only else 'warn-only (--warn-only)'
    base, paths = changed_paths(args.base_ref, cwd)
    apporo = [path for path in paths if is_apporo(path)]
    where = f'against {args.base_ref}, merge base {base[:12]}'
    if apporo:
        shown = ', '.join(apporo[:10]) + (f' (+{len(apporo) - 10} more)' if len(apporo) > 10 else '')
        return True, (f'strict: this change touches Apporo files ({where}): {shown}. Apporo\'s own changes '
                      'are checked strictly (owner decision 2026-11-17)')
    return False, (f'warn-only: none of the {len(paths)} files this change touches ({where}) is an Apporo file '
                   f'(a path containing "{APPORO_MARK}"). Apporo never fails such a change (owner decision 2026-11-17)')


def main(argv=None, checks=CHECKS):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--strict', action='store_true',
                       help='fail on any problem (before an Apporo install or helm upgrade; the daily sync)')
    group.add_argument('--warn-only', action='store_true', help='never fail (report)')
    parser.add_argument('--base-ref', help='CI: strict only when the change since the merge base with this ref '
                                           'touches an Apporo file')
    parser.add_argument('--only', help='comma-separated subset of: ' + ', '.join(name for name, _, _ in checks))
    args = parser.parse_args(argv)
    only = [name.strip() for name in args.only.split(',')] if args.only else None
    unknown = sorted(set(only or ()) - {name for name, _, _ in checks})
    if unknown:
        parser.error('unknown check(s): ' + ', '.join(unknown))
    try:
        strict, why = mode_line(args)
    except (UsageError, OSError) as err:
        print(f'Apporo checks: {err}')
        print(f'::error title=Apporo checks::{one_line(str(err))}')
        return 2
    print(f'Apporo checks ({"strict" if strict else "warn-only"}; docs/APPORO-K3S.md 〈CI 與檢查〉)')
    print(f'mode: {why}')

    try:
        state = Checks(strict)
    except Exception as err:  # never expected; a warn-only run still must not turn red
        state = None
        failed = [('setup', f'the checks could not start: {err!r}')]
        print(f'   FAIL the checks could not start:\n{quoted(traceback.format_exc())}')
    else:
        failed = []
    for name, title, check in checks if state is not None else ():
        if only and name not in only:
            continue
        print(f'\n-- {name}: {title}')
        try:
            status, messages = check(state)
        except Exception:  # a bug here must not turn a warn-only run red
            status, messages = 'fail', ['the check itself crashed:\n' + quoted(traceback.format_exc())]
        for message in messages:
            print(f'   {status.upper():4} {message}')
        if status == 'fail':
            failed.append((name, first_line(messages[0]) if messages else name))

    print()
    if not failed:
        print('all Apporo checks passed')
        return 0
    names = ', '.join(name for name, _ in failed)
    if strict:
        print(f'FAILED  Apporo checks: {names}. {why}. {INSTALL_HINT if args.strict else STRICT_HINT}')
        for name, message in failed:
            print(f'::error file={APPORO},title=Apporo {name}::{one_line(message)}')
        return 1
    print(f'WARNING (not blocking) Apporo checks failed: {names}. {why}. {HINT}')
    for name, message in failed:
        print(f'::warning file={APPORO},title=Apporo {name} (not blocking)::{one_line(message + ". " + HINT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
