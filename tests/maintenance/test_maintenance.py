"""Offline Helm/runtime tests; optional real age + temporary PostgreSQL 16.

python3 -m unittest discover -s tests/maintenance -v
NA07_AGE_BIN=/path/to/age NA07_POSTGRES_BIN=/path/to/bin <same command>
No kubeconfig, service credentials or live DB connections are used.
"""
import json
import os
from pathlib import Path
import shutil
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

    def test_default_prod_stg_schedules_and_secrets(self):
        for site in [None, 'deploy/woow-k3s.yaml', 'deploy/woow-k3s-stg.yaml']:
            with self.subTest(site=site):
                p = self.render(site=site)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn('schedule: "45 */6 * * *"', p.stdout)
                self.assertIn('schedule: "30 4 * * *"', p.stdout)
                self.assertIn('timeZone: Asia/Taipei', p.stdout)
                self.assertIn('name: PGPASSWORD\n', p.stdout)
                self.assertNotIn('AGE-SECRET-KEY-', p.stdout)
                self.assertEqual(p.stdout.count('  suspend: true'), 2)

    def test_active_backup_requires_recipient_and_image_pin(self):
        for backup, error in [
            ({'suspend': False}, 'backup.ageRecipient'),
            ({'suspend': 'false'}, 'backup.suspend must be a boolean'),
            ({'enabled': 'false'}, 'backup.enabled must be a boolean'),
            ({'image': {'digest': 'sha256:bad'}}, 'backup.image.digest'),
            ({'suspend': False, 'ageRecipient': PUBLIC}, 'backup.image.digest'),
            ({'suspend': False, 'ageRecipient': PUBLIC, 'image': {'digest': 'sha256:'+'0'*64}}, 'backup.image.digest'),
            ({'ageRecipient': 'AGE-SECRET-KEY-DO-NOT-ACCEPT'}, 'public recipient'),
            ({'ageRecipient': PUBLIC+'\nINJECTED'}, 'public recipient'),
            ({'keepDays': 0}, 'backup.keepDays'),
            ({'keepDays': 1.5}, 'backup.keepDays'),
            ({'capacityWarningPercent': 101}, 'backup.capacityWarningPercent'),
        ]:
            with self.subTest(backup=backup):
                p=self.render({'backup': backup})
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(error, p.stderr)
        p=self.render({'backup': {'suspend': False, 'ageRecipient': PUBLIC,
                                 'image': {'digest': DIGEST, 'tag': 'test'}}}, 'backup.yaml')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('suspend: false', p.stdout)
        self.assertIn('@'+DIGEST, p.stdout)
        self.assertIn('command:\n                - bash\n                - /maintenance/backup.sh', p.stdout)
        self.assertIn('activeDeadlineSeconds: 3600', p.stdout)

    def test_retention_guards(self):
        for key, value in [('days', 0), ('days', '180; DROP TABLE logs'),
                           ('batchSize', 10001), ('batchSleepSeconds', 0), ('maxBatches', -1)]:
            p=self.render({'retention': {key: value}})
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('retention.'+key, p.stderr)

    def test_disabled_components_and_scoped_network(self):
        p=self.render({'backup': {'enabled': False}, 'retention': {'enabled': False}})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('kind: CronJob', p.stdout)
        self.assertNotIn('name: newapi-retention', p.stdout)
        p=self.render(template='networkpolicy.yaml')
        self.assertEqual(p.returncode, 0, p.stderr)
        postgres=p.stdout.split('  name: newapi-postgres\n',1)[1].split('---',1)[0]
        self.assertIn('app.kubernetes.io/component: retention', postgres)
        retention=p.stdout.split('  name: newapi-retention\n',1)[1]
        self.assertIn('app.kubernetes.io/instance: newapi', retention)
        self.assertIn('app.kubernetes.io/component: postgres', retention)
        self.assertIn('port: 5432', retention)
        self.assertNotIn('ipBlock', retention)

    def test_monitoring_covers_both_pvcs_jobs_and_stale_backup(self):
        p=self.render({'monitoring': {'enabled': True, 'labels': {'release': 'monitoring'}}}, 'monitoring.yaml')
        self.assertEqual(p.returncode, 0, p.stderr)
        for expected in ['kind: PrometheusRule', 'release: monitoring', 'data-newapi-postgres-',
                         'newapi-backup', '> 70', 'kube_job_status_failed', 'kube_job_owner',
                         'kube_cronjob_status_last_successful_time', 'kube_cronjob_created']:
            self.assertIn(expected, p.stdout)


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
                      AGE_RECIPIENT=PUBLIC, KEEP_DAYS='7', CAPACITY_WARNING_PERCENT='70')

    def execute(self, **env):
        return run(['bash', str(SCRIPTS/'backup.sh')], env=dict(self.env, **env))

    def test_success_prunes_at_seven_days_and_preserves_recent_and_unrelated(self):
        for name, days in [('newapi-expired.dump.age',8), ('newapi-exact.dump.age',7),
                           ('newapi-legacy.dump',8), ('newapi-recent.dump.age',6),
                           ('unrelated.age',9), ('newapi-stale.part',1)]:
            p=self.backups/name; p.write_text('old')
            ts=__import__('time').time()-days*86400; os.utime(p,(ts,ts))
        p=self.execute()
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertFalse((self.backups/'newapi-expired.dump.age').exists())
        self.assertFalse((self.backups/'newapi-exact.dump.age').exists())
        self.assertFalse((self.backups/'newapi-legacy.dump').exists())
        self.assertFalse((self.backups/'newapi-stale.part').exists())
        self.assertTrue((self.backups/'newapi-recent.dump.age').exists())
        self.assertTrue((self.backups/'unrelated.age').exists())
        self.assertIn('pg_database_size_bytes=12345',p.stdout)
        self.assertIn('backup_pvc_used_percent=70',p.stdout)
        new=[x for x in self.backups.glob('newapi-*.dump.age') if x.name!='newapi-recent.dump.age']
        self.assertEqual(len(new),1)
        self.assertNotIn('sk-',new[0].read_text())

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
