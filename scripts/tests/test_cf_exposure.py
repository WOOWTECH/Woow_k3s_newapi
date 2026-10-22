"""Offline NA06 configuration, permission, token, pagination and header evidence tests."""
import copy
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'cf-exposure.py'
SPEC = importlib.util.spec_from_file_location('cf_exposure', SCRIPT)
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)
TOKEN = 'unit-test-token-never-print-123456789'
CFG = {'stage': 'stg', 'account_id': 'a' * 32, 'zone_id': 'b' * 32,
       'tunnel_id': '11111111-1111-1111-1111-111111111111',
       'owner_email': 'elmo@example.test', 'team_domain': 'unit-team.cloudflareaccess.com'}


class FixtureAPI:
    def __init__(self):
        want = cf.desired(CFG)
        self.apps = []
        self.policies = {}
        for n, app in enumerate(want['applications']):
            app = copy.deepcopy(app)
            app['id'] = f'00000000-0000-0000-0000-{n:012d}'
            self.policies[app['id']] = app.pop('policies')
            self.apps.append(app)
        self.zone_apps = []
        self.zone = {'id': CFG['zone_id'], 'name': cf.ZONE, 'status': 'active',
                     'account': {'id': CFG['account_id']}}
        self.waf = {'kind': 'zone', 'phase': 'http_request_firewall_custom', 'rules': [want['waf']]}
        self.dns = [want['dns']]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == '/user/tokens/verify':
            return {'result': {'status': 'active'}}
        if path == '/zones/' + CFG['zone_id']:
            return {'result': self.zone}
        if '/rulesets/' in path:
            return {'result': self.waf}
        raise AssertionError('unexpected API read')

    def listing(self, path):
        self.calls.append(path)
        if path.endswith('/policies'):
            return self.policies[path.split('/')[-2]]
        if '/dns_records?' in path:
            return self.dns
        if path.startswith('/zones/'):
            return self.zone_apps
        return self.apps


