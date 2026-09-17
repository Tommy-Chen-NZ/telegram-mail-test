import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import mailagent as a


MAIL = {'from': 'sender@example.invalid', 'subject': 'Test', 'body': 'Submit the report by Friday.',
        'truncated': False, 'attachments_read': False}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(a, DATA=Path(self.temp.name) / 'data', SECRETS=Path(self.temp.name) / 'secrets')
        self.paths.start()
        self.db = a.connect()

    def tearDown(self):
        self.db.close()
        self.paths.stop()
        self.temp.cleanup()

    def job(self, key='100'):
        with self.db:
            self.db.execute('INSERT INTO jobs(id,received,discovered,mail) VALUES(?,?,?,?)',
                            (key, time.time() - 1, time.time(), json.dumps(MAIL)))

    def state(self):
        return self.db.execute('SELECT * FROM jobs').fetchone()

    def test_send_retry_reuses_persisted_summary_and_keeps_events(self):
        self.job()
        counts = {'model': 0, 'send': 0}
        def summarize(mail, record):
            counts['model'] += 1
            return 'Submit the report by Friday.'
        def send(text):
            counts['send'] += 1
            if counts['send'] == 1:
                raise a.Failure('http_429', 90)
            return {'message_id': 42}
        a.work_one(self.db, summarize, send)
        self.assertEqual(self.state()['state'], 'retry')
        self.assertGreater(self.state()['next_try'], time.time() + 85)
        self.assertFalse(a.work_one(self.db, summarize, send))
        with self.db:
            self.db.execute('UPDATE jobs SET next_try=0')
        a.work_one(self.db, summarize, send)
        self.assertEqual(counts, {'model': 1, 'send': 2})
        self.assertEqual(self.state()['state'], 'sent')
        self.assertEqual(self.state()['mail'], '{}')
        self.assertEqual(self.state()['telegram_id'], 42)
        self.assertFalse(a.work_one(self.db, summarize, send))
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='retry'").fetchone()[0], 1)

    def test_real_process_crash_recovery_and_uncertainty_record(self):
        self.job()
        code = "import mailagent as a, os; d=a.connect(); d.execute(\"UPDATE jobs SET state='sending',summary='saved'\"); d.commit(); os._exit(9)"
        env = dict(os.environ, AGENT_DATA=str(a.DATA))
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True)
        self.assertEqual(result.returncode, 9)
        a.recover(self.db)
        self.assertEqual(self.state()['state'], 'retry')
        self.assertEqual(self.db.execute("SELECT code FROM events WHERE kind='recovered'").fetchone()[0], 'delivery_uncertain')
        a.work_one(self.db, lambda *_: self.fail('summary regenerated'), lambda _: {'message_id': 7})
        self.assertEqual(self.state()['state'], 'sent')

    def test_backup_restores_pending_jobs_checkpoint_and_retry(self):
        self.job()
        with self.db:
            a.put(self.db, 'cursor', 123)
            self.db.execute("UPDATE jobs SET state='retry',attempts=4,next_try=500,last_error='http_429'")
            a.event(self.db, '100', 'retry', 'http_429', 4)
        target = Path(self.temp.name) / 'backup.sqlite3'
        a.backup(str(target))
        restored = sqlite3.connect(target)
        self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(restored.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0], '123')
        self.assertEqual(restored.execute('SELECT state,attempts,last_error FROM jobs').fetchone(), ('retry', 4, 'http_429'))
        self.assertEqual(restored.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        restored.close()

    def test_restore_command_preserves_progress_and_refuses_overwrite(self):
        self.job()
        with self.db:
            a.put(self.db, 'cursor', 456)
        snapshot = Path(self.temp.name) / 'restore-source.sqlite3'
        a.backup(str(snapshot))
        destination = Path(self.temp.name) / 'new-host'
        destination.mkdir()
        # Windows has no fcntl; the production Linux lock is separate from restore logic.
        with patch.object(a, 'DATA', destination), patch.object(a, 'exclusive', contextlib.nullcontext):
            a.restore(str(snapshot))
            with contextlib.closing(a.connect()) as restored:
                self.assertEqual(a.meta(restored, 'cursor'), '456')
                self.assertEqual(restored.execute('SELECT state FROM jobs').fetchone()[0], 'pending')
            with self.assertRaisesRegex(a.Failure, 'restore_requires_empty_data_directory'):
                a.restore(str(snapshot))

    def test_model_observes_tool_result_before_submission(self):
        def response(name, args):
            return {'choices': [{'message': {'content': None, 'tool_calls': [{'id': name, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}
        trace, payloads = [], []
        responses = [response('submit_summary', {'summary': 'Premature submission'}),
                     response('read_mail', {'offset': 0}), response('submit_summary', {'summary': 'Submit the report by Friday.'})]
        def model(url, payload, key, timeout):
            payloads.append(json.loads(json.dumps(payload)))
            return responses.pop(0)
        with patch.object(a, 'secret', return_value={'endpoint': 'https://example.invalid', 'model': 'test', 'api_key': 'fake'}), patch.object(a, 'post', side_effect=model):
            result = a.agent_summary(MAIL, lambda kind, code: trace.append(code))
        self.assertEqual(result, 'Submit the report by Friday.')
        self.assertEqual(trace, ['submit_summary', 'read_mail', 'submit_summary'])
        self.assertEqual(payloads[2]['messages'][-1]['role'], 'tool')
        self.assertIn(MAIL['body'], json.loads(payloads[2]['messages'][-1]['content'])['body'])

    def test_tool_loop_is_bounded(self):
        response = {'choices': [{'message': {'tool_calls': [{'id': 'x', 'function': {'name': 'shell', 'arguments': '{}'}}]}}]}
        with patch.object(a, 'secret', return_value={'endpoint': 'https://example.invalid', 'model': 'test', 'api_key': 'fake'}), patch.object(a, 'post', return_value=response) as model:
            with self.assertRaisesRegex(a.Failure, 'agent_step_limit'):
                a.agent_summary(MAIL)
            self.assertEqual(model.call_count, 4)

    def test_mail_parsing_skips_attachment_and_active_html(self):
        raw = b'Subject: HTML\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello</p><script>SECRET</script><p>World</p>'
        result = a.parse_mail(raw, True)
        self.assertIn('Hello', result['body'])
        self.assertNotIn('SECRET', result['body'])
        self.assertTrue(result['truncated'])

    def test_stage_gates_and_credential_change(self):
        a.save_secret('telegram', {'token': 'fake', 'chat_id': '1'})
        with self.assertRaisesRegex(a.Failure, 'verify_telegram_first'):
            a.require_verified(self.db, 'telegram')
        with self.db:
            a.put(self.db, 'telegram_verified', a.fingerprint('telegram'))
        a.require_verified(self.db, 'telegram')
        a.save_secret('telegram', {'token': 'changed', 'chat_id': '1'})
        with self.assertRaises(a.Failure):
            a.require_verified(self.db, 'telegram')

    def test_uid_replay_and_validity_reset_deduplicate(self):
        self.job('100')
        with self.db:
            a.put(self.db, 'uidvalidity', 1)
            a.put(self.db, 'cursor', 0)
            a.put(self.db, 'start', 0)
        class IMAP:
            validity = 1
            def select(self, *args, **kwargs):
                return 'OK', []
            def response(self, name):
                return name, [str(self.validity if name == 'UIDVALIDITY' else 3).encode()]
            def uid(self, operation, *args):
                if operation == 'search':
                    return 'OK', [b'1 2']
                if 'X-GM-MSGID' in args[1]:
                    gm = b'100' if args[0] == b'1' else b'101'
                    return 'OK', [b'1 (X-GM-MSGID ' + gm + b' INTERNALDATE "17-Sep-2026 00:00:00 +0000" RFC822.SIZE 30)']
                return 'OK', [(b'meta', b'Subject: Hi\r\n\r\nbody')]
        client = IMAP()
        a.poll(self.db, client)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.assertEqual(a.meta(self.db, 'cursor'), '2')
        client.validity = 2
        a.poll(self.db, client)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.assertEqual(a.meta(self.db, 'uidvalidity'), '2')

    def test_failed_imap_fetch_does_not_advance_checkpoint(self):
        with self.db:
            a.put(self.db, 'uidvalidity', 1)
            a.put(self.db, 'cursor', 0)
            a.put(self.db, 'start', 0)
        class IMAP:
            def select(self, *args, **kwargs):
                return 'OK', []
            def response(self, name):
                return name, [b'1' if name == 'UIDVALIDITY' else b'2']
            def uid(self, operation, *args):
                return ('OK', [b'1']) if operation == 'search' else ('NO', [])
        with self.assertRaisesRegex(a.Failure, 'imap_metadata_failed'):
            a.poll(self.db, IMAP())
        self.assertEqual(a.meta(self.db, 'cursor'), '0')


if __name__ == '__main__':
    unittest.main()
