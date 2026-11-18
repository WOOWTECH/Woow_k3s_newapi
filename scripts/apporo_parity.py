#!/usr/bin/env python3
"""Does Apporo's new-api (deploy/apporo-k3s.yaml) still follow WOOW prod?

Owner decision 2026-11-17: Apporo follows WOOW prod, but Apporo never blocks a
WOOW pull request. CI runs this with --strict as the "parity" check of
scripts/apporo_checks.py, which fails only a change that touches an Apporo file
and turns the result into a warning for any other change. Nothing closes the
gap a WOOW change leaves automatically yet: the daily sync tool
(scripts/apporo_sync.py, Woow_k3s_newapi PR #45) runs
scripts/apporo_checks.py --strict and alerts, but opens no PR. Bring
deploy/apporo-k3s.yaml up to date in a PR of its own (docs/APPORO-K3S.md
〈跟著 WOOW 改〉), and before every Apporo install or helm upgrade run
scripts/apporo_checks.py --strict, which runs this with --strict and checks
Apporo's own values (scripts/tests/test_apporo_values.py) as well.

Offline: helm template only, no cluster, credentials or network.

- Values: WOOW prod renders in new-api and Apporo in new-api-apporo. The
  Apporo render must equal the WOOW prod render with the documented Apporo
  differences (below; docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉) layered on
  top. While Apporo's proxy is dormant, both are compared again with the proxy
  switched on, so its settings do not fall behind before the activation PR.
- The proxy pin: the render comparison takes Apporo's own pin (it may lag),
  so the pin is compared on its own with WOOW prod's current pin. A different
  pin means Apporo is behind, or WOOW prod rolled back and Apporo now leads.
- Per-instance evidence is never compared (nor copied): encryptedBackup
  capacityVerification and restoreVerification, which the chart binds to the
  release namespace, and ageRecipient.
- deploy/litellm-from-newapi-apporo-ingress.yaml mirrors
  deploy/litellm-from-newapi-ingress.yaml except the name and the source
  namespace.

  scripts/apporo_parity.py [--strict]

Exit: 0 in sync, or behind without --strict (warning printed); 1 behind with
--strict; 2 error. Whether a result blocks a pull request is
scripts/apporo_checks.py's call, not this tool's.
- An Apporo file that cannot be read, or does not render as committed, is an
  error in both modes: that is what helm would deploy.
- While Apporo's proxy is dormant, a render that fails only with the proxy
  switched on is a warning without --strict and an error with it (Apporo cannot
  be activated like that).
- A WOOW side that cannot be rendered is a warning without --strict: the WOOW
  lint/render step reports a broken WOOW file on its own.
"""
import argparse
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'chart'
WOOW = ROOT / 'deploy/woow-k3s.yaml'
APPORO = ROOT / 'deploy/apporo-k3s.yaml'
LITELLM_WOOW = ROOT / 'deploy/litellm-from-newapi-ingress.yaml'
LITELLM_APPORO = ROOT / 'deploy/litellm-from-newapi-apporo-ingress.yaml'
WOOW_NAMESPACE = 'new-api'
APPORO_NAMESPACE = 'new-api-apporo'

# ---- The documented Apporo differences (docs/APPORO-K3S.md 〈和 WOOW 正式站的差異〉).
# scripts/tests/test_apporo_values.py holds deploy/apporo-k3s.yaml to them.
PUBLIC_URL = 'https://newapi.apporo.ai'
# Customer-visible new-api messages in English (owner decision 2026-11-17).
DEFAULT_LANGUAGE = 'en'
# The apun Odoo web pod (component application) and its cron pod.
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
# proxy.addQuotaHourlyCapPoints, owner decision 2026-11-17 (WOOW prod: chart
# default 100000). The cap counts points and an Apporo point is worth more:
# 1 point = US$1 and the model ratios are WOOW's / 30, so 100000 would allow
# about US$93,750 of model usage an hour (WOOW: about US$3,125). Every proxy
# replica gets cap / replicas, so 2 replicas x the 10,000-point largest single
# conversion = 20000 still lets that conversion through on either replica.
HOURLY_CAP_POINTS = 20000
# woow_paas_platform converts 50-10,000 points at a time (copied from WOOW).
LARGEST_CONVERSION_POINTS = 10000
QUOTA_PER_POINT = 15625
# Never compared: each instance has its own (the chart binds the first two to
# the release namespace; the recipient is masked in the render).
PER_INSTANCE = ('encryptedBackup.capacityVerification', 'encryptedBackup.restoreVerification',
                'encryptedBackup.ageRecipient')
