"""Server pull orchestration with a fake Docker CLI and real bundle extraction."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]
SHA = 'b' * 40
REPOSITORY = 'ghcr.io/tommy-chen-nz/telegram-mail-test'
DIGEST = REPOSITORY + '@sha256:' + 'a' * 64


@unittest.skipUnless(sys.platform.startswith('linux'), 'Pull tests require Linux')
class PullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copy(SOURCE / 'scripts/pull-release.sh', self.root / 'scripts')
        self.bundle = self.root / 'bundle'
        (self.bundle / 'scripts').mkdir(parents=True)
        for name in ('compose.yaml', 'prepare.sh', 'mailagent.py', 'dashboard.py',
                     'demo_dashboard.py', 'webhook.py', 'gmail_api.py', 'requirements.txt'):
            shutil.copy(SOURCE / name, self.bundle / name)
        shutil.copy(SOURCE / 'scripts/pull-release.sh', self.bundle / 'scripts')
        (self.bundle / 'scripts/deploy-server.sh').write_text(
            '#!/bin/bash\nset -eu\nprintf "%s\\n" "$@" > "$1/deploy-args"\necho DEPLOY_OK\n')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import os, sys, tarfile
args = sys.argv[1:]
if args[:2] == ['image','inspect']:
    if args[3] == '{{.Id}}': print('sha256:' + 'c'*64)
    elif 'Labels' in args[3]: print(os.environ.get('TEST_REVISION','b'*40))
    else: print('ghcr.io/tommy-chen-nz/telegram-mail-test@sha256:' + 'a'*64)
elif args[:1] == ['create']:
    assert args[1:3] == ['--network','host']
    assert args[-1] == 'sha256:' + 'c'*64
    print('fake-container')
elif args[:1] == ['cp']:
    assert args[1:] == ['fake-container:/opt/deployment/.', '-']
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        archive.add(os.environ['TEST_BUNDLE'], arcname='.')
''')
        docker.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_pull(self, mode, revision=SHA):
        env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
               'TEST_BUNDLE': str(self.bundle), 'TEST_REVISION': revision}
        return subprocess.run(['bash', str(self.root / 'scripts/pull-release.sh'),
                               REPOSITORY + ':sha-' + SHA, mode],
                              env=env, capture_output=True, text=True)

    def test_prepare_pins_digest_and_never_starts_services(self):
        result = self.run_pull('prepare')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PULL_READY', result.stdout)
        self.assertIn('MAIL_AGENT_IMAGE=' + DIGEST, (self.root / '.env').read_text())
        self.assertTrue((self.root / 'secrets').is_dir())
        self.assertFalse((self.root / 'deploy-args').exists())
        again = self.run_pull('prepare')
        self.assertNotEqual(again.returncode, 0)

    def test_deploy_uses_matching_bundle_and_preserves_state(self):
        self.assertEqual(self.run_pull('prepare').returncode, 0)
        (self.root / 'secrets/token').write_text('KEEP')
        result = self.run_pull('deploy')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'deploy-args').read_text().splitlines(),
                         [str(self.root), SHA, DIGEST, 'false', 'telegram-mail-test'])
        self.assertEqual((self.root / 'secrets/token').read_text(), 'KEEP')
        self.assertEqual((self.root / 'gmail_api.py').read_text(), (SOURCE / 'gmail_api.py').read_text())

    def test_revision_mismatch_stops_before_preparation(self):
        result = self.run_pull('prepare', revision='d'*40)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env').exists())
