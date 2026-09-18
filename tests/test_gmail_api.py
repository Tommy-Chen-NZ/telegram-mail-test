import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

import mailagent as a
from gmail_api import GmailAPI


def message(key='65', body='Submit the report by Friday.', **changes):
    return {'id': key, 'internalDate': '1200000', 'labelIds': ['INBOX'],
            'payload': {'mimeType': 'text/plain', 'headers': [{'name': 'From', 'value': 'sender@example.com'},
                {'name': 'Subject', 'value': 'Report'}, {'name': 'Content-Type', 'value': 'text/plain; charset=utf-8'}],
                'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}, **changes}


class GmailAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(a, DATA=Path(self.temp.name)/'data', SECRETS=Path(self.temp.name)/'secrets')
        self.paths.start()
        self.db = a.connect()
        a.save_secret('gmail', {'email': 'test@example.com'})  # No app password.
        a.save_secret('gmail_oauth', {'client_id': 'test-client', 'client_secret': 'PRIVATE-SECRET',
                                    'refresh_token': 'PRIVATE-REFRESH', 'topic': 'projects/test/topics/gmail'})
        with self.db:
            a.put(self.db, 'account', 'test@example.com')
            a.put(self.db, 'start', 1000)
            a.put(self.db, 'cursor', 456)  # Existing IMAP progress.
        self.api = GmailAPI(a)
        self.profile = {'emailAddress': 'test@example.com', 'historyId': '10'}

    def tearDown(self):
        self.db.close()
        self.paths.stop()
        self.temp.cleanup()

    def cursor(self, value):
        with self.db:
            a.put(self.db, 'gmail_history_cursor', value)

    def reopen(self):
        self.db.close()
        self.db = a.connect()

    def test_switch_verifies_oauth_and_preserves_imap_and_api_progress_without_password(self):
        a.save_secret('telegram', {'token': 'fake', 'chat_id': '1'})
        with self.db:
            a.put(self.db, 'telegram_verified', a.fingerprint('telegram'))
        self.cursor(9)
        with patch.object(a, 'exclusive', contextlib.nullcontext), patch.object(a, 'gmail_api_client', return_value=self.api), \
             patch.object(self.api, 'get', side_effect=[self.profile, {'messages': [{'id': '65'}]}, message()]), \
             patch.object(a, 'mail_connection') as imap:
            a.enable_gmail_api()
        self.assertEqual(a.meta(self.db, 'gmail_source'), 'gmail_api')
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '9')
        self.assertEqual(a.meta(self.db, 'start'), '1000')
        self.assertEqual(a.meta(self.db, 'cursor'), '456')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        a.require_verified(self.db, 'gmail')
        imap.assert_not_called()
        a.save_secret('gmail_oauth', {**a.secret('gmail_oauth'), 'refresh_token': 'changed'})
        with self.assertRaisesRegex(a.Failure, 'verify_gmail_first'):
            a.require_verified(self.db, 'gmail')

    def test_migration_resync_deduplicates_decimal_imap_ids_and_replays_arrivals_during_scan(self):
        with self.db:
            self.db.execute("INSERT INTO jobs(id,received,discovered,mail,state) VALUES('100',1100,1100,'{}','sent')")
        responses = [self.profile, {'messages': [{'id': '64'}, {'id': '65'}]}, message(),
                     {'historyId': '20', 'history': [{'messagesAdded': [{'message': {'id': '65'}}, {'message': {'id': '66'}}]}]},
                     message('66')]
        with patch.object(self.api, 'get', side_effect=responses) as get:
            self.assertFalse(self.api.poll(self.db))
            self.reopen()
            self.assertTrue(self.api.poll(self.db))
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,state FROM jobs ORDER BY id')],
                         [('100', 'sent'), ('101', 'pending'), ('102', 'pending')])
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '20')
        self.assertEqual(get.call_args_list[1].kwargs['q'], 'after:999')
        self.assertEqual(get.call_args_list[3].kwargs['startHistoryId'], '10')

    def test_history_pagination_resumes_after_restart_and_keeps_work_durable(self):
        self.cursor(10)
        page1 = {'historyId': '30', 'nextPageToken': 'page-2', 'history': [
            {'messagesAdded': [{'message': {'id': '65'}}],
             'labelsAdded': [{'message': {'id': '66'}, 'labelIds': ['INBOX']},
                             {'message': {'id': '67'}, 'labelIds': ['STARRED']}]}]}
        page2 = {'historyId': '30', 'history': [{'messagesAdded': [{'message': {'id': '65'}}, {'message': {'id': '68'}}]}]}
        with patch.object(self.api, 'get', side_effect=[page1, page2]) as get:
            self.assertFalse(self.api.sync_page(self.db))
            self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '10')
            self.reopen()
            self.assertTrue(self.api.sync_page(self.db))
            self.assertEqual(get.call_args.kwargs['pageToken'], 'page-2')
            self.assertEqual(get.call_args.kwargs['startHistoryId'], '10')
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '30')
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM gmail_fetch ORDER BY id')], ['65', '66', '68'])

    def test_failed_body_fetch_survives_restart_and_backup_even_after_history_advances(self):
        self.cursor(10)
        history = {'historyId': '20', 'history': [{'messagesAdded': [{'message': {'id': '65'}}]}]}
        with patch.object(self.api, 'get', side_effect=[history, a.Failure('gmail_http_503')]):
            with self.assertRaisesRegex(a.Failure, 'gmail_http_503'):
                self.api.poll(self.db)
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '20')
        snapshot = Path(self.temp.name)/'backup.sqlite3'
        a.backup(str(snapshot))
        with contextlib.closing(sqlite3.connect(snapshot)) as restored:
            self.assertEqual(restored.execute('SELECT id FROM gmail_fetch').fetchone()[0], '65')
            self.assertEqual(restored.execute("SELECT value FROM meta WHERE key='gmail_history_cursor'").fetchone()[0], '20')
        self.reopen()
        with patch.object(self.api, 'get', side_effect=[{'historyId': '20'}, message()]):
            self.assertTrue(self.api.poll(self.db))
        self.assertEqual(self.db.execute('SELECT id,state FROM jobs').fetchone()['id'], '101')
        self.assertEqual(self.db.execute('SELECT count(*) FROM gmail_fetch').fetchone()[0], 0)

    def test_real_process_crash_preserves_queued_fetch_and_progress(self):
        self.cursor(10)
        code = """import os, mailagent as a
from gmail_api import GmailAPI
api = GmailAPI(a)
api.get = lambda *args, **kwargs: {'historyId':'20','history':[{'messagesAdded':[{'message':{'id':'65'}}]}]}
api.sync_page(a.connect())
os._exit(9)
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
            env={**os.environ, 'AGENT_DATA': str(a.DATA), 'AGENT_SECRETS': str(a.SECRETS)})
        self.assertEqual(result.returncode, 9, result.stderr)
        self.reopen()
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '20')
        self.assertEqual(self.db.execute('SELECT id FROM gmail_fetch').fetchone()[0], '65')

    def test_expired_history_resyncs_all_pages_without_resetting_start(self):
        self.cursor(1)
        with patch.object(self.api, 'get', side_effect=[a.Failure('gmail_http_404'), self.profile,
                {'messages': [{'id': '65'}], 'nextPageToken': 'next'}, {'messages': [{'id': '66'}]}]) as get:
            self.assertFalse(self.api.sync_page(self.db))
            self.assertFalse(self.api.sync_page(self.db))
            self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '1')
            self.reopen()
            self.assertFalse(self.api.sync_page(self.db))
            self.assertEqual(get.call_args.kwargs['pageToken'], 'next')
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '10')
        self.assertEqual(a.meta(self.db, 'start'), '1000')
        self.assertEqual(self.db.execute('SELECT count(*) FROM gmail_fetch').fetchone()[0], 2)

    def test_history_failure_never_discards_progress_and_invalid_page_restarts_from_cursor(self):
        self.cursor(10)
        with self.db:
            a.put(self.db, 'gmail_history_page', 'expired-page')
        with patch.object(self.api, 'get', side_effect=a.Failure('gmail_http_503')):
            with self.assertRaises(a.Failure):
                self.api.sync_page(self.db)
        self.assertEqual(a.meta(self.db, 'gmail_history_page'), 'expired-page')
        with patch.object(self.api, 'get', side_effect=a.Failure('gmail_http_400')):
            self.assertFalse(self.api.sync_page(self.db))
        self.assertEqual(a.meta(self.db, 'gmail_history_page'), '')
        self.assertEqual(a.meta(self.db, 'gmail_history_cursor'), '10')

    def test_old_archived_and_deleted_mail_is_skipped_without_stalling_queue(self):
        with self.db:
            self.api.enqueue(self.db, [{'id': key} for key in ['65', '66', '67', '68']])
        with patch.object(self.api, 'get', side_effect=[message(internalDate='999000'),
                message('66', labelIds=['SENT']), a.Failure('gmail_http_404'), message('68')]):
            self.api.fetch_pending(self.db)
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM jobs')], ['104'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM gmail_fetch').fetchone()[0], 0)

    def test_parser_skips_attachment_subtrees_and_scripts_and_marks_missing_body(self):
        full = message()
        full['payload']['mimeType'] = 'multipart/mixed'
        full['payload']['body'] = {}
        full['payload']['parts'] = [
            {'mimeType': 'multipart/mixed', 'filename': 'attached.eml', 'parts': [message(body='PRIVATE-ATTACHMENT')['payload']]},
            {'mimeType': 'text/html', 'body': {'data': base64.urlsafe_b64encode(b'<p>Hello</p><script>PRIVATE-SCRIPT</script>').decode()}},
            {'mimeType': 'text/plain', 'body': {'attachmentId': 'external-body'}},
        ]
        mail = self.api.parse_mail(full)
        self.assertIn('Hello', mail['body'])
        self.assertNotIn('PRIVATE', mail['body'])
        self.assertTrue(mail['truncated'])
        self.assertFalse(mail['attachments_read'])

    def test_large_body_uses_explicitly_truncated_snippet_and_never_fetches_attachments(self):
        metadata = message(snippet='Report &amp; deadline')
        metadata['payload'].pop('body')
        with patch.object(self.api, 'get', side_effect=[a.Failure('gmail_response_too_large'), metadata]) as get:
            _, mail = self.api.read_mail('65')
        self.assertEqual(mail['body'], 'Report & deadline')
        self.assertTrue(mail['truncated'])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs['format'], 'metadata')

    def test_access_token_is_cached_and_refreshed_on_401_without_logging_secrets(self):
        values = [{'access_token': 'PRIVATE-ACCESS', 'expires_in': 3600}, self.profile,
                  {'messages': []}, a.Failure('gmail_http_401'),
                  {'access_token': 'PRIVATE-NEW', 'expires_in': 3600}, self.profile, {'messages': []}]
        with patch.object(self.api, 'json_request', side_effect=values) as request, contextlib.redirect_stdout(io.StringIO()) as output:
            self.api.get('messages')
            self.api.get('messages')
        self.assertEqual(request.call_count, 7)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(request.call_args.args[0].headers['Authorization'], 'Bearer PRIVATE-NEW')

    def test_rotated_credentials_cannot_read_a_different_mailbox(self):
        with patch.object(self.api, 'json_request', side_effect=[{'access_token': 'PRIVATE'},
                {'emailAddress': 'other@example.com', 'historyId': '1'}]):
            with self.assertRaisesRegex(a.Failure, 'oauth_mailbox_mismatch'):
                self.api.get('messages')
        self.assertIsNone(self.api.token)

    def test_response_size_and_http_errors_are_bounded_and_sanitized(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'x' * 11
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(a.Failure, 'gmail_response_too_large'):
                self.api.json_request(Mock(), limit=10)
        opener.open.side_effect = urllib.error.HTTPError('https://example.invalid/PRIVATE', 429,
                'PRIVATE ERROR', {'Retry-After': '120'}, io.BytesIO(b'PRIVATE BODY'))
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(a.Failure) as error:
                self.api.json_request(Mock())
        self.assertEqual(str(error.exception), 'gmail_http_429')
        self.assertEqual(error.exception.retry_after, 120)

    def test_collector_uses_only_api_and_keeps_notifications_arriving_during_sync(self):
        with self.db:
            a.put(self.db, 'gmail_source', 'gmail_api')
            a.put(self.db, 'gmail_verified', a.gmail_api_fingerprint())
            self.db.execute("INSERT INTO webhook_events(message_id,history_id,received) VALUES('before','10',1)")
        class Stop:
            waits = 0
            def is_set(self): return self.waits >= 2
            def wait(self, seconds):
                self.waits += 1
                return self.is_set()
        calls = []
        def poll(db):
            calls.append(True)
            if len(calls) == 1:
                return False
            self.assertIsNone(db.execute("SELECT processed FROM webhook_events WHERE message_id='before'").fetchone()[0])
            with db:
                db.execute("INSERT INTO webhook_events(message_id,history_id,received) VALUES('during','20',2)")
            return True
        with patch.object(a, 'STOP', Stop()), patch.object(a, 'gmail_api_client', return_value=self.api), \
             patch.object(self.api, 'poll', side_effect=poll), patch.object(a, 'mail_connection') as imap:
            a.collect()
        imap.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(self.db.execute("SELECT processed FROM webhook_events WHERE message_id='before'").fetchone()[0])
        self.assertIsNone(self.db.execute("SELECT processed FROM webhook_events WHERE message_id='during'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
