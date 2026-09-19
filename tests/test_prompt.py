import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mailagent as a


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(a, DATA=self.root / 'data', SECRETS=self.root / 'secrets')
        self.paths.start()
        self.environment = patch.dict(os.environ, {'SUMMARY_PROMPT': ''})
        self.environment.start()
        self.db = a.connect()
        a.save_secret('model', {'endpoint': 'https://example.invalid/chat/completions',
                               'model': 'example', 'api_key': 'PRIVATE-KEY'})

    def tearDown(self):
        self.db.close()
        self.environment.stop()
        self.paths.stop()
        self.temp.cleanup()

    def test_import_preserves_model_selection_and_secrets_requires_reverification(self):
        secret_before = (a.SECRETS / 'model.json').read_bytes()
        with self.db:
            a.put(self.db, 'model_active', json.dumps({'model': 'selected-model',
                  'endpoint': 'https://example.invalid/selected', 'prompt': 'Old prompt',
                  'credential_digest': a.credential_digest(a.secret('model'))}))
            a.put(self.db, 'cross_email_memory', 'Existing memory')
        digest = a.fingerprint('model')
        with self.db:
            a.put(self.db, 'model_verified', digest)
        source = self.root / 'prompt.txt'
        source.write_text('New English prompt', encoding='utf-8-sig')
        output = io.StringIO()
        with patch.object(a, 'exclusive', contextlib.nullcontext), contextlib.redirect_stdout(output):
            a.set_prompt(source)
        cfg = a.model_config()
        self.assertEqual(cfg['model'], 'selected-model')
        self.assertEqual(cfg['endpoint'], 'https://example.invalid/selected')
        self.assertEqual(cfg['prompt'], 'New English prompt')
        self.assertEqual((a.SECRETS / 'model.json').read_bytes(), secret_before)
        self.assertNotIn('PRIVATE-KEY', output.getvalue())
        self.assertNotIn('PRIVATE-KEY', a.meta(self.db, 'model_active'))
        self.assertEqual(a.meta(self.db, 'cross_email_memory'), 'Existing memory')
        with self.assertRaisesRegex(a.Failure, 'verify_model_first'):
            a.require_verified(self.db, 'model')

    def test_invalid_files_and_running_worker_leave_config_unchanged(self):
        source = self.root / 'prompt.txt'
        for data in (b'', b'  ', b'x' * 8193, b'\xff', b'hello\x00world'):
            source.write_bytes(data)
            with self.assertRaises(a.Failure):
                a.set_prompt(source)
            self.assertIsNone(a.meta(self.db, 'model_active'))
        source.write_text('Valid prompt', encoding='utf-8')
        with patch.object(a, 'exclusive', side_effect=a.Failure('worker_already_running')):
            with self.assertRaisesRegex(a.Failure, 'worker_already_running'):
                a.set_prompt(source)
        self.assertIsNone(a.meta(self.db, 'model_active'))

    def test_environment_overrides_saved_prompt_without_changing_storage(self):
        with self.db:
            a.put(self.db, 'model_active', json.dumps({'model': 'selected',
                  'endpoint': 'https://example.invalid/selected', 'prompt': 'Saved prompt',
                  'credential_digest': a.credential_digest(a.secret('model'))}))
        digest = a.fingerprint('model')
        with self.db:
            a.put(self.db, 'model_verified', digest)
        with patch.dict(os.environ, {'SUMMARY_PROMPT': '  Environment prompt\nWith two lines.  '}):
            cfg = a.model_config()
            self.assertEqual(cfg['prompt'], 'Environment prompt\nWith two lines.')
            self.assertEqual(cfg['model'], 'selected')
            with self.assertRaisesRegex(a.Failure, 'verify_model_first'):
                a.require_verified(self.db, 'model')
        self.assertEqual(a.model_config()['prompt'], 'Saved prompt')
        a.require_verified(self.db, 'model')
        with patch.dict(os.environ, {'SUMMARY_PROMPT': '   '}):
            self.assertEqual(a.model_config()['prompt'], 'Saved prompt')

    def test_environment_prompt_validation_and_default_fallback(self):
        self.assertEqual(a.model_config()['prompt'], a.DEFAULT_PROMPT)
        for value, code in (('x' * 8193, 'prompt_too_large'), ('\u00e9' * 4097, 'prompt_too_large')):
            with patch.dict(os.environ, {'SUMMARY_PROMPT': value}):
                with self.assertRaisesRegex(a.Failure, code):
                    a.model_config()


if __name__ == '__main__':
    unittest.main()
