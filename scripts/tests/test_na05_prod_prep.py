"""NA-05 offline guards: no credentials, cluster, settings calls or Odoo tests."""
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tests.contract import gate_check, report

ROOT=Path(__file__).resolve().parents[2]
BASE='d487835aeb8c20ed8143e3d0de7ca94b7008e749'


def proxy_values(enabled, pin=None):
    lines=['proxy:', '  enabled: '+str(enabled).lower()]
    if pin:
        lines+=['  image:', '    tag: '+str(pin.get('tag') or ''),
                '    digest: '+str(pin.get('digest') or '')]
    return '\n'.join(lines)+'\n'


class ProdContractGateTests(unittest.TestCase):
    def setUp(self):
        self.base={key:(ROOT/path).read_text() for key,path in gate_check.FILES.items()}
        self.pin=report.pins()['proxy']

    def required(self,old_prod,new_prod):
        old=dict(self.base,prod=old_prod);new=dict(self.base,prod=new_prod)
        by_path={path:key for key,path in gate_check.FILES.items()}
        with patch.object(gate_check,'_at',side_effect=lambda ref,p:old[by_path[p]]), \
             patch.object(gate_check,'_now',side_effect=lambda p:new[by_path[p]]):
            return gate_check.required(BASE)

    def evaluate(self,old_prod,new_prod,evidence=None):
        old=dict(self.base,prod=old_prod);new=dict(self.base,prod=new_prod)
        by_path={path:key for key,path in gate_check.FILES.items()}
        with tempfile.TemporaryDirectory() as tmp:
            if evidence is not None:
                name=report.report_name({'newapi':report.pins()['newapi'],
                                         'proxy':report.image_pin(new_prod,'proxy')})
                (Path(tmp)/name).write_text(json.dumps(evidence))
            output=io.StringIO()
            with patch.object(gate_check,'_at',side_effect=lambda ref,p:old[by_path[p]]), \
                 patch.object(gate_check,'_now',side_effect=lambda p:new[by_path[p]]), \
                 patch.object(report,'REPORTS_DIR',tmp),contextlib.redirect_stdout(output):
                code=gate_check.main(['--base-ref',BASE])
            return code,output.getvalue()

    def evidence(self):
        return json.loads((ROOT/'tests/contract/reports'/report.report_name(report.pins())).read_text())

    def test_dormant_unpinned_values_need_no_report(self):
        self.assertEqual(self.required('',proxy_values(False)),[])
        self.assertEqual(self.evaluate('',proxy_values(False))[0],0)

    def test_first_activation_without_pin_change_requires_report(self):
        old=proxy_values(False,self.pin);new=proxy_values(True,self.pin)
        self.assertEqual(len(self.required(old,new)),1)
        code,text=self.evaluate(old,new)
        self.assertEqual(code,1);self.assertIn('commit tests/contract/reports/',text)

    def test_active_unchanged_pin_still_requires_report(self):
        active=proxy_values(True,self.pin)
        self.assertEqual(len(self.required(active,active)),1)
        self.assertEqual(self.evaluate(active,active)[0],1)

    def test_valid_alternate_indentation_cannot_bypass_unchanged_pin_gate(self):
        active=proxy_values(True,self.pin).replace('\n  ', '\n    ')
        self.assertEqual(len(self.required(active,active)),1)
        code,text=self.evaluate(active,active)
        self.assertEqual(code,1)
        self.assertIn('FAIL prod proxy',text)

    def test_escaped_yaml_keys_cannot_override_proven_dormancy(self):
        for active in (
            proxy_values(False,self.pin) + '  "\\u0065nabled": true\n',
            proxy_values(False,self.pin) + '"\\u0070roxy":\n  enabled: true\n',
        ):
            with self.subTest(active=active):
                self.assertEqual(len(self.required(active,active)),1)
                self.assertEqual(self.evaluate(active,active)[0],1)

    def test_matching_existing_stg_report_opens_only_contract_gate(self):
        self.assertEqual(self.evaluate(proxy_values(False,self.pin),proxy_values(True,self.pin),self.evidence())[0],0)

    def test_changed_or_non_green_evidence_rejected(self):
        for mutate in ['digest','partial','cleanup']:
            e=self.evidence()
            if mutate=='digest':e['env']['declared']['proxy']['digest']='sha256:'+'c'*64
            elif mutate=='partial':e['summary']['partial']=1
            else:e['cleanup']['left']=['synthetic-test-user']
            with self.subTest(mutate=mutate):
                self.assertEqual(self.evaluate(proxy_values(False),proxy_values(True,self.pin),e)[0],1)

    def test_digest_only_and_placeholder_prod_pins_fail_closed(self):
        for tag,digest in [(None,self.pin['digest']),('latest',self.pin['digest']),
                           (self.pin['tag'],'sha256:'+'0'*64)]:
            new=proxy_values(True,{'tag':tag,'digest':digest})
            self.assertEqual(len(self.required('',new)),1)
            code,text=self.evaluate('',new)
            self.assertEqual(code,1);self.assertIn('full commit-SHA tag',text)
        # Pinning a digest while disabled is not a route around contract checks.
        self.assertEqual(len(self.required('',proxy_values(False,{'digest':self.pin['digest']}))),1)

    def test_only_literal_false_skips_activation_validation(self):
        for text in ['proxy:\n  enabled: "false"\n','proxy:\n  enabled: !!bool "true"\n',
                     'proxy: {enabled: true}\n',
                     'proxy:\n  enabled: false\n  enabled: true\n',
                     'proxy:\n  enabled: false\nproxy:\n  enabled: true\n',
                     'proxy:\n  <<: *defaults\n',
                     '"proxy":\n  "enabled": true\n']:
            self.assertTrue(gate_check.proxy_enabled(text))
        self.assertFalse(gate_check.proxy_enabled('proxy:\n  enabled: false # dormant\n'))
        self.assertFalse(gate_check.proxy_enabled('proxy:\n    enabled: false\n'))


class ProdRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.base=Path(cls.tmp.name)
        archive=subprocess.run(['git','archive',BASE,'chart'],cwd=ROOT,capture_output=True,check=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            for member in tar.getmembers():
                if member.isfile():
                    if Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                        raise AssertionError('unsafe archive member')
                    target=cls.base/member.name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def render(self,chart,site=None,extra=None):
        args=['helm','template','newapi',str(chart),'-n','new-api' if site!='stg' else 'new-api-stg']
        if site:args+=['-f',str(ROOT/'deploy'/('woow-k3s-stg.yaml' if site=='stg' else 'woow-k3s.yaml'))]
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as tmp:
            if extra:
                json.dump(extra,tmp);tmp.flush();args+=['-f',tmp.name]
            return subprocess.run(args,cwd=ROOT,text=True,capture_output=True)

    def normalized(self,text):
        # Offline Helm generates random test Secret bytes; compare their key
        # shape without printing values. No live lookup/credential is used.
        text=re.sub(r'(?m)^(  (?:SESSION_SECRET|CRYPTO_SECRET|POSTGRES_PASSWORD|REDIS_PASSWORD|PROXY_VALKEY_PASSWORD):).*$',r'\1 <redacted>',text)
        return sorted('\n'.join(l.rstrip() for l in d.splitlines() if l.strip() and not l.lstrip().startswith('#')) for d in text.split('---') if 'kind:' in d)

    def test_default_and_core_site_render_unchanged_from_pr18(self):
        # PR18 protects the core/proxy resources. Retention was dormant in
        # that baseline; exercise its active policy and script separately in
        # tests/maintenance instead of comparing it to the historical script.
        dormant_retention={'retention': {'enabled': False}}
        for site in [None,'prod','stg']:
            before=self.render(self.base/'chart',site,dormant_retention)
            after=self.render(ROOT/'chart',site,dormant_retention)
            self.assertEqual(before.returncode,0,before.stderr);self.assertEqual(after.returncode,0,after.stderr)
            self.assertNotIn('  name: newapi-retention\n',after.stdout)
            with self.subTest(site=site):self.assertEqual(self.normalized(before.stdout),self.normalized(after.stdout))
            if site is None:
                self.assertNotIn('  name: newapi-proxy\n',after.stdout)
                self.assertNotIn('PROXY_VALKEY_PASSWORD:',after.stdout)
                self.assertNotIn('app.kubernetes.io/component: proxy',after.stdout)

    def test_prod_proxy_activation_runtime_contract(self):
        rendered=self.render(ROOT/'chart','prod')
        self.assertEqual(rendered.returncode,0,rendered.stderr)
        docs=[d for d in rendered.stdout.split('---') if re.search(r'(?m)^kind: ',d)]

        def document(kind,name):
            matches=[d for d in docs if re.search(rf'(?m)^kind: {re.escape(kind)}$',d)
                     and re.search(rf'(?m)^  name: {re.escape(name)}$',d)]
            self.assertEqual(len(matches),1,f'expected one {kind}/{name}')
            return matches[0]

        deployment=document('Deployment','newapi-proxy')
        self.assertRegex(deployment,r'(?m)^  replicas: 2$')
        env={}
        for name,secret,key in re.findall(
                r'(?ms)^            - name: (PROXY_[A-Z_]+)\n'
                r'\s+valueFrom:\n\s+secretKeyRef:\n'
                r'\s+name: ([^\n]+)\n\s+key: ([^\n]+)',deployment):
            env[name]=(secret.strip(),key.strip())
        self.assertEqual(env.get('PROXY_AP_TOKEN'),('newapi-proxy-pats','ap'))
        self.assertEqual(env.get('PROXY_OP_TOKEN'),('newapi-proxy-pats','op'))
        self.assertEqual(env.get('PROXY_AUTH_WEB'),('newapi-proxy-hmac','web'))
        self.assertEqual(env.get('PROXY_AUTH_CRON'),('newapi-proxy-hmac','cron'))

        policy=document('NetworkPolicy','newapi-proxy')
        ingress=re.search(r'(?ms)^  ingress:\n(.*?)^  egress:',policy)
        self.assertIsNotNone(ingress)
        ingress=ingress.group(1)
        client=re.search(
            r'(?ms)^(?P<indent> +)- namespaceSelector:\n(?P<namespace>.*?)'
            r'^(?P=indent)  podSelector:\n(?P<pod>.*?)(?=^(?P=indent)- |\Z)',ingress)
        self.assertIsNotNone(client,'namespace AND pod selectors must share one peer')
        self.assertRegex(client['namespace'],
            r'(?m)^\s+kubernetes\.io/metadata\.name: paas-system$')
        # toYaml may reorder mapping keys: both fields must belong to this
        # same pod selector, regardless of their serialized order.
        self.assertRegex(client['pod'],
            r'(?m)^\s+app\.kubernetes\.io/instance: paas-odoo-platform$')
        self.assertRegex(client['pod'],
            r'(?ms)\s+matchExpressions:\n'
            r'\s+- key: app\.kubernetes\.io/component\n'
            r'\s+operator: In\n'
            r'\s+values:\n'
            r'\s+- server\n'
            r'\s+- cron(?:\n|$)')
        namespaces=re.findall(r'(?m)^\s+kubernetes\.io/metadata\.name: ([^\s]+)$',ingress)
        self.assertEqual(namespaces,['paas-system'])
        self.assertNotIn('new-api-stg',ingress)

    def test_invalid_enable_or_zero_replica_cannot_masquerade_as_dormant(self):
        for proxy,err in [({'enabled':'false'},'proxy.enabled must be a boolean'),
                          ({'enabled':True,'replicas':0},'proxy.replicas must be a positive integer'),
                          ({'enabled':True,'replicas':1.5},'proxy.replicas must be a positive integer'),
                          ({'enabled':True,'replicas':2147483648},'proxy.replicas must be a positive integer')]:
            p=self.render(ROOT/'chart','prod',{'proxy':proxy})
            self.assertNotEqual(p.returncode,0);self.assertIn(err,p.stderr)


if __name__=='__main__':unittest.main()