class TestPlanAndPreflight(unittest.TestCase):
    def setUp(self):
        self.api = FixtureAPI()

    def test_exact_access_waf_dns_configuration_only_is_verified(self):
        result = cf.preflight(CFG, self.api)
        self.assertEqual(result['status'], 'configuration_verified')
        self.assertFalse(result['write_permissions_proven'])
        self.assertFalse(result['runtime_exposure_verified'])
        self.assertFalse(result['na06_passed'])
        self.assertTrue(all('newapi.woowtech.io' not in path for path in self.api.calls))

    def test_account_zone_mismatch_fails_before_app_queries(self):
        self.api.zone['account']['id'] = 'c' * 32
        with self.assertRaisesRegex(cf.Failure, 'zone_account_mismatch'):
            cf.preflight(CFG, self.api)
        self.assertEqual(len(self.api.calls), 2)

    def test_missing_and_duplicate_account_or_zone_apps_fail(self):
        self.api.zone_apps = [copy.deepcopy(self.api.apps[0])]
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api)
        self.api.zone_apps = []
        self.api.apps.pop()
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api)

    def test_wildcard_and_subpath_shadowing_is_rejected(self):
        for domain in ('*.woowtech.io/*', cf.HOST + '/api/*', cf.HOST + '/v1/models'):
            api = FixtureAPI()
            api.zone_apps = [{'domain': domain}]
            with self.subTest(domain=domain), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api)

    def test_everyone_allow_and_extra_policy_fail_closed(self):
        for mutation in ('everyone', 'extra', 'require', 'exclude'):
            api = FixtureAPI()
            policy = api.policies[api.apps[0]['id']][0]
            if mutation == 'everyone':
                policy['include'] = [{'everyone': {}}]
            elif mutation == 'extra':
                api.policies[api.apps[0]['id']].append(copy.deepcopy(policy))
            else:
                policy[mutation] = [{'everyone': {}}]
            with self.subTest(mutation=mutation), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api)

    def test_application_and_policy_session_override_must_match(self):
        self.api.apps[0]['session_duration'] = '24h'
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api)
        self.api = FixtureAPI()
        self.api.policies[self.api.apps[0]['id']][0]['session_duration'] = '24h'
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api)

    def test_unknown_app_destinations_require_manual_review(self):
        self.api.apps[0]['destinations'] = [{'type': 'public', 'uri': 'other.example.test'}]
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api)

    def test_waf_must_be_exact_enabled_block_and_first(self):
        for mutation in ('disabled', 'expression', 'skip_first', 'duplicate'):
            api = FixtureAPI()
            if mutation == 'disabled':
                api.waf['rules'][0]['enabled'] = False
            elif mutation == 'expression':
                api.waf['rules'][0]['expression'] = 'true'
            elif mutation == 'skip_first':
                api.waf['rules'].insert(0, {'action': 'skip'})
            else:
                api.waf['rules'].append(copy.deepcopy(api.waf['rules'][0]))
            with self.subTest(mutation=mutation), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api)

    def test_dns_unresolved_unproxied_wrong_tunnel_or_duplicate_never_pass(self):
        for mutation in ('missing', 'unproxied', 'wrong', 'duplicate'):
            api = FixtureAPI()
            if mutation == 'missing':
                api.dns = []
            elif mutation == 'unproxied':
                api.dns[0]['proxied'] = False
            elif mutation == 'wrong':
                api.dns[0]['content'] = 'unexpected.cfargotunnel.com'
            else:
                api.dns.append(copy.deepcopy(api.dns[0]))
            with self.subTest(mutation=mutation), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api)

    def test_plan_stays_host_scoped_and_handles_raw_paths_without_regex_entitlement(self):
        rule = cf.desired(CFG)['waf']['expression']
        self.assertIn('http.host eq "' + cf.HOST + '"', rule)
        self.assertNotIn('newapi.woowtech.io', rule)
        self.assertIn('raw.http.request.uri.path', rule)
        self.assertIn('http.request.method eq "POST"', rule)
        self.assertNotIn(' matches ', rule)


class TestTokenAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'token'
        self.path.write_text(TOKEN + '\n')
        self.path.chmod(0o600)

    def test_owner_only_regular_token_file(self):
        self.assertEqual(cf.token_file(self.path), TOKEN)
        self.path.chmod(0o400)
        self.assertEqual(cf.token_file(self.path), TOKEN)

    def test_symlink_fifo_missing_and_group_permissions_fail(self):
        link = self.path.parent / 'link'
        link.symlink_to(self.path)
        fifo = self.path.parent / 'fifo'
        os.mkfifo(fifo, 0o600)
        for candidate in (link, fifo, self.path.parent / 'missing'):
            with self.subTest(kind=candidate.name), self.assertRaises(cf.Failure):
                cf.token_file(candidate)
        self.path.chmod(0o640)
        with self.assertRaises(cf.Failure):
            cf.token_file(self.path)

    def test_token_newline_injection_overflow_and_blank_rejected(self):
        for value in ('', TOKEN + '\nother', 'x' * 513, TOKEN + '\r\n', TOKEN + '"'):
            self.path.write_text(value)
            with self.subTest(length=len(value)), self.assertRaises(cf.Failure):
                cf.token_file(self.path)

    def test_prod_missing_ids_and_arbitrary_team_rejected(self):
        for key, value in (('stage', 'prod'), ('account_id', ''), ('zone_id', 'guess'),
                           ('team_domain', 'attacker.example.test'), ('tunnel_id', 'bad')):
            cfg = dict(CFG, **{key: value})
            self.path.write_text(json.dumps(cfg))
            with self.subTest(key=key), self.assertRaises(cf.Failure):
                cf.config_file(self.path)

    def test_default_dry_run_no_token_read_or_network(self):
        self.path.write_text(json.dumps(CFG))
        with patch.object(cf.sys, 'argv', ['cf-exposure.py', '--config', str(self.path)]), \
                patch.object(cf, 'token_file', side_effect=AssertionError('no token read')), \
                patch.object(cf.http.client, 'HTTPSConnection', side_effect=AssertionError('offline')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cf.main(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data['network_calls'], 0)
        self.assertEqual(data['mutations'], 0)
        self.assertNotIn(TOKEN, output.getvalue())


class TestAPITransport(unittest.TestCase):
    def response(self, status=200, data=None):
        response = Mock(status=status)
        response.read.return_value = json.dumps(data or {'success': True, 'result': {}}).encode()
        return response

    def test_get_only_fixed_api_and_token_not_in_url(self):
        response = self.response()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(cf.http.client, 'HTTPSConnection', return_value=connection) as factory:
            cf.ReadAPI(TOKEN).get('/zones/' + CFG['zone_id'])
        factory.assert_called_once_with('api.cloudflare.com', timeout=15)
        args = connection.request.call_args
        self.assertEqual(args.args[:2], ('GET', '/client/v4/zones/' + CFG['zone_id']))
        self.assertNotIn(TOKEN, args.args[1])
        connection.close.assert_called_once()

    def test_forbidden_missing_and_redirect_never_read_body(self):
        for code in (301, 302, 401, 403, 404, 429, 500):
            response = self.response(code)
            connection = Mock()
            connection.getresponse.return_value = response
            with self.subTest(code=code), \
                    patch.object(cf.http.client, 'HTTPSConnection', return_value=connection), \
                    self.assertRaises(cf.Failure):
                cf.ReadAPI(TOKEN).get('/user/tokens/verify')
            response.read.assert_not_called()

    def test_pagination_reads_all_pages_or_refuses_unproven_listing(self):
        api = cf.ReadAPI(TOKEN)
        first = [{'id': str(i)} for i in range(100)]
        last = [{'id': '100'}]
        api.get = Mock(side_effect=[
            {'result': first, 'result_info': {'page': 1, 'per_page': 100, 'count': 100, 'total_count': 101, 'total_pages': 2}},
            {'result': last, 'result_info': {'page': 2, 'per_page': 100, 'count': 1, 'total_count': 101, 'total_pages': 2}},
        ])
        self.assertEqual(api.listing('/accounts/' + CFG['account_id'] + '/access/apps'), first + last)
        api.get = Mock(return_value={'result': []})
        with self.assertRaises(cf.Failure):
            api.listing('/accounts/' + CFG['account_id'] + '/access/apps')

    def test_empty_listing_requires_consistent_zero_total(self):
        for pages in (0, 1):
            api = cf.ReadAPI(TOKEN)
            api.get = Mock(return_value={'result': [], 'result_info': {
                'page': 1, 'per_page': 100, 'count': 0, 'total_count': 0, 'total_pages': pages}})
            self.assertEqual(api.listing('/accounts/' + CFG['account_id'] + '/access/apps'), [])
        api.get = Mock(return_value={'result': [], 'result_info': {
            'page': 1, 'per_page': 100, 'count': 0, 'total_count': 1, 'total_pages': 1}})
        with self.assertRaises(cf.Failure):
            api.listing('/accounts/' + CFG['account_id'] + '/access/apps')

    def test_truncated_page_inconsistent_totals_and_duplicates_fail(self):
        first = {'result': [{'id': str(i)} for i in range(100)], 'result_info': {
            'page': 1, 'per_page': 100, 'count': 100, 'total_count': 101, 'total_pages': 2}}
        for second in (
                {'result': [], 'result_info': {'page': 2, 'per_page': 100, 'count': 0, 'total_count': 101, 'total_pages': 2}},
                {'result': [{'id': '0'}], 'result_info': {'page': 2, 'per_page': 100, 'count': 1, 'total_count': 101, 'total_pages': 2}},
                {'result': [], 'result_info': {'page': 2, 'per_page': 100, 'count': 0, 'total_count': 100, 'total_pages': 1}},
                {'result': [], 'result_info': {'page': 2, 'per_page': 100, 'count': 0, 'total_count': 0, 'total_pages': 0}}):
            api = cf.ReadAPI(TOKEN)
            api.get = Mock(side_effect=[first, second])
            with self.subTest(metadata=second['result_info']), self.assertRaises(cf.Failure):
                api.listing('/accounts/' + CFG['account_id'] + '/access/apps')


class TestHeaderEvidence(unittest.TestCase):
    def classify(self, status, location='', ray='abc123-TEST', mitigated=''):
        return cf.classify(status, location, ray, mitigated, CFG['team_domain'])

    def test_access_needs_exact_https_team_login_path_and_edge_header(self):
        login = 'https://' + CFG['team_domain'] + '/cdn-cgi/access/login/app?private=never-print'
        self.assertTrue(self.classify(302, login)['access_redirect_observed'])
        self.assertFalse(self.classify(302, login)['access_enforcement_verified'])
        for location in ('https://other.cloudflareaccess.com/cdn-cgi/access/login/app',
                         login.replace('https:', 'http:'), login.replace('/cdn-cgi/access/login', '/console'),
                         login.replace('/cdn-cgi/access/login/', '/cdn-cgi/access/login-evil/'),
                         login.replace('/app?', '/../../../not-login?'),
                         login.replace('/app?', '/%2e%2e/not-login?'),
                         'https://unit-team.cloudflareaccess.com.attacker.test/cdn-cgi/access/login'):
            self.assertFalse(self.classify(302, location)['access_redirect_observed'])
        self.assertFalse(self.classify(302, login, '')['access_redirect_observed'])
        self.assertFalse(self.classify(302, login, mitigated='challenge')['access_redirect_observed'])

    def test_cloudflare_ray_and_403_never_imply_access_or_waf(self):
        self.assertEqual(self.classify(403)['classification'], 'unattributed_403')
        self.assertFalse(self.classify(403)['access_redirect_observed'])
        self.assertEqual(self.classify(403, mitigated='challenge')['classification'], 'edge_challenge_not_access')
        self.assertFalse(self.classify(401)['access_redirect_observed'])
        self.assertFalse(self.classify(200)['access_redirect_observed'])

    def test_probe_records_post_method_and_never_reads_body_or_follows_redirect(self):
        response = Mock(status=302)
        headers = {'Location': 'https://' + CFG['team_domain'] + '/cdn-cgi/access/login/app', 'CF-Ray': 'abc-TEST'}
        response.getheader.side_effect = lambda key, default='': headers.get(key, default)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(cf.http.client, 'HTTPSConnection', return_value=connection):
            result = cf.probe(CFG)
        response.read.assert_not_called()
        self.assertEqual(connection.request.call_count, len(cf.ANONYMOUS) + len(cf.BOUNDARIES))
        self.assertIn(('POST', '/pg/chat/completions'), [(r['method'], r['path']) for r in result['requests']])
        self.assertFalse(result['na06_passed'])
        self.assertNotIn('Location', json.dumps(result))

    def test_unresolved_dns_fails_all_checks_not_success(self):
        with patch.object(cf.http.client, 'HTTPSConnection', side_effect=OSError):
            result = cf.probe(CFG)
        self.assertFalse(result['anonymous_access_redirects_observed'])
        self.assertTrue(all(r['classification'] == 'dns_or_transport_unresolved'
                            for r in result['requests']))

    def test_cli_never_prints_token_on_config_or_permission_error(self):
        result = subprocess.run(['python3', str(SCRIPT), '--preflight', '--config', '/nonexistent',
                                 '--token-file', '/not-read'], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn('/nonexistent', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
