"""Exercise rollout and rollback against a fake Docker CLI; never contact a host."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy-server.sh'
IMAGE = 'ghcr.io/example/mail@sha256:' + 'a' * 64
SHA = 'b' * 40


@unittest.skipUnless(sys.platform.startswith('linux'), 'Deployment shell tests require Linux')
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        (self.root / 'secrets').mkdir()
        (self.root / '.env').write_text('LOCAL_UID=1000\nWEBHOOK_BIND=127.0.0.1\n')
        (self.root / 'compose.yaml').write_text('previous configuration\n')
        (self.root / 'secrets' / 'model.json').write_text('PRIVATE_CREDENTIAL')
        with sqlite3.connect(self.root / 'data' / 'agent.sqlite3') as db:
            db.execute('CREATE TABLE progress(id TEXT)')
            db.execute("INSERT INTO progress VALUES('already-sent')")
        release = self.root / 'releases' / SHA
        release.mkdir(parents=True)
        (release / 'compose.yaml').write_text('new configuration\n')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'docker'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as log:
    log.write(json.dumps(args)+'\\n')
if args[:1]==['ps']:
    if 'label=com.docker.compose.service=agent' in args: print('old-agent')
elif args[:1]==['compose'] and 'config' in args and '--format' in args:
    print(json.dumps({'services':{'agent':{'image':'old-image'}}}))
elif args[:1]==['compose'] and 'up' in args:
    if any('releases' in a for a in args):
        envfiles=[args[i+1] for i,a in enumerate(args) if a=='--env-file']
        assert len(envfiles)==2
        assert 'MAIL_AGENT_IMAGE=ghcr.io/' in pathlib.Path(envfiles[-1]).read_text()
        if os.environ.get('TEST_FAIL')=='1': sys.exit(1)
sys.exit(0)
''')
        fake.chmod(0o755)
        self.calls = self.root / 'calls.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def run_deploy(self, fail=False):
        env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
               'TEST_CALLS': str(self.calls), 'TEST_FAIL': '1' if fail else '0'}
        return subprocess.run(['bash', str(SCRIPT), str(self.root), SHA, IMAGE, 'false', 'telegram-mail-test'],
                              env=env, capture_output=True, text=True)

    def test_success_preserves_data_and_credentials_and_pins_image(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DEPLOY_OK', result.stdout)
        self.assertIn('MAIL_AGENT_IMAGE=' + IMAGE, (self.root / '.env').read_text())
        self.assertIn('WEBHOOK_BIND=127.0.0.1', (self.root / '.env').read_text())
        self.assertEqual((self.root / 'secrets' / 'model.json').read_text(), 'PRIVATE_CREDENTIAL')
        snapshot = next((self.root / 'backups').glob('*/agent.sqlite3'))
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('SELECT id FROM progress').fetchone()[0], 'already-sent')
        self.assertEqual((self.root / 'compose.yaml').read_text(), 'new configuration\n')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any('build' in call for call in calls))

    def test_unhealthy_release_restores_previous_running_services_only(self):
        result = self.run_deploy(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ROLLBACK_OK', result.stdout)
        self.assertNotIn('MAIL_AGENT_IMAGE=', (self.root / '.env').read_text())
        self.assertEqual((self.root / 'compose.yaml').read_text(), 'previous configuration\n')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        rollback = [c for c in calls if 'up' in c and any(a.endswith('compose.json') for a in c)][0]
        self.assertEqual(rollback[-1], 'agent')
        self.assertNotIn('dashboard', rollback)
        with sqlite3.connect(self.root / 'data' / 'agent.sqlite3') as db:
            self.assertEqual(db.execute('SELECT id FROM progress').fetchone()[0], 'already-sent')


if __name__ == '__main__':
    unittest.main()