SECRET_KEYS = 'SESSION_SECRET|CRYPTO_SECRET|POSTGRES_PASSWORD|REDIS_PASSWORD|PROXY_VALKEY_PASSWORD'
LITELLM_SWAP = {'name: litellm-from-newapi-ingress': 'name: litellm-from-newapi-apporo-ingress',
                'kubernetes.io/metadata.name: new-api': f'kubernetes.io/metadata.name: {APPORO_NAMESPACE}'}
UNSET = '<unset>'
DIFF_LINES = 60


class RenderError(Exception):
    pass


def helm(*args):
    try:
        proc = subprocess.run(['helm', *args], text=True, capture_output=True)
    except FileNotFoundError:
        raise RenderError('helm is not installed') from None
    if proc.returncode:
        raise RenderError(proc.stderr.strip() or f'helm exit {proc.returncode}')
    return proc.stdout


def values(path):
    """The values a YAML file sets, parsed by helm itself (CI has no YAML library)."""
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp)
        (chart / 'templates').mkdir()
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: values-dump\nversion: 0.0.0\n')
        (chart / 'templates/values.json').write_text('{{ toJson .Values }}\n')
        out = helm('template', 'v', str(chart), '-f', str(path))
    return json.loads(out.split('# Source: values-dump/templates/values.json\n', 1)[1])


def render(path, namespace, extra=None, chart=CHART):
    """``helm template`` of ``path`` (plus ``extra`` values) in ``namespace``."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as tmp:
        json.dump(extra or {}, tmp)
        tmp.flush()
        out = helm('template', 'newapi', str(chart), '-n', namespace, '-f', str(path), '-f', tmp.name)
    # Offline Helm generates random Secret bytes (templates/secret.yaml): keep
    # the key names, drop the values.
    out = re.sub(rf'(?m)^(  (?:{SECRET_KEYS}):).*$', r'\1 <redacted>', out)
    # Per-instance output: the age recipient and the namespace in PromQL.
    out = re.sub(r'(?m)^(\s+- name: AGE_RECIPIENT\n\s+value: ).*$', r'\1<per-instance>', out)
    return out.replace(f'namespace="{namespace}"', 'namespace="<release namespace>"')


def documented(apporo_values, enabled):
    """The documented Apporo differences, as values layered on WOOW prod's."""
    proxy = apporo_values.get('proxy') or {}
    image = proxy.get('image') or {}
    return {
        'publicUrl': PUBLIC_URL,
        'newapi': {'env': {'DEFAULT_LANGUAGE': DEFAULT_LANGUAGE}},
        'proxy': {
            'enabled': enabled,
            'clients': APUN_CLIENTS,
            'addQuotaHourlyCapPoints': HOURLY_CAP_POINTS,
            # Apporo's own, checked in scripts/tests/test_apporo_values.py: the
            # AP/OP expiry ("" only while dormant) and a pin WOOW prod has or had.
            'apExpiresAt': proxy.get('apExpiresAt', ''),
            'opExpiresAt': proxy.get('opExpiresAt', ''),
            'image': {'tag': image.get('tag', ''), 'digest': image.get('digest', '')},
        },
    }


def merge(*layers):
    """Helm's merge of values layers: maps merge, anything else replaces, null deletes."""
    out = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if value is None:
                out.pop(key, None)
            elif isinstance(value, dict):
                out[key] = merge(out[key] if isinstance(out.get(key), dict) else {}, value)
            else:
                out[key] = value
    return out


def flat(tree, prefix=''):
    if isinstance(tree, dict) and tree:
        out = {}
        for key, value in tree.items():
            out.update(flat(value, f'{prefix}.{key}' if prefix else key))
        return out
    return {prefix: tree}


def value_differences(chart_values, woow_values, apporo_values):
    """(key, WOOW prod + documented, Apporo) for every effective value that differs."""
    expected = flat(merge(chart_values, woow_values, documented(apporo_values, (apporo_values.get('proxy') or {}).get('enabled'))))
    actual = flat(merge(chart_values, apporo_values))
    out = []
    for key in sorted(set(expected) | set(actual)):
        if any(key == p or key.startswith(p + '.') for p in PER_INSTANCE):
            continue
        if expected.get(key, UNSET) != actual.get(key, UNSET):
            out.append((key, expected.get(key, UNSET), actual.get(key, UNSET)))
    return out


def proxy_pin(tree):
    """(tag, digest) of proxy.image in effective values."""
    image = (tree.get('proxy') or {}).get('image') or {}
    return image.get('tag') or '', image.get('digest') or ''


