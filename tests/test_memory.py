import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import mailagent as a


def response(name, args):
    return {'choices': [{'message': {'tool_calls': [{'id': name, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(a, 'DATA', Path(self.temp.name) / 'data')
        self.paths.start()
        self.db = a.connect()
        self.mail = {'from': 'alex@example.invalid', 'subject': 'Project Atlas',
                     'body': 'The deadline is September 25.', 'truncated': False}

    def tearDown(self):
        self.db.close()
        self.paths.stop()
        self.temp.cleanup()

    def job(self, key):
        with self.db:
            self.db.execute('INSERT INTO jobs(id,received,discovered,mail) VALUES(?,?,?,?)',
                            (key, time.time(), time.time(), json.dumps(self.mail)))

    def test_two_emails_read_replace_and_restore_memory(self):
        old = 'Alex: Atlas deadline September 25.'
        new = 'Alex: Atlas deadline moved to September 28.'
        responses = [response('read_mail', {'offset': 0}),
                     response('submit_summary', {'summary': 'Atlas is due September 25.', 'memory': old}),
                     response('read_mail', {'offset': 0}),
                     response('submit_summary', {'summary': 'Atlas is now due September 28.', 'memory': new})]
        payloads = []
        def model(url, payload, key, timeout):
            payloads.append(json.loads(json.dumps(payload)))
            return responses.pop(0)
        with patch.object(a, 'model_config', return_value={'endpoint': 'https://example.invalid', 'model': 'test', 'api_key': 'fake'}), \
             patch.object(a, 'post', side_effect=model):
            self.job('1')
            a.work_one(self.db, send=lambda _: {'message_id': 1})
            self.mail['body'] = 'The deadline has moved to September 28.'
            self.job('2')
            a.work_one(self.db, send=lambda _: {'message_id': 2})
        context = json.loads(payloads[2]['messages'][1]['content'])
        self.assertEqual(context['prior_memory'], old)
        self.assertEqual(a.meta(self.db, 'cross_email_memory'), new)
        self.assertEqual(a.meta(self.db, 'memory_source_job'), '2')
        self.assertNotIn(old, payloads[2]['messages'][0]['content'])
        self.assertIn('untrusted background', payloads[2]['messages'][0]['content'])
        self.assertNotIn('memory', a.TOOLS[1]['function']['parameters']['properties'])
        snapshot = Path(self.temp.name) / 'backup.sqlite3'
        a.backup(str(snapshot))
        with contextlib.closing(sqlite3.connect(snapshot)) as restored:
            self.assertEqual(a.meta(restored, 'cross_email_memory'), new)
        with contextlib.closing(a.connect()) as reopened:
            self.assertEqual(a.meta(reopened, 'cross_email_memory'), new)

    def test_send_retry_cannot_overwrite_newer_memory(self):
        self.job('1')
        with patch.object(a, 'agent_summary', return_value=('First summary', 'First memory')) as model:
            def fail(_):
                raise a.Failure('http_429', 90)
            a.work_one(self.db, send=fail)
            self.assertEqual(a.meta(self.db, 'cross_email_memory'), 'First memory')
            self.job('2')
            model.return_value = ('Second summary', 'Newer memory')
            a.work_one(self.db, send=lambda _: {'message_id': 2})
            with self.db:
                self.db.execute("UPDATE jobs SET next_try=0 WHERE id='1'")
            a.recover(self.db)
            a.work_one(self.db, send=lambda _: {'message_id': 1})
            self.assertEqual(model.call_count, 2)
        self.assertEqual(a.meta(self.db, 'cross_email_memory'), 'Newer memory')
        self.assertEqual(a.meta(self.db, 'memory_source_job'), '2')

    def test_invalid_memory_does_not_block_summary_or_replace_previous(self):
        for bad in (None, 123, 'x' * 1501, '\u00e9' * 751, '\ud800'):
            with self.subTest(value_type=type(bad).__name__):
                trace = []
                with patch.object(a, 'post', side_effect=[response('read_mail', {'offset': 0}),
                     response('submit_summary', {'summary': 'Valid summary', 'memory': bad})]):
                    result = a.agent_summary(self.mail, lambda *args: trace.append(args),
                        cfg={'endpoint': 'https://example.invalid', 'model': 'test', 'api_key': 'fake'},
                        memory='Previous memory', include_memory=True)
                self.assertEqual(result, ('Valid summary', None))
                self.assertIn(('memory_skipped', 'invalid_or_oversized'), trace)
        with self.db:
            a.put(self.db, 'cross_email_memory', 'Previous memory')
        self.job('1')
        with patch.object(a, 'agent_summary', return_value=('Valid summary', None)):
            a.work_one(self.db, send=lambda _: {'message_id': 1})
        self.assertEqual(a.meta(self.db, 'cross_email_memory'), 'Previous memory')
        self.assertEqual(self.db.execute('SELECT state FROM jobs').fetchone()[0], 'sent')

    def test_memory_and_summary_roll_back_together(self):
        self.job('1')
        with self.db:
            a.put(self.db, 'cross_email_memory', 'Old memory')
            self.db.execute("CREATE TRIGGER reject_memory BEFORE INSERT ON meta WHEN NEW.key='memory_updated_at' BEGIN SELECT RAISE(ABORT, 'test'); END")
        with patch.object(a, 'agent_summary', return_value=('New summary', 'New memory')), \
             patch.object(a, 'telegram') as send:
            a.work_one(self.db, send=send)
            send.assert_not_called()
        self.assertEqual(a.meta(self.db, 'cross_email_memory'), 'Old memory')
        self.assertIsNone(self.db.execute('SELECT summary FROM jobs').fetchone()[0])

    def test_clear_and_status_do_not_expose_memory(self):
        with self.db:
            a.put(self.db, 'cross_email_memory', 'Private project context')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            a.status()
        self.assertNotIn('Private project context', output.getvalue())
        with patch.object(a, 'exclusive', contextlib.nullcontext):
            a.memory_command(clear=True)
        self.assertEqual(a.meta(self.db, 'cross_email_memory', ''), '')


if __name__ == '__main__':
    unittest.main()
