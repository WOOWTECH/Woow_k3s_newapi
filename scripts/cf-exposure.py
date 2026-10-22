#!/usr/bin/env python3
"""NA-06 stage-only plan/read preflight. No Cloudflare mutation implementation.

API JSON is parsed in memory, never printed. Origin response bodies are never
read. No env token/default account/zone, redirects, proxies or arbitrary API URL.
"""
import argparse
import fnmatch
import hashlib
import http.client
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

HOST = 'newapi-stg.woowtech.io'
ZONE = 'woowtech.io'
API = 'api.cloudflare.com'
ID = re.compile(r'[a-f0-9]{32}\Z')
UUID = re.compile(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\Z')
ANONYMOUS = [('GET', p) for p in (
    '/', '/console', '/api/status', '/api/pricing', '/api/ratio_config',
    '/api/perf-metrics/summary')] + [('POST', '/pg/chat/completions')]
BOUNDARIES = [('GET', p) for p in ('/v1', '/v1/', '/v1x', '/V1/models')]
DENIED = [('GET', p) for p in (
    '/v1/dashboard/billing/subscription', '/v1/tasks/x', '/v1/mj/submit/imagine',
    '/v1/video/generations/x', '/v1/../api/status', '/v1/%2e%2e/api/status', '//api/status')]


class Failure(Exception):
    """Fixed local failure code only; never incorporates provider or filesystem text."""


def require(condition, code='invalid_input'):
    if not condition:
        raise Failure(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def config_file(path):
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Failure('invalid_config') from None
    require(isinstance(cfg, dict) and set(cfg) == {
        'stage', 'account_id', 'zone_id', 'tunnel_id', 'owner_email', 'team_domain'})
    require(cfg['stage'] == 'stg')
    require(all(isinstance(cfg[k], str) and ID.fullmatch(cfg[k]) for k in ('account_id', 'zone_id')))
    require(isinstance(cfg['tunnel_id'], str) and UUID.fullmatch(cfg['tunnel_id']))
    require(isinstance(cfg['owner_email'], str) and re.fullmatch(
        r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', cfg['owner_email']))
    require(valid_team(cfg['team_domain']))
    return cfg


def valid_team(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com', value)


def desired(cfg):
    # Restrict methods as well as paths; raw/normalized disagreement and encoded
    # path characters must not cross from the public relay into origin routing.
    allowed = '(' + ' or '.join([
        ('(http.request.method eq "GET" and (http.request.uri.path eq "/v1/models" or '
         'starts_with(http.request.uri.path, "/v1/models/")))'),
        '(http.request.method eq "POST" and http.request.uri.path in {' + ' '.join(
            '"/v1/' + p + '"' for p in ('chat/completions', 'completions', 'embeddings', 'responses', 'messages')) + '})',
    ]) + ')'
    abnormal = '(raw.http.request.uri.path ne http.request.uri.path or ' \
        'raw.http.request.uri.path contains "%" or raw.http.request.uri.path contains ".." or ' \
        'raw.http.request.uri.path contains "//")'
    block = '(http.host eq "' + HOST + '" and (' + abnormal + ' or '
    block += '(starts_with(lower(http.request.uri.path), "/v1") and not ' + allowed + ') or '
    block += '(http.request.uri.path contains "/mj")))'
    return {
        'stage': 'stg', 'host': HOST, 'account_id': cfg['account_id'], 'zone_id': cfg['zone_id'],
        'applications': [
            {'name': 'NA06 stg management', 'type': 'self_hosted', 'domain': HOST + '/*',
             'session_duration': '1h', 'policies': [
                 {'name': 'Elmo only', 'decision': 'allow', 'precedence': 1,
                  'include': [{'email': {'email': cfg['owner_email']}}], 'exclude': [], 'require': []}]},
            {'name': 'NA06 stg relay', 'type': 'self_hosted', 'domain': HOST + '/v1/*',
             'session_duration': '1h', 'policies': [
                 {'name': 'Relay public', 'decision': 'bypass', 'precedence': 1,
                  'include': [{'everyone': {}}], 'exclude': [], 'require': []}]},
        ],
        'waf': {'ref': 'na06_stg_relay_guard', 'description': 'NA06 stg sold relay only',
                'action': 'block', 'enabled': True, 'expression': block},
        'dns': {'type': 'CNAME', 'name': HOST, 'content': cfg['tunnel_id'] + '.cfargotunnel.com',
                'proxied': True, 'ttl': 1},
    }


def token_file(path):
    require(hasattr(os, 'O_NOFOLLOW'), 'secure_open_unavailable')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077
                    and info.st_mode & stat.S_IRUSR and info.st_uid == os.geteuid(), 'unsafe_token_file')
            raw = os.read(fd, 514)
        finally:
            os.close(fd)
    except OSError:
        raise Failure('unsafe_token_file') from None
    if raw.endswith(b'\n'):
        raw = raw[:-1]
    require(re.fullmatch(rb'[A-Za-z0-9._~-]{20,512}', raw) is not None, 'invalid_token_file')
    return raw.decode('ascii')


class ReadAPI:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        require(path.startswith(('/accounts/', '/zones/', '/user/tokens/verify'))
                and not any(c in path for c in '\r\n'), 'invalid_api_path')
        connection = http.client.HTTPSConnection(API, timeout=15)
        try:
            connection.request('GET', '/client/v4' + path, headers={
                'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'})
            response = connection.getresponse()
            # Redirects, denied scope, missing objects and entitlement errors
            # fail before reading a body. No raw error response is retained.
            require(response.status == 200, 'api_scope_or_resource_unavailable')
            raw = response.read(2_000_001)
            require(len(raw) <= 2_000_000, 'api_response_too_large')
            data = json.loads(raw)
            require(isinstance(data, dict) and data.get('success') is True and 'result' in data,
                    'api_scope_or_resource_unavailable')
            return data
        except (OSError, ValueError, http.client.HTTPException):
            raise Failure('api_read_failed') from None
        finally:
            connection.close()

    def listing(self, path):
        result = []
        total_pages = total_count = None
        for page in range(1, 101):
            data = self.get(path + ('&' if '?' in path else '?') + urlencode({'page': page, 'per_page': 100}))
            rows, info = data['result'], data.get('result_info')
            require(isinstance(rows, list) and isinstance(info, dict), 'pagination_unproven')
            require(all(type(info.get(k)) is int for k in
                        ('page', 'per_page', 'count', 'total_count', 'total_pages')), 'pagination_unproven')
            require(info['page'] == page and info['per_page'] == 100
                    and info['count'] == len(rows) and info['total_count'] >= 0
                    and 0 <= info['total_pages'] <= 100, 'pagination_unproven')
            expected_pages = (info['total_count'] + 99) // 100
            require(info['total_pages'] in ((0, 1) if info['total_count'] == 0 else (expected_pages,)),
                    'pagination_unproven')
            require(len(rows) == min(100, max(0, info['total_count'] - (page - 1) * 100)),
                    'pagination_unproven')
            if total_pages is None:
                total_pages, total_count = info['total_pages'], info['total_count']
            require((info['total_pages'], info['total_count']) == (total_pages, total_count),
                    'pagination_unproven')
            require(all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in rows),
                    'pagination_unproven')
            result.extend(rows)
            require(len({row['id'] for row in result}) == len(result), 'pagination_unproven')
            if page >= total_pages:
                require(len(result) == total_count, 'pagination_unproven')
                return result
        raise Failure('pagination_unproven')


def overlaps(domain):
    # Conservative matching. Unknown/multi-destination apps are never ignored.
    hostname = domain.split('/', 1)[0].lower()
    return fnmatch.fnmatchcase(HOST, hostname)


def check_application(actual, target, policies):
    require(actual.get('type') == 'self_hosted' and actual.get('domain') == target['domain']
            and actual.get('session_duration') == target['session_duration'], 'access_drift')
    require(not actual.get('destinations')
            and actual.get('self_hosted_domains', []) in ([], [target['domain']]), 'access_extra_destinations')
    require(not actual.get('options_preflight_bypass') and not actual.get('service_auth_401_redirect'), 'access_bypass_drift')
    require(len(policies) == 1, 'access_policy_drift')
    policy, expected = policies[0], target['policies'][0]
    for key in ('decision', 'precedence', 'include'):
        require(policy.get(key) == expected[key], 'access_policy_drift')
    require(not policy.get('exclude') and not policy.get('require'), 'access_policy_drift')
    require(not policy.get('connection_rules') and not policy.get('approval_required'), 'access_policy_drift')
    require(policy.get('session_duration') in (None, target['session_duration']), 'access_session_drift')


def preflight(cfg, api):
    want = desired(cfg)
    require(api.get('/user/tokens/verify')['result'].get('status') == 'active', 'inactive_token')
    zone = api.get('/zones/' + cfg['zone_id'])['result']
    require(zone.get('id') == cfg['zone_id'] and zone.get('name') == ZONE
            and zone.get('account', {}).get('id') == cfg['account_id']
            and zone.get('status') == 'active', 'zone_account_mismatch')
    apps = []
    for scope, identity in (('accounts', cfg['account_id']), ('zones', cfg['zone_id'])):
        prefix = '/' + scope + '/' + identity + '/access/apps'
        for app in api.listing(prefix):
            require(isinstance(app, dict), 'invalid_api_schema')
            domains = [app.get('domain', '')] + (app.get('self_hosted_domains') or [])
            require(not app.get('destinations'), 'access_destinations_need_review')
            if any(overlaps(domain) for domain in domains):
                apps.append((prefix, app))
    require(len(apps) == 2, 'access_overlap_or_missing')
    require(sorted(app['domain'] for _, app in apps) == sorted(a['domain'] for a in want['applications']),
            'access_overlap_or_missing')
    for target in want['applications']:
        prefix, app = next((p, a) for p, a in apps if a['domain'] == target['domain'])
        require(isinstance(app.get('id'), str) and UUID.fullmatch(app['id']), 'invalid_api_schema')
        policies = api.listing(prefix + '/' + app['id'] + '/policies')
        check_application(app, target, policies)
    waf = api.get('/zones/' + cfg['zone_id'] + '/rulesets/phases/http_request_firewall_custom/entrypoint')['result']
    require(waf.get('kind') == 'zone' and waf.get('phase') == 'http_request_firewall_custom', 'waf_drift')
    rules = waf.get('rules', [])
    require(isinstance(rules, list) and rules, 'waf_missing')
    # Must be first: preceding skip/execute/terminating rules could bypass it.
    require(all(rules[0].get(k) == want['waf'][k] for k in ('ref', 'action', 'enabled', 'expression')), 'waf_drift')
    require(sum(r.get('ref') == want['waf']['ref'] for r in rules) == 1, 'waf_duplicate')
    dns = api.listing('/zones/' + cfg['zone_id'] + '/dns_records?' + urlencode({'name': HOST}))
    require(len(dns) == 1 and all(dns[0].get(k) == v for k, v in want['dns'].items()), 'dns_missing_or_drift')
    return {'status': 'configuration_verified', 'stage': 'stg', 'read_scopes_verified': True,
            'write_permissions_proven': False, 'access_apps': 2, 'waf_rule_first': True,
            'proxied_dns_verified': True, 'desired_sha256': digest(want),
            'runtime_exposure_verified': False, 'na06_passed': False}


def classify(status, location, ray, mitigated, team):
    require(valid_team(team), 'invalid_team_domain')
    try:
        target = urlsplit(location)
        login = (target.scheme == 'https' and target.netloc == team
                 and (target.path == '/cdn-cgi/access/login' or target.path.startswith('/cdn-cgi/access/login/'))
                 and not any(value in target.path for value in ('..', '%', '\\', '//'))
                 and not target.fragment)
    except ValueError:
        login = False
    # CF-Ray/Server alone identify an edge traversal, never which layer denied.
    edge = bool(re.fullmatch(r'[a-fA-F0-9]+-[A-Za-z0-9]+', ray or ''))
    if status == 302 and login and edge and not mitigated:
        return {'status': status, 'classification': 'access_login_redirect', 'access_redirect_observed': True, 'access_enforcement_verified': False}
    if mitigated == 'challenge' and edge:
        label = 'edge_challenge_not_access'
    elif status == 403:
        label = 'unattributed_403'
    elif status == 401:
        label = 'unattributed_401_not_access'
    elif status == 200:
        label = 'reachable_not_access'
    else:
        label = 'unproven_response'
    return {'status': status, 'classification': label, 'access_redirect_observed': False, 'access_enforcement_verified': False}


def probe(cfg):
    results = []
    for method, path in ANONYMOUS + BOUNDARIES:
        connection = None
        try:
            connection = http.client.HTTPSConnection(HOST, timeout=15)
            connection.request(method, path, body=b'' if method == 'POST' else None,
                               headers={'User-Agent': 'NA06-exposure-preflight/1', 'Accept': '*/*'})
            response = connection.getresponse()
            evidence = classify(response.status, response.getheader('Location', ''),
                                response.getheader('CF-Ray', ''), response.getheader('CF-Mitigated', ''),
                                cfg['team_domain'])
            results.append({'method': method, 'path': path, 'status': response.status, **evidence})
            # No response.read(), redirect following, cookie jar, or body log.
        except (OSError, http.client.HTTPException):
            results.append({'method': method, 'path': path, 'status': 0,
                            'classification': 'dns_or_transport_unresolved', 'access_redirect_observed': False, 'access_enforcement_verified': False})
        finally:
            if connection is not None:
                connection.close()
    passed = all(r['access_redirect_observed'] for r in results[:len(ANONYMOUS)])
    return {'status': 'headers_observed', 'stage': 'stg', 'requests': results,
            'anonymous_access_redirects_observed': passed,
            'access_enforcement_verified': False, 'na06_passed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='Default; no token read or network')
    modes.add_argument('--preflight', action='store_true', help='Explicit Cloudflare API GETs only')
    modes.add_argument('--probe', action='store_true', help='Explicit anonymous stage requests; headers only')
    modes.add_argument('--classify-headers', action='store_true', help='Offline curl metadata classifier')
    parser.add_argument('--config')
    parser.add_argument('--token-file')
    parser.add_argument('--team-domain')
    args = parser.parse_args()
    if args.classify_headers:
        require(args.team_domain and not args.token_file and not args.config)
        lines = sys.stdin.read(8192).splitlines()
        require(len(lines) == 4 and lines[0].isdigit(), 'invalid_header_metadata')
        result = classify(int(lines[0]), *lines[1:], args.team_domain)
        print(json.dumps(result))
        return 0 if result['access_redirect_observed'] else 1
    require(args.config is not None and not args.team_domain)
    cfg = config_file(args.config)
    if args.preflight:
        require(args.token_file is not None, 'token_file_required')
        result = preflight(cfg, ReadAPI(token_file(args.token_file)))
    elif args.probe:
        require(not args.token_file)
        result = probe(cfg)
    else:
        require(not args.token_file, 'dry_run_must_not_read_token')
        result = {'status': 'planned_only', 'stage': 'stg', 'network_calls': 0,
                  'mutations': 0, 'na06_passed': False, 'desired': desired(cfg)}
    print(json.dumps(result, sort_keys=True))
    return 1 if args.probe and not result['anonymous_access_redirects_observed'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Failure as exc:
        print(json.dumps({'status': 'failed_closed', 'code': str(exc), 'na06_passed': False}))
        sys.exit(1)
    except Exception:  # noqa: BLE001 - never expose API/token/response exception text
        print('{"status":"failed_closed","code":"invalid_response_or_local_error","na06_passed":false}')
        sys.exit(1)
