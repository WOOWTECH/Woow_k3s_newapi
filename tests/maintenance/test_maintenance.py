"""Offline Helm/runtime tests; optional real age + temporary PostgreSQL 16.

python3 -m unittest discover -s tests/maintenance -v
NA07_AGE_BIN=/path/to/age NA07_POSTGRES_BIN=/path/to/bin <same command>
No kubeconfig, service credentials or live DB connections are used.
"""
import json
import io
import re
import tarfile
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'chart/files/maintenance'
# Ephemeral test public recipient; identity exists only under /tmp during tests.
PUBLIC = 'age1j2gvs7t4h8cf5t6wpmt2ux423sguzpdhq26qcs6a8wtru29wd92qe8ateu'
DIGEST = 'sha256:' + 'a' * 64


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)


class HelmTests(unittest.TestCase):
    def render(self, values=None, template=None, site=None):
        args = ['helm', 'template', 'newapi', str(ROOT / 'chart'), '-n', 'new-api-stg']
        if site:
            args += ['-f', str(ROOT / site)]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
            json.dump(values or {}, f); f.flush()
            args += ['-f', f.name]
            if template:
                args += ['--show-only', 'templates/' + template]
            return run(args)

    def active_encrypted(self):
        return {'enabled': True, 'suspend': False, 'ageRecipient': PUBLIC,
                'image': {'digest': DIGEST, 'tag': 'test'},
                'capacityVerification': {'evidenceRef': 'offline-fixture/capacity-review',
                    'namespace': 'new-api-stg', 'release': 'newapi',
                    'storageClassName': 'longhorn', 'requestedSize': '20Gi'}}

    def verified_encrypted(self):
        enc=self.active_encrypted()
        enc['restoreVerification']={'archiveSha256': 'sha256:'+'b'*64,
            'evidenceRef': 'stg/restore-evidence-123', 'imageDigest': DIGEST,
            'recipient': PUBLIC, 'namespace': 'new-api-stg', 'release': 'newapi',
            'verifiedAt': '2026-10-21T12:00:00Z'}
        return enc

    def test_all_sites_dormant_without_new_pvc_and_legacy_backup_preserved(self):
        for site in [None, 'deploy/woow-k3s.yaml', 'deploy/woow-k3s-stg.yaml']:
            with self.subTest(site=site):
                p = self.render(site=site)
                self.assertEqual(p.returncode, 0, p.stderr)
                jobs = [d for d in p.stdout.split('---') if 'kind: CronJob' in d]
                self.assertEqual(len(jobs), 1)
                legacy = jobs[0]
                self.assertIn('  name: newapi-pg-backup\n', legacy)
                self.assertIn('schedule: "45 3 * * *"', legacy)
                self.assertIn('suspend: false', legacy)
                self.assertIn('value: "7"' if site == 'deploy/woow-k3s-stg.yaml' else 'value: "14"', legacy)
                self.assertIn('claimName: newapi-backup\n', legacy)
                for name in ['newapi-pg-backup-age', 'newapi-backup-age', 'newapi-retention']:
                    self.assertNotIn('  name: ' + name, p.stdout)
                for component in ['backup-encrypted', 'retention']:
                    self.assertNotIn('app.kubernetes.io/component: ' + component, p.stdout)

    def test_all_site_resource_identities_match_pre_na07(self):
        def identities(rendered):
            result=set()
            for doc in rendered.split('---'):
                kind=re.search(r'^kind: (\S+)$',doc,re.M)
                name=re.search(r'^  name: (\S+)$',doc,re.M)
                if kind and name: result.add((kind[1],name[1]))
            return result
        archive=subprocess.run(['git','archive','b775213','chart'],cwd=ROOT,capture_output=True)
        self.assertEqual(archive.returncode,0)
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                # Trusted tracked files from this fixed baseline; write files only.
                for member in tar.getmembers():
                    if member.isfile():
                        self.assertNotIn('..',Path(member.name).parts)
                        self.assertFalse(Path(member.name).is_absolute())
                        target=Path(tmp)/member.name;target.parent.mkdir(parents=True,exist_ok=True)
                        target.write_bytes(tar.extractfile(member).read())
            for site in [None,'deploy/woow-k3s.yaml','deploy/woow-k3s-stg.yaml']:
                args=['helm','template','newapi',str(Path(tmp)/'chart'),'-n','new-api-stg']
                if site: args+=['-f',str(ROOT/site)]
                baseline=run(args);current=self.render(site=site)
                self.assertEqual(baseline.returncode,0,baseline.stderr)
                self.assertEqual(current.returncode,0,current.stderr)
                self.assertEqual(identities(current.stdout),identities(baseline.stdout))

    def test_legacy_dump_validation_cleanup_and_runtime_unchanged_from_main(self):
        base=run(['git','show','b775213:chart/templates/backup.yaml'],cwd=ROOT)
        self.assertEqual(base.returncode,0,base.stderr)
        current=(ROOT/'chart/templates/backup.yaml').read_text()
        # The only template addition is an explicit default-false suspend field.
        self.assertEqual(current.replace('  suspend: {{ .Values.backup.suspend }}\n',''),base.stdout)

    def test_active_encrypted_requires_recipient_and_image_pin(self):
        for enc,error in [
            ({'suspend': False, 'ageRecipient': ''}, 'encryptedBackup.ageRecipient'),
            ({'suspend': 'false'}, 'encryptedBackup.suspend must be a boolean'),
            ({'enabled': 'false'}, 'encryptedBackup.enabled must be a boolean'),
            ({'pruneEnabled': 'false'}, 'encryptedBackup.pruneEnabled must be a boolean'),
            ({'image': {'digest': 'sha256:bad'}}, 'encryptedBackup.image.digest'),
            ({'suspend': False,'ageRecipient': PUBLIC, 'image': {'digest': ''}}, 'encryptedBackup.image.digest'),
            ({'suspend': False,'ageRecipient': PUBLIC,'image': {'digest': 'sha256:'+'0'*64}}, 'encryptedBackup.image.digest'),
            ({'ageRecipient': 'AGE-SECRET-KEY-DO-NOT-ACCEPT'}, 'public recipient'),
            ({'ageRecipient': PUBLIC+'\nINJECTED'}, 'public recipient'),
            ({'keepDays': 0}, 'encryptedBackup.keepDays'),
            ({'keepDays': 1.5}, 'encryptedBackup.keepDays'),
            ({'capacityWarningPercent': 101}, 'encryptedBackup.capacityWarningPercent'),
        ]:
            with self.subTest(enc=enc):
                base = self.active_encrypted()
                base['suspend'] = True
                base.update(enc)
                p=self.render({'encryptedBackup': base})
                self.assertNotEqual(p.returncode,0)
                self.assertIn(error,p.stderr)
        p=self.render({'encryptedBackup': self.active_encrypted()})
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(p.stdout.count('  suspend: false'),2)  # old + new run in parallel
        self.assertIn('@'+DIGEST,p.stdout)
        self.assertIn('activeDeadlineSeconds: 3600',p.stdout)

    def test_suspended_provisioning_requires_recipient_and_capacity_review(self):
        for suspend in [True, False]:
            enc = self.active_encrypted()
            enc['suspend'] = suspend
            enc['ageRecipient'] = ''
            p = self.render({'encryptedBackup': enc})
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('encryptedBackup.ageRecipient', p.stderr)
            for key, value in [('evidenceRef', ''), ('namespace', 'new-api'),
                               ('release', 'other'), ('storageClassName', 'other'),
                               ('requestedSize', '10Gi')]:
                with self.subTest(suspend=suspend, key=key):
                    enc = self.active_encrypted()
                    enc['suspend'] = suspend
                    enc['capacityVerification'][key] = value
                    p = self.render({'encryptedBackup': enc})
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn('capacityVerification', p.stderr)
        enc = self.active_encrypted()
        enc['suspend'] = True
        p = self.render({'encryptedBackup': enc})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('  name: newapi-backup-age', p.stdout)
        self.assertIn('  suspend: true', p.stdout)
        self.assertIn('schedule: "45 */6 * * *"', p.stdout)

    def test_capacity_evidence_is_bound_to_requested_size_and_environment(self):
        enc = self.active_encrypted()
        enc['size'] = '5Gi'
        p = self.render({'encryptedBackup': enc})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('capacityVerification', p.stderr)
        enc['capacityVerification']['requestedSize'] = '5Gi'
        self.assertEqual(self.render({'encryptedBackup': enc}).returncode, 0)
        # No key/evidence needed while absent: dormant images stay pinned.
        p = self.render({'encryptedBackup': {'enabled': False, 'ageRecipient': '',
                                             'capacityVerification': {'evidenceRef': ''}}})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_retirement_and_pruning_require_reviewed_restore_for_this_deployment(self):
        for key in ['enabled','suspend']:
            p=self.render({'backup': {key: 'false'}})
            self.assertNotEqual(p.returncode,0)
            self.assertIn('backup.'+key+' must be a boolean',p.stderr)
        for backup in [{'suspend': True},{'enabled': False}]:
            self.assertNotEqual(self.render({'backup': backup}).returncode,0)
            p=self.render({'backup': backup,'encryptedBackup': self.active_encrypted()})
            self.assertNotEqual(p.returncode,0)
            self.assertIn('restoreVerification',p.stderr)
            enc=self.verified_encrypted()
            p=self.render({'backup': backup,'encryptedBackup': enc})
            self.assertEqual(p.returncode,0,p.stderr)
            # Rolling back new encryption while the old track is retired is forbidden.
            enc['suspend']=True
            self.assertNotEqual(self.render({'backup': backup,'encryptedBackup': enc}).returncode,0)
        enc=self.active_encrypted();enc['pruneEnabled']=True
        self.assertNotEqual(self.render({'encryptedBackup': enc}).returncode,0)
        enc=self.verified_encrypted();enc['pruneEnabled']=True
        self.assertEqual(self.render({'encryptedBackup': enc}).returncode,0)
        for key,value in [('imageDigest','sha256:'+'c'*64),('recipient',PUBLIC+'x'),
                          ('namespace','new-api'),('release','other'),('verifiedAt',''),
                          ('evidenceRef',''),('archiveSha256','sha256:'+'0'*64)]:
            enc=self.verified_encrypted();enc['restoreVerification'][key]=value
            with self.subTest(key=key):
                p=self.render({'backup': {'suspend': True},'encryptedBackup': enc})
                self.assertNotEqual(p.returncode,0)
                self.assertIn('restoreVerification',p.stderr)

    def test_retention_guards(self):
        for key, value in [('days', 0), ('days', 179), ('days', 181), ('days', '180; DROP TABLE logs'),
                           ('batchSize', 10001), ('batchSleepSeconds', 0), ('maxBatches', -1)]:
            p=self.render({'retention': {'enabled': True,key: value}})
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('retention.'+key, p.stderr)

    def test_disabled_components_and_scoped_network(self):
        p=self.render({'encryptedBackup': {'enabled': False}, 'retention': {'enabled': False}})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count('kind: CronJob'),1)
        self.assertIn('  name: newapi-pg-backup\n',p.stdout)
        self.assertNotIn('  name: newapi-pg-backup-age\n',p.stdout)
        self.assertNotIn('name: newapi-retention', p.stdout)
        enc = self.active_encrypted()
        enc['capacityVerification']['requestedSize'] = '5Gi'
        p=self.render({'encryptedBackup': enc, 'retention': {'enabled': True}},
                      template='networkpolicy.yaml',site='deploy/woow-k3s-stg.yaml')
        self.assertEqual(p.returncode, 0, p.stderr)
        postgres=p.stdout.split('  name: newapi-postgres\n',1)[1].split('---',1)[0]
        self.assertIn('app.kubernetes.io/component: retention', postgres)
        retention=p.stdout.split('  name: newapi-retention\n',1)[1]
        self.assertIn('app.kubernetes.io/instance: newapi', retention)
        self.assertIn('app.kubernetes.io/component: postgres', retention)
        self.assertIn('port: 5432', retention)
        self.assertNotIn('ipBlock', retention)
        self.assertIn('app.kubernetes.io/component: backup-encrypted',postgres)
        age=p.stdout.split('  name: newapi-backup-age\n',1)[1]
        self.assertIn('app.kubernetes.io/component: backup-encrypted',age)
        self.assertIn('app.kubernetes.io/component: postgres',age)
        self.assertNotIn('ipBlock',age)

    def test_monitoring_covers_both_pvcs_jobs_and_stale_backup(self):
        p=self.render({'monitoring': {'enabled': True, 'labels': {'release': 'monitoring'}}}, 'monitoring.yaml')
        self.assertEqual(p.returncode, 0, p.stderr)
        for expected in ['kind: PrometheusRule', 'release: monitoring', 'data-newapi-postgres-',
                         'newapi-backup', '> 70', 'kube_job_status_failed',
                         'kube_cronjob_status_last_successful_time', 'kube_cronjob_created']:
            self.assertIn(expected, p.stdout)
        self.assertIn('alert: NewapiLegacyBackupStale',p.stdout)
        self.assertIn('> 25 * 3600',p.stdout)
        self.assertNotIn('alert: NewapiEncryptedBackupStale',p.stdout)
        p=self.render({'monitoring': {'enabled': True},'encryptedBackup': self.active_encrypted()},'monitoring.yaml')
        self.assertIn('alert: NewapiEncryptedBackupStale',p.stdout)
        self.assertIn('> 7 * 3600',p.stdout)
        self.assertIn('backup(-age)?',p.stdout)
        self.assertIn('job_name=~"newapi-(pg-backup(-age)?|retention)-[0-9]+"',p.stdout)
        self.assertNotIn('kube_job_owner',p.stdout)
        p=self.render({'backup': {'suspend': True},'encryptedBackup': self.verified_encrypted(),
                       'monitoring': {'enabled': True}},'monitoring.yaml')
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertNotIn('alert: NewapiLegacyBackupStale',p.stdout)
        self.assertIn('alert: NewapiEncryptedBackupStale',p.stdout)


