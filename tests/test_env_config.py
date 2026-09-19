import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import env_config as e
import mailagent as a
import ngrok_setup as n
import webhook as w


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(a, DATA=self.root / 'data', SECRETS=self.root / 'secrets')
        self.paths.start()
        self.file = self.root / '.env'
        self.file.write_text('LOCAL_UID=0\nMAIL_AGENT_IMAGE=pinned-image\n', encoding='utf-8')
        self.credentials = {
            'telegram': {'token': 'private-telegram', 'chat_id': '7558581320'},
            'gmail': {'email': 'user@example.invalid', 'app_password': 'old-password'},
            'gmail_oauth': {'topic': 'projects/example-project/topics/gmail-events', 'client_id': 'client',
                            'client_secret': 'private-google', 'refresh_token': 'private-refresh'},
            'model': {'endpoint': 'https://example.invalid/chat/completions', 'model': 'test-model', 'api_key': 'private-model'},
            'webhook': {'audience': 'https://example.ngrok.app/webhooks/gmail',
                        'service_account': 'gmail-webhook-push@example-project.iam.gserviceaccount.com',
                        'subscription': 'projects/example-project/subscriptions/gmail-push'},
        }
        for name, value in self.credentials.items():
            a.save_secret(name, value)
        directory = a.SECRETS / 'ngrok'
        directory.mkdir()
        (directory / 'config.yml').write_text(json.dumps(n.config('example.ngrok.app', 'private-ngrok')))

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def migrate(self):
        with patch.object(a, 'exclusive', contextlib.nullcontext):
            a.migrate_env()

    def test_migration_roundtrip_no_output_and_legacy_files_retained(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.migrate()
            self.migrate()
        self.assertIn('ENV_MIGRATED', output.getvalue())
        self.assertNotIn('private-', output.getvalue())
        self.assertEqual(e.read(self.file)['MAIL_AGENT_IMAGE'], 'pinned-image')
        for name, value in self.credentials.items():
            self.assertEqual(a.secret(name), value)
            self.assertTrue((a.SECRETS / (name + '.json')).exists())
        if os.name != 'nt':
            self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_environment_only_recovery_setup_and_watch_renewal(self):
        self.migrate()
        for path in a.SECRETS.glob('*.json'):
            path.unlink()
        (a.SECRETS / 'ngrok/config.yml').unlink()
        self.assertTrue(a.has_secret('gmail_oauth'))
        for name, value in self.credentials.items():
            self.assertEqual(a.secret(name), value)
        n.render(self.root)
        cfg = json.loads((a.SECRETS / 'ngrok/config.yml').read_text())
        self.assertEqual(cfg['agent']['authtoken'], 'private-ngrok')
        self.assertNotIn('private-google', json.dumps(cfg))
        a.save_secret('telegram', {'token': 'replacement-token', 'chat_id': '7558581320'})
        self.assertEqual(a.secret('telegram')['token'], 'replacement-token')
        self.assertFalse((a.SECRETS / 'telegram.json').exists())
        with patch.object(w, 'renew_watch') as renew, patch.object(w.STOP, 'is_set', side_effect=[False, True]), \
             patch.object(w.STOP, 'wait'):
            w.maintain_watch()
        renew.assert_called_once()

    def test_conflict_or_invalid_file_leaves_original_untouched(self):
        e.update(self.file, {'TELEGRAM_BOT_TOKEN': 'different-token'})
        before = self.file.read_bytes()
        with self.assertRaisesRegex(a.Failure, 'env_migration_conflict'):
            self.migrate()
        self.assertEqual(self.file.read_bytes(), before)
        self.file.write_text("TELEGRAM_BOT_TOKEN='never-print-me\n")
        with self.assertRaises(a.Failure) as error:
            a.secret('telegram')
        self.assertNotIn('never-print-me', str(error.exception))

    def test_no_fallback_to_old_credentials_when_unified_fields_missing(self):
        e.update(self.file, {'ENV_CONFIG_VERSION': '1', 'TELEGRAM_CHAT_ID': '7558581320'})
        with self.assertRaisesRegex(a.Failure, 'missing_or_invalid_telegram'):
            a.secret('telegram')

    def test_migration_preserves_active_model_and_prompt(self):
        with contextlib.closing(a.connect()) as db:
            with db:
                a.put(db, 'model_active', json.dumps({'model': 'selected-model',
                      'endpoint': 'https://example.invalid/selected', 'prompt': 'Imported prompt',
                      'credential_digest': a.credential_digest(a.secret('model'))}))
        self.migrate()
        values = e.read(self.file)
        self.assertEqual(values['SUMMARY_PROMPT'], 'Imported prompt')
        self.assertEqual(values['MODEL_NAME'], 'selected-model')
        self.assertEqual(a.model_config()['model'], 'selected-model')
        self.assertEqual(a.model_config()['prompt'], 'Imported prompt')

    def test_literals_prompt_override_and_invalid_syntax(self):
        values = {'TEST_VALUE': "literal $TOKEN # text's \\ backslash", 'EMPTY': '',
                  'SUMMARY_PROMPT': 'Summarize clearly.\nUse English.'}
        e.update(self.file, values)
        self.assertTrue(values.items() <= e.read(self.file).items())
        self.assertEqual(a.model_config()['prompt'], values['SUMMARY_PROMPT'])
        for bad in ('KEY=x\nKEY=y\n', "KEY='unclosed\n", 'KEY="bad\\q"\n', 'KEY="ok" junk\n'):
            self.file.write_text(bad)
            with self.assertRaises(e.ConfigError):
                e.read(self.file)


if __name__ == '__main__':
    unittest.main()