def pin_text(pin):
    tag, digest = pin
    return f'{tag[:12] or "<no tag>"}@{digest.split(":")[-1][:12] or "<no digest>"}'


def manifest_lines(path):
    return [line.rstrip() for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def short(value, width=100):
    text = value if value == UNSET else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= width else text[:width - 3] + '...'


def one_line(text):
    """A workflow-command message: one line, % CR LF escaped."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def check(strict=False, woow=WOOW, apporo=APPORO, litellm_woow=LITELLM_WOOW, litellm_apporo=LITELLM_APPORO,
          chart=CHART):
    """Print the comparison and return the exit code."""
    print(f'Apporo follows WOOW prod: {rel(apporo)} vs {rel(woow)} + documented differences '
          f'({"strict" if strict else "warn-only"}; docs/APPORO-K3S.md 〈跟著 WOOW 改〉)')

    def apporo_error(what, err):
        message = f'{what}: {err}'.strip()
        print(f'ERROR   {message}')
        print(f'::error file={rel(apporo)},title=Apporo values do not render::'
              + one_line(message.splitlines()[0] + ' (Apporo\'s own validity: an error in every mode of this tool)'))
        return 2

    try:
        apporo_values = values(apporo)
    except (RenderError, OSError) as err:
        return apporo_error(f'{rel(apporo)} cannot be read', err)
    enabled = (apporo_values.get('proxy') or {}).get('enabled')
    if enabled is not None and not isinstance(enabled, bool):
        return apporo_error(f'{rel(apporo)} proxy.enabled', 'must be a literal true or false')
    label = {enabled: f'proxy as committed (enabled: {json.dumps(enabled)})'}
    label.setdefault(True, 'proxy switched on')

    # As committed: what helm would deploy. The shared chart must render it in
    # every mode, like every other committed values file.
    try:
        renders = {enabled: render(apporo, APPORO_NAMESPACE, {'proxy': {'enabled': enabled}}, chart)}
    except RenderError as err:
        return apporo_error(f'{rel(apporo)} does not render in {APPORO_NAMESPACE} with {label[enabled]}', err)

    differs, cannot, errors = [], [], []
    # While dormant, also with the proxy switched on (the chart checks the proxy
    # settings only when it renders the proxy). Not deployed yet, so a failure
    # here only warns: a chart change for WOOW, such as a new required proxy
    # value, must not block the WOOW PR. --strict, the check before an Apporo
    # install or upgrade, treats it as an error.
    switched_on = None
    states = [enabled]
    if enabled is not True:
        try:
            renders[True] = render(apporo, APPORO_NAMESPACE, {'proxy': {'enabled': True}}, chart)
            states.append(True)
        except RenderError as err:
            first = str(err).splitlines()[0] if str(err) else repr(err)
            switched_on = (f'{rel(apporo)} would not render in {APPORO_NAMESPACE} with the proxy switched on '
                           f'(dormant now; the activation PR needs it): {first}')
            if strict:
                print(f'ERROR   {switched_on}')
                errors.append(switched_on)
            else:
                print(f'WARN    {switched_on}')

    diffs = {}
    for state in states:
        try:
            expected = render(woow, WOOW_NAMESPACE, documented(apporo_values, state), chart)
        except RenderError as err:
            cannot.append(f'{rel(woow)} + documented differences does not render in {WOOW_NAMESPACE} '
                          f'({label[state]}): {str(err).splitlines()[0] if str(err) else err}')
            print(f'WARN    cannot compare ({label[state]}): {cannot[-1]}')
            continue
        if expected == renders[state]:
            print(f'ok      values, {label[state]}')
            continue
        diffs[state] = list(difflib.unified_diff(expected.splitlines(), renders[state].splitlines(),
                                                 'WOOW prod + documented differences', 'Apporo',
                                                 lineterm='', n=1))
        changed = sum(1 for line in diffs[state] if line[:1] in '+-' and line[:3] not in ('+++', '---'))
        print(f'DIFFERS values, {label[state]}: the renders differ in {changed} line(s)')
        differs.append(label[state])

    keys = []
    chart_values = woow_values = None
    try:
        chart_values, woow_values = values(Path(chart) / 'values.yaml'), values(woow)
    except (RenderError, OSError) as err:
        cannot.append(f'{rel(woow)} values cannot be read: {str(err).splitlines()[0] if str(err) else err}')
        print(f'WARN    cannot compare the proxy pin or the values: {cannot[-1]}')
    if (diffs or switched_on) and woow_values is not None:
        keys = value_differences(chart_values, woow_values, apporo_values)
        if keys:
            print('        values that differ (WOOW prod + documented differences | Apporo):')
            for key, want, got in keys:
                print(f'          {key}: {short(want)} | {short(got)}')
        elif diffs:
            print('        no value-level difference found; see the render diff')
    if diffs:
        state, diff = next(iter(diffs.items()))
        print(f'        render diff ({label[state]}), first {DIFF_LINES} lines:')
        for line in diff[:DIFF_LINES]:
            print(f'          {line}')

    # The render comparison above takes Apporo's own pin; compare it with WOOW
    # prod's current one here.
    if woow_values is not None:
        mine, theirs = proxy_pin(merge(chart_values, apporo_values)), proxy_pin(merge(chart_values, woow_values))
        if mine == theirs:
            print(f'ok      proxy pin = WOOW prod\'s ({pin_text(theirs)})')
        else:
            print(f'DIFFERS proxy pin: Apporo {pin_text(mine)} | WOOW prod {pin_text(theirs)}. Apporo is behind '
                  'WOOW prod, or WOOW prod rolled back and Apporo now leads: follow WOOW prod\'s pin in an Apporo '
                  'PR of its own (docs/APPORO-K3S.md 〈跟著 WOOW 改〉)')
            differs.append('proxy pin')

    try:
        expected_np = [line.replace(line.strip(), LITELLM_SWAP[line.strip()]) if line.strip() in LITELLM_SWAP
                       else line for line in manifest_lines(litellm_woow)]
    except OSError as err:
        expected_np = None
        cannot.append(f'{rel(litellm_woow)} cannot be read: {err}')
        print(f'WARN    cannot compare the LiteLLM ingress copy: {cannot[-1]}')
    try:
        actual_np = manifest_lines(litellm_apporo)
    except OSError as err:
        return apporo_error(f'{rel(litellm_apporo)} cannot be read', err)
    if expected_np is not None:
        if expected_np == actual_np:
            print(f'ok      {rel(litellm_apporo)} mirrors {rel(litellm_woow)}')
        else:
            print(f'DIFFERS {rel(litellm_apporo)} no longer mirrors {rel(litellm_woow)} (name and source namespace aside):')
            for line in list(difflib.unified_diff(expected_np, actual_np, 'expected', 'Apporo', lineterm='', n=1))[:DIFF_LINES]:
                print(f'          {line}')
            differs.append(rel(litellm_apporo))

    if not differs and not cannot and not switched_on:
        print('in sync')
        return 0
    names = [k for k, _, _ in keys] + (['proxy pin'] if 'proxy pin' in differs else [])
    what = ', '.join(names[:8]) + (f' (+{len(names) - 8} more)' if len(names) > 8 else '')
    summary = []
    if differs or names:
        summary.append(f'Apporo differs from WOOW prod + the documented differences ({what or "; ".join(differs)})')
    if switched_on:
        summary.append(switched_on)
    if cannot:
        summary.append('could not compare: ' + '; '.join(cannot))
    if strict:
        print('FAILED  ' + '. '.join(summary))
        return 2 if (errors or cannot) else 1
    note = ('. Warn-only (owner decision 2026-11-17: Apporo never blocks a WOOW PR). Nothing syncs these values '
            'automatically yet: bring deploy/apporo-k3s.yaml up to date in a PR of its own, and run '
            'scripts/apporo_checks.py --strict before any Apporo install or helm upgrade '
            '(docs/APPORO-K3S.md 〈跟著 WOOW 改〉)')
    print('WARNING (not blocking) ' + '. '.join(summary) + note)
    print(f'::warning file={rel(apporo)},title=Apporo does not follow WOOW prod::' + one_line('. '.join(summary) + note))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--strict', action='store_true',
                        help='exit 1 when Apporo is behind (scripts/apporo_checks.py runs it so)')
    parser.add_argument('--woow', type=Path, default=WOOW, help=argparse.SUPPRESS)
    parser.add_argument('--apporo', type=Path, default=APPORO, help=argparse.SUPPRESS)
    parser.add_argument('--litellm-woow', type=Path, default=LITELLM_WOOW, help=argparse.SUPPRESS)
    parser.add_argument('--litellm-apporo', type=Path, default=LITELLM_APPORO, help=argparse.SUPPRESS)
    parser.add_argument('--chart', type=Path, default=CHART, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return check(args.strict, args.woow, args.apporo, args.litellm_woow, args.litellm_apporo, args.chart)


if __name__ == '__main__':
    sys.exit(main())