STUB = '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
name=Path(sys.argv[0]).name
mode=os.environ.get('MODE','ok')
if name=='pg_isready': sys.exit(0)
if name=='sleep': sys.exit(0)
if name=='pg_dump':
    sys.stdout.write('PGDMP-test-sk-sensitive-content')
    sys.exit(17 if mode=='dump-fail' else 0)
if name=='age':
    data=sys.stdin.read()
    Path(sys.argv[sys.argv.index('-o')+1]).write_text('age-encryption.org/v1\\nCIPHERTEXT')
    sys.exit(19 if mode=='age-fail' else 0)
if name=='df':
    print('Filesystem 1024-blocks Used Available Capacity Mounted on')
    print('stub 100 71 29 '+os.environ.get('USED','70')+'% /backups')
if name=='psql':
    if mode=='psql-fail': sys.exit(21)
    print('12345')
'''


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.bin=self.base/'bin'; self.bin.mkdir()
        self.backups=self.base/'backups'; self.backups.mkdir()
        for tool in ['pg_dump','age','pg_isready','df','psql','sleep']:
            p=self.bin/tool; p.write_text(STUB); p.chmod(0o755)
        self.env=dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'], BACKUP_DIR=str(self.backups),
                      AGE_RECIPIENT=PUBLIC, KEEP_DAYS='7', CAPACITY_WARNING_PERCENT='70',PRUNE_ENABLED='false')

    def execute(self, **env):
        return run(['bash', str(SCRIPTS/'backup.sh')], env=dict(self.env, **env))

    def test_success_prunes_at_seven_days_and_preserves_recent_and_unrelated(self):
        for name, days in [('newapi-age-expired.dump.age',8), ('newapi-age-exact.dump.age',7),
                           ('newapi-legacy.dump',8), ('newapi-age-recent.dump.age',6),
                           ('unrelated.age',9), ('newapi-age-stale.dump.age.part',1)]:
            p=self.backups/name; p.write_text('old')
            ts=__import__('time').time()-days*86400; os.utime(p,(ts,ts))
        p=self.execute(PRUNE_ENABLED='true')
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertFalse((self.backups/'newapi-age-expired.dump.age').exists())
        self.assertFalse((self.backups/'newapi-age-exact.dump.age').exists())
        self.assertTrue((self.backups/'newapi-legacy.dump').exists())
        self.assertFalse((self.backups/'newapi-age-stale.dump.age.part').exists())
        self.assertTrue((self.backups/'newapi-age-recent.dump.age').exists())
        self.assertTrue((self.backups/'unrelated.age').exists())
        self.assertIn('pg_database_size_bytes=12345',p.stdout)
        self.assertIn('backup_pvc_used_percent=70',p.stdout)
        new=[x for x in self.backups.glob('newapi-age-*.dump.age') if x.name!='newapi-age-recent.dump.age']
        self.assertEqual(len(new),1)
        self.assertNotIn('sk-',new[0].read_text())

    def test_unverified_track_never_prunes_prior_archives_or_parts(self):
        old_names=['newapi-age-old.dump.age','newapi-legacy.dump','newapi-legacy.dump.part',
                   'newapi-age-stale.dump.age.part','newapi-old.dump.age']
        for name in old_names:
            p=self.backups/name;p.write_text('preserve');os.utime(p,(0,0))
        p=self.execute()
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('pruning disabled',p.stdout)
        for name in old_names:
            self.assertEqual((self.backups/name).read_text(),'preserve')

    def test_failed_dump_or_encryption_does_not_publish_or_prune_good_archives(self):
        old=self.backups/'newapi-old.dump.age'; old.write_text('previous-good')
        os.utime(old,(0,0))
        for mode in ['dump-fail','age-fail']:
            with self.subTest(mode=mode):
                p=self.execute(MODE=mode)
                self.assertNotEqual(p.returncode,0)
                self.assertEqual(list(self.backups.iterdir()),[old])

    def test_capacity_is_strictly_above_threshold_and_retains_new_backup(self):
        p=self.execute(USED='71')
        self.assertNotEqual(p.returncode,0)
        self.assertIn('capacity threshold exceeded',p.stderr)
        self.assertEqual(len(list(self.backups.glob('*.dump.age'))),1)
        p=self.execute(USED='2',CAPACITY_WARNING_PERCENT='1')
        self.assertNotEqual(p.returncode,0)

    def test_measurement_failure_fails_job(self):
        self.assertNotEqual(self.execute(USED='invalid').returncode,0)
        self.assertNotEqual(self.execute(MODE='psql-fail').returncode,0)

    @unittest.skipUnless(os.environ.get('NA07_AGE_BIN'), 'set NA07_AGE_BIN for real age round-trip')
    def test_real_age_roundtrip_wrong_identity_and_truncated_dump_failure(self):
        age=Path(os.environ['NA07_AGE_BIN']); keygen=age.with_name('age-keygen')
        identity=self.base/'identity'
        p=run([str(keygen),'-o',str(identity)])
        self.assertEqual(p.returncode,0,p.stderr)
        recipient=run([str(keygen),'-y',str(identity)]).stdout.strip()
        (self.bin/'age').unlink(); (self.bin/'age').symlink_to(age)
        p=self.execute(AGE_RECIPIENT=recipient)
        self.assertEqual(p.returncode,0,p.stderr)
        archive=next(self.backups.glob('*.dump.age'))
        self.assertNotIn(b'sk-sensitive',archive.read_bytes())
        p=run([str(age),'-d','-i',str(identity),str(archive)])
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(p.stdout,'PGDMP-test-sk-sensitive-content')
        wrong=self.base/'wrong'; run([str(keygen),'-o',str(wrong)])
        self.assertNotEqual(run([str(age),'-d','-i',str(wrong),str(archive)]).returncode,0)
        before=set(self.backups.iterdir())
        self.assertNotEqual(self.execute(AGE_RECIPIENT=recipient,MODE='dump-fail').returncode,0)
        self.assertEqual(set(self.backups.iterdir()),before)


if __name__ == '__main__':
    unittest.main()
