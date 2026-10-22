"""Optional temporary PostgreSQL 16 integration (Unix socket only, never live).

Set NA07_POSTGRES_BIN to server bin dir and NA07_POSTGRES_CLIENT_BIN to client
bin dir, NA07_AGE_BIN to age executable; all databases/identities use temp dirs.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_maintenance import ROOT, SCRIPTS, run


class SchemaProvenanceTests(unittest.TestCase):
    def test_schema_evidence_matches_pinned_tag_and_bigint_declarations(self):
        fixture=json.loads((ROOT/'tests/maintenance/fixtures/rc44-schema.json').read_text())
        self.assertIn('tag: '+fixture['tag'],(ROOT/'chart/values.yaml').read_text())
        for model in fixture['models'].values():
            self.assertRegex(model['created_at_declaration'],r'CreatedAt\s+int64')
            self.assertIn('bigint',model['created_at_declaration'])
            self.assertEqual(model['unit'],'unix_seconds')
            self.assertRegex(model['sha256'],r'^[a-f0-9]{64}$')


@unittest.skipUnless(os.environ.get('NA07_POSTGRES_BIN') and os.environ.get('NA07_POSTGRES_CLIENT_BIN'),
                     'set NA07_POSTGRES_BIN + NA07_POSTGRES_CLIENT_BIN for temporary PostgreSQL 16')
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='na07-pg-')
        cls.base=Path(cls.tmp.name); cls.data=cls.base/'data'; cls.sock=cls.base/'socket'; cls.sock.mkdir()
        cls.server=Path(os.environ['NA07_POSTGRES_BIN']); cls.client=Path(os.environ['NA07_POSTGRES_CLIENT_BIN'])
        cls.env=dict(os.environ, PGHOST=str(cls.sock), PGPORT='56437', PGUSER='na07', PGDATABASE='postgres',
                     PATH=str(cls.client)+':'+str(cls.server)+':'+os.environ['PATH'],
                     RETENTION_DAYS='180', BATCH_SIZE='10000', BATCH_SLEEP_SECONDS='1', MAX_BATCHES='1000',
                     MAINTENANCE_DIR=str(SCRIPTS))
        p=run([str(cls.server/'initdb'),'-D',str(cls.data),'-U','na07','--auth=trust','--no-locale','--encoding=UTF8'],env=cls.env)
        if p.returncode:
            cls.tmp.cleanup(); raise RuntimeError(p.stderr)
        # Disable TCP; socket directory and data are private to this test.
        p=run([str(cls.server/'pg_ctl'),'-D',str(cls.data),'-l',str(cls.base/'server.log'),
               '-o',f"-k {cls.sock} -h '' -p 56437",'-w','start'],env=cls.env)
        if p.returncode:
            cls.tmp.cleanup(); raise RuntimeError(p.stderr)

    @classmethod
    def tearDownClass(cls):
        try:
            p=run([str(cls.server/'pg_ctl'),'-D',str(cls.data),'-m','immediate','-w','stop'],env=cls.env)
            if p.returncode: raise RuntimeError(p.stderr)
        finally:
            cls.tmp.cleanup()

    def sql(self, text, database='postgres'):
        p=run([str(self.client/'psql'),'-XqAt','-v','ON_ERROR_STOP=1','-c',text],env=dict(self.env,PGDATABASE=database))
        self.assertEqual(p.returncode,0,p.stderr)
        return p.stdout.strip()

    def setUp(self):
        # rc.44 GORM id primary key and created_at bigint; payload is synthetic.
        self.sql('DROP TABLE IF EXISTS logs, audit_logs; '
                 'CREATE TABLE logs(id bigserial PRIMARY KEY,created_at bigint,content text); '
                 'CREATE INDEX ON logs(created_at,id); '
                 'CREATE TABLE audit_logs(id bigserial PRIMARY KEY,created_at bigint,content text); '
                 'CREATE INDEX ON audit_logs(created_at);')

    def test_retention_commits_multiple_batches_and_preserves_recent_rows(self):
        for table in ['logs','audit_logs']:
            self.sql(f"INSERT INTO {table}(created_at,content) SELECT floor(extract(epoch FROM now()))::bigint-200*86400,'old' FROM generate_series(1,10001); "
                     f"INSERT INTO {table}(created_at,content) VALUES (floor(extract(epoch FROM now()))::bigint,'recent'), (floor(extract(epoch FROM now()))::bigint-180*86400+60,'inside-boundary');")
        p=run(['bash',str(SCRIPTS/'retention.sh')],env=self.env)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('batch=1 deleted=10000',p.stdout)
        self.assertIn('batch=2 deleted=1',p.stdout)
        for table in ['logs','audit_logs']:
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table}'), '2')
            self.assertEqual(self.sql(f"SELECT count(*) FROM {table} WHERE content='old'"),'0')

    def test_schema_mismatch_fails_before_deleting(self):
        self.sql("INSERT INTO logs(created_at,content) VALUES (0,'preserve'); ALTER TABLE audit_logs ALTER COLUMN created_at TYPE timestamp USING to_timestamp(created_at);")
        p=run(['bash',str(SCRIPTS/'retention.sh')],env=self.env)
        self.assertNotEqual(p.returncode,0)
        self.assertIn('bigint schema required',p.stderr)
        self.assertEqual(self.sql('SELECT count(*) FROM logs'),'1')

    def test_batch_limit_fails_instead_of_unbounded_cleanup(self):
        self.sql('INSERT INTO logs(created_at) VALUES (0),(1)')
        p=run(['bash',str(SCRIPTS/'retention.sh')],env=dict(self.env,BATCH_SIZE='1',MAX_BATCHES='1'))
        self.assertNotEqual(p.returncode,0)
        self.assertIn('batch limit reached',p.stderr)
        self.assertEqual(self.sql('SELECT count(*) FROM logs'),'1')

    def test_sql_strict_cutoff_and_skip_locked(self):
        self.sql('INSERT INTO logs(id,created_at) VALUES (1,99),(2,100),(3,101),(4,98)')
        lock=subprocess.Popen([str(self.client/'psql'),'-XqAt','-v','ON_ERROR_STOP=1'],
                              env=self.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True)
        try:
            lock.stdin.write('BEGIN; SELECT id FROM logs WHERE id=1 FOR UPDATE;\n')
            lock.stdin.flush()
            self.assertEqual(lock.stdout.readline().strip(),'1')
            p=run([str(self.client/'psql'),'-XqAt','-v','ON_ERROR_STOP=1','-v','table=logs',
                   '-v','cutoff=100','-v','batch_size=10000','-f',str(SCRIPTS/'retention-batch.sql')],env=self.env)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(p.stdout.strip(),'1')
            self.assertEqual(self.sql('SELECT string_agg(id::text,\',\' ORDER BY id) FROM logs'),'1,2,3')
        finally:
            lock.communicate('COMMIT;\n',timeout=5)
            self.assertEqual(lock.returncode,0)

    @unittest.skipUnless(os.environ.get('NA07_AGE_BIN'),'set NA07_AGE_BIN for actual pg_dump/age/pg_restore')
    def test_real_encrypted_dump_restores_to_temporary_database(self):
        self.sql("INSERT INTO logs(created_at,content) VALUES (123,'sk-synthetic-only'); INSERT INTO audit_logs(created_at,content) VALUES (456,'test-audit');")
        age=Path(os.environ['NA07_AGE_BIN']); keygen=age.with_name('age-keygen')
        identity=self.base/'test-identity'
        p=run([str(keygen),'-o',str(identity)])
        self.assertEqual(p.returncode,0,p.stderr)
        recipient=run([str(keygen),'-y',str(identity)]).stdout.strip()
        backups=self.base/'backups'; backups.mkdir()
        env=dict(self.env,PATH=str(age.parent)+':'+self.env['PATH'],BACKUP_DIR=str(backups),
                 AGE_RECIPIENT=recipient,KEEP_DAYS='7',CAPACITY_WARNING_PERCENT='100')
        p=run(['bash',str(SCRIPTS/'backup.sh')],env=env)
        self.assertEqual(p.returncode,0,p.stderr)
        archive=next(backups.glob('*.dump.age'))
        data=archive.read_bytes()
        self.assertTrue(data.startswith(b'age-encryption.org/v1'))
        self.assertNotIn(b'sk-synthetic',data)
        dec=subprocess.run([str(age),'-d','-i',str(identity),str(archive)],capture_output=True)
        self.assertEqual(dec.returncode,0,dec.stderr.decode())
        self.sql('CREATE DATABASE na07_restore')
        p=subprocess.run([str(self.client/'pg_restore'),'--exit-on-error','--no-owner','--no-privileges','-d','na07_restore'],
                         input=dec.stdout,capture_output=True,env=env)
        self.assertEqual(p.returncode,0,p.stderr.decode())
        self.assertEqual(self.sql('SELECT content FROM logs','na07_restore'),'sk-synthetic-only')
        self.assertEqual(self.sql('SELECT content FROM audit_logs','na07_restore'),'test-audit')
