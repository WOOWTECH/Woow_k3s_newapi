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
ORIGIN = 'http://reviewed-service.new-api-stg.svc.cluster.local:3000'
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
        self.organization = {'auth_domain': CFG['team_domain']}
        self.tunnel = {'id': CFG['tunnel_id'], 'deleted_at': None,
                       'config_src': 'cloudflare', 'status': 'healthy'}
        self.clients = [{'id': '33333333-3333-3333-3333-333333333333', 'config_version': 1,
                         'conns': [{'id': '44444444-4444-4444-4444-444444444444',
                                    'client_id': '33333333-3333-3333-3333-333333333333'}]}]
        self.configuration = {'tunnel_id': CFG['tunnel_id'], 'source': 'cloudflare', 'version': 1,
                              'config': {'ingress': [
                                  {'hostname': cf.HOST, 'service': ORIGIN,
                                   'originRequest': {'connectTimeout': 10, 'keepAliveTimeout': 90}},
                                  {'service': 'http_status:404'}]}}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == '/user/tokens/verify':
            return {'result': {'status': 'active'}}
        if path == '/zones/' + CFG['zone_id']:
            return {'result': self.zone}
        if path.endswith('/access/organizations'):
            return {'result': self.organization}
        if path.endswith('/configurations'):
            return {'result': self.configuration}
        if path.endswith('/connections'):
            count = len(self.clients) if isinstance(self.clients, list) else 0
            return {'result': self.clients, 'result_info': {
                'page': 1, 'per_page': max(20, count), 'count': count, 'total_count': count}}
        if '/cfd_tunnel/' in path:
            return {'result': self.tunnel}
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
        result = cf.preflight(CFG, self.api, ORIGIN)
        self.assertEqual(result['status'], 'configuration_verified')
        self.assertFalse(result['write_permissions_proven'])
        self.assertFalse(result['runtime_exposure_verified'])
        self.assertFalse(result['na06_passed'])
        self.assertTrue(all('newapi.woowtech.io' not in path for path in self.api.calls))

    def test_account_zone_mismatch_fails_before_app_queries(self):
        self.api.zone['account']['id'] = 'c' * 32
        with self.assertRaisesRegex(cf.Failure, 'zone_account_mismatch'):
            cf.preflight(CFG, self.api, ORIGIN)
        self.assertEqual(len(self.api.calls), 2)

    def test_missing_and_duplicate_account_or_zone_apps_fail(self):
        self.api.zone_apps = [copy.deepcopy(self.api.apps[0])]
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api, ORIGIN)
        self.api.zone_apps = []
        self.api.apps.pop()
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api, ORIGIN)

    def test_wildcard_and_subpath_shadowing_is_rejected(self):
        for domain in ('*.woowtech.io/*', cf.HOST + '/api/*', cf.HOST + '/v1/models'):
            api = FixtureAPI()
            api.zone_apps = [{'domain': domain}]
            with self.subTest(domain=domain), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api, ORIGIN)

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
                cf.preflight(CFG, api, ORIGIN)

    def test_application_and_policy_session_override_must_match(self):
        self.api.apps[0]['session_duration'] = '24h'
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api, ORIGIN)
        self.api = FixtureAPI()
        self.api.policies[self.api.apps[0]['id']][0]['session_duration'] = '24h'
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api, ORIGIN)

    def test_unknown_app_destinations_require_manual_review(self):
        self.api.apps[0]['destinations'] = [{'type': 'public', 'uri': 'other.example.test'}]
        with self.assertRaises(cf.Failure):
            cf.preflight(CFG, self.api, ORIGIN)

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
                cf.preflight(CFG, api, ORIGIN)

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
                cf.preflight(CFG, api, ORIGIN)

    def test_tunnel_identity_health_and_schema_fail_closed(self):
        mutations = [
            ('id', '22222222-2222-2222-2222-222222222222'),
            ('deleted_at', '2026-01-01'), ('config_src', 'local'),
            ('status', 'down'), ('status', 'degraded'), ('status', None),
        ]
        for key, value in mutations:
            api = FixtureAPI()
            api.tunnel[key] = value
            with self.subTest(field=key, value=value), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api, ORIGIN)
        for key in ('id', 'deleted_at', 'config_src', 'status'):
            api = FixtureAPI()
            del api.tunnel[key]
            with self.subTest(missing=key), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api, ORIGIN)

    def test_clients_schema_connections_and_versions_fail_closed(self):
        for mutation in ('empty', 'object', 'null', 'row', 'id', 'duplicate', 'version_missing',
                         'version_old', 'version_new', 'version_bool', 'version_string',
                         'conns_missing', 'conns_empty', 'conns_object', 'conn_row',
                         'conn_id', 'client_id', 'duplicate_conn', 'mixed_versions'):
            api = FixtureAPI()
            client = api.clients[0]
            if mutation == 'empty': api.clients = []
            elif mutation == 'object': api.clients = {}
            elif mutation == 'null': api.clients = None
            elif mutation == 'row': api.clients = [None]
            elif mutation == 'id': client.pop('id')
            elif mutation == 'duplicate': api.clients.append(copy.deepcopy(client))
            elif mutation == 'version_missing': client.pop('config_version')
            elif mutation.startswith('version_'):
                client['config_version'] = {'version_old': 0, 'version_new': 2,
                                            'version_bool': True, 'version_string': '1'}[mutation]
            elif mutation == 'conns_missing': client.pop('conns')
            elif mutation == 'conns_empty': client['conns'] = []
            elif mutation == 'conns_object': client['conns'] = {}
            elif mutation == 'conn_row': client['conns'] = [None]
            elif mutation == 'conn_id': client['conns'][0].pop('id')
            elif mutation == 'client_id': client['conns'][0]['client_id'] = 'mismatch'
            elif mutation == 'duplicate_conn': client['conns'].append(copy.deepcopy(client['conns'][0]))
            else:
                other = copy.deepcopy(client)
                other['id'] = '55555555-5555-5555-5555-555555555555'
                other['config_version'] = 0
                api.clients.append(other)
            with self.subTest(mutation=mutation), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api, ORIGIN)

    def test_multiple_clients_must_all_match_configuration_version(self):
        other = copy.deepcopy(self.api.clients[0])
        other['id'] = '55555555-5555-5555-5555-555555555555'
        other['conns'][0]['client_id'] = other['id']
        self.api.clients.append(other)
        self.assertTrue(cf.preflight(CFG, self.api, ORIGIN)['connector_config_version_verified'])
        self.api.configuration['version'] = 2
        with self.assertRaisesRegex(cf.Failure, 'tunnel_connector_config_unproven'):
            cf.preflight(CFG, self.api, ORIGIN)
        for client in self.api.clients:
            client['config_version'] = 2
        self.assertTrue(cf.preflight(CFG, self.api, ORIGIN)['connector_config_version_verified'])

    def test_retired_fields_are_not_health_evidence_or_required(self):
        # Valid dedicated Client[] succeeds without either retired field.
        result = cf.preflight(CFG, self.api, ORIGIN)
        self.assertTrue(result['connector_config_version_verified'])
        self.api.tunnel['connections'] = [{'is_pending_reconnect': False}]
        self.api.clients = []
        with self.assertRaisesRegex(cf.Failure, 'tunnel_connector_health_unknown'):
            cf.preflight(CFG, self.api, ORIGIN)
        self.api.clients = FixtureAPI().clients
        for value in (False, True, None):
            self.api.clients[0]['conns'][0]['is_pending_reconnect'] = value
            self.assertTrue(cf.preflight(CFG, self.api, ORIGIN)['connector_config_version_verified'])

    def test_connections_metadata_must_not_hide_more_clients(self):
        original = self.api.get
        valid = {'page': 1, 'per_page': 20, 'count': 1, 'total_count': 1}
        missing = object()
        cases = [missing, None, [], {},
                 dict(valid, total_count=2), dict(valid, page=2),
                 dict(valid, per_page=0), dict(valid, count=0),
                 dict(valid, total_count=-1), dict(valid, page=True),
                 dict(valid, count='1')]
        cases.extend({k: v for k, v in valid.items() if k != field} for field in valid)
        for info in cases:
            def get(path):
                result = original(path)
                if path.endswith('/connections'):
                    if info is missing:
                        result.pop('result_info')
                    else:
                        result['result_info'] = info
                return result
            self.api.get = get
            with self.subTest(info='missing' if info is missing else info), self.assertRaisesRegex(
                    cf.Failure, 'tunnel_clients_completeness_unknown'):
                cf.preflight(CFG, self.api, ORIGIN)
        info = valid
        self.assertTrue(cf.preflight(CFG, self.api, ORIGIN)['connector_config_version_verified'])

    def test_team_domain_must_be_read_not_inferred_from_input(self):
        for organization in ({}, None, {'auth_domain': 'other.cloudflareaccess.com'}):
            api = FixtureAPI()
            api.organization = organization
            with self.subTest(organization=organization), self.assertRaisesRegex(
                    cf.Failure, 'access_team_unknown_or_mismatch'):
                cf.preflight(CFG, api, ORIGIN)

    def test_ingress_must_be_exact_and_remote_configuration_proven(self):
        for mutation in ('identity', 'source', 'version', 'missing', 'extra', 'order',
                         'hostname', 'origin', 'path', 'fallback', 'origin_options', 'global_options'):
            api = FixtureAPI()
            config = api.configuration['config']
            route = config['ingress'][0]
            if mutation == 'identity':
                api.configuration['tunnel_id'] = 'wrong'
            elif mutation == 'source':
                api.configuration.pop('source')
            elif mutation == 'version':
                api.configuration['version'] = True
            elif mutation == 'missing':
                config.pop('ingress')
            elif mutation == 'extra':
                config['ingress'].insert(0, {'hostname': '*', 'service': ORIGIN})
            elif mutation == 'order':
                config['ingress'].reverse()
            elif mutation == 'hostname':
                route['hostname'] = '*.woowtech.io'
            elif mutation == 'origin':
                route['service'] = 'http://other.new-api-stg.svc.cluster.local:3000'
            elif mutation == 'path':
                route['path'] = '/api/.*'
            elif mutation == 'fallback':
                config['ingress'][1]['service'] = ORIGIN
            elif mutation == 'origin_options':
                route['originRequest']['noTLSVerify'] = True
            else:
                config['originRequest'] = {'httpHostHeader': 'unexpected'}
            with self.subTest(mutation=mutation), self.assertRaises(cf.Failure):
                cf.preflight(CFG, api, ORIGIN)

    def test_origin_requires_explicit_reviewed_staging_url(self):
        for origin in (None, '', 'http://newapi.new-api.svc.cluster.local:3000',
                       ORIGIN + '/path', ORIGIN + '?query', ORIGIN.replace(':3000', ':0'),
                       ORIGIN.replace(':3000', ':65536')):
            with self.subTest(origin=origin), self.assertRaises(cf.Failure):
                cf.preflight(CFG, FixtureAPI(), origin)

    def test_missing_dns_still_fails_after_tunnel_team_verified(self):
        self.api.dns = []
        with self.assertRaisesRegex(cf.Failure, 'dns_missing_or_drift'):
            cf.preflight(CFG, self.api, ORIGIN)
        self.assertTrue(any(path.endswith('/configurations') for path in self.api.calls))
        self.assertTrue(any(path.endswith('/access/organizations') for path in self.api.calls))

    def test_api_evidence_does_not_claim_runtime_readiness_or_leak_ids(self):
        result = cf.preflight(CFG, self.api, ORIGIN)
        for key in ('tunnel_configuration_verified', 'connector_health_observed', 'access_team_verified'):
            self.assertTrue(result[key])
        for key in ('public_dns_resolution_verified', 'bootstrap_verified', 'runtime_exposure_verified', 'na06_passed'):
            self.assertFalse(result[key])
        for value in (CFG['account_id'], CFG['zone_id'], CFG['tunnel_id'], CFG['team_domain'], ORIGIN):
            self.assertNotIn(value, json.dumps(result))

    def test_new_endpoints_use_get_only_transport_and_denials_fail_closed(self):
        api = cf.ReadAPI(TOKEN)
        fixture = FixtureAPI()
        api.listing = fixture.listing
        def response(path):
            return {'success': True, **fixture.get(path.removeprefix('/client/v4'))}
        connection = Mock()
        current = []
        connection.request.side_effect = lambda method, path, **kwargs: current.append((method, path))
        connection.getresponse.side_effect = lambda: Mock(
            status=200, read=lambda limit: json.dumps(response(current[-1][1])).encode())
        with patch.object(cf.http.client, 'HTTPSConnection', return_value=connection):
            cf.preflight(CFG, api, ORIGIN)
        self.assertTrue(all(method == 'GET' for method, _ in current))
        self.assertTrue(any(path.endswith('/configurations') for _, path in current))
        self.assertTrue(any(path.endswith('/connections') for _, path in current))
        for code in (403, 404):
            for endpoint in ('/access/organizations', '/cfd_tunnel/' + CFG['tunnel_id'],
                             '/cfd_tunnel/' + CFG['tunnel_id'] + '/configurations',
                             '/cfd_tunnel/' + CFG['tunnel_id'] + '/connections'):
                denied = Mock(status=code)
                connection.getresponse.side_effect = None
                connection.getresponse.return_value = denied
                with patch.object(cf.http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(cf.Failure):
                    api.get('/accounts/' + CFG['account_id'] + endpoint)
                denied.read.assert_not_called()

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

    def test_cli_missing_origin_stops_before_token_or_network(self):
        self.path.write_text(json.dumps(CFG))
        with patch.object(cf.sys, 'argv', [
                'cf-exposure.py', '--config', str(self.path), '--preflight', '--token-file', '/not-read']), \
                patch.object(cf, 'token_file', side_effect=AssertionError('no token read')), \
                patch.object(cf.http.client, 'HTTPSConnection', side_effect=AssertionError('offline')), \
                self.assertRaisesRegex(cf.Failure, 'tunnel_origin_unknown'):
            cf.main()

    def test_cli_origin_argument_is_rejected_outside_preflight(self):
        with patch.object(cf.sys, 'argv', [
                'cf-exposure.py', '--dry-run', '--expected-origin', ORIGIN]), \
                self.assertRaisesRegex(cf.Failure, 'unexpected_origin_argument'):
            cf.main()

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
        for key in ('account_id', 'zone_id', 'tunnel_id'):
            self.assertNotIn(CFG[key], output.getvalue())


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
