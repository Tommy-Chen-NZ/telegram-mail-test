import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mailagent as a
import ngrok_setup as n


class NgrokTests(unittest.TestCase):
    def test_setup_preserves_image_and_writes_private_scoped_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env').write_text('LOCAL_UID=1000\nMAIL_AGENT_IMAGE=KEEP\nWEBHOOK_BIND=0.0.0.0\n')
            url = n.configure(root, 'gully-gallantly-luckless.ngrok-free.dev', 'test-only-token',
                              'storied-precept-509003-e1')
            self.assertEqual(url, 'https://gully-gallantly-luckless.ngrok-free.dev/webhooks/gmail')
            self.assertIn('MAIL_AGENT_IMAGE=KEEP', (root / '.env').read_text())
            self.assertIn('WEBHOOK_BIND=127.0.0.1', (root / '.env').read_text())
            cfg = json.loads((root / 'secrets/ngrok/config.yml').read_text())
            self.assertEqual(cfg['agent']['inspect_db_size'], -1)
            self.assertFalse(cfg['agent']['web_addr'])
            self.assertEqual(cfg['endpoints'][0]['upstream']['url'], 'http://127.0.0.1:8080')
            self.assertNotIn('test-only-token', (root / 'secrets/webhook.json').read_text())
            if os.name != 'nt':
                self.assertEqual((root / 'secrets/ngrok/config.yml').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(a.Failure):
                n.configure(root, 'different.ngrok.app', 'test-only-token', 'storied-precept-509003-e1')

    def test_invalid_domain_does_not_disclose_token(self):
        for domain in ('http://example.ngrok.app', 'example.ngrok.app/other',
                       'example.ngrok.app?key=secret', 'example.ngrok.app.evil.com',
                       'localhost', 'example.ngrok.app:8080'):
            with self.assertRaises(a.Failure) as error:
                n.config(domain, 'test-only-token')
            self.assertNotIn('test-only-token', str(error.exception))

    def test_verification_requires_exact_backend_response_and_unsigned_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env').write_text('LOCAL_UID=1000\n')
            n.configure(root, 'example.ngrok.app', 'test-only-token', 'example-project')
            health = (200, b'', 'application/json')
            rejected = (401, b'{"error":"unauthorized"}', 'application/json')
            output = io.StringIO()
            with patch.object(n, 'request_result', side_effect=[health, health, rejected]), contextlib.redirect_stdout(output):
                n.verify(root)
            self.assertIn('TUNNEL_VERIFIED', output.getvalue())
            self.assertNotIn('test-only-token', output.getvalue())
            with patch.object(n, 'request_result', side_effect=[health, (200, b'ngrok error', 'text/html')]):
                with self.assertRaisesRegex(a.Failure, 'public_webhook_health_failed'):
                    n.verify(root)
