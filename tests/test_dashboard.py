import contextlib
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import dashboard as d
import mailagent as a


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(a, DATA=Path(self.tmp.name)/'data', SECRETS=Path(self.tmp.name)/'secrets')
        self.paths.start()
        self.db = a.connect()

    def tearDown(self):
        self.db.close()
        self.paths.stop()
        self.tmp.cleanup()

    def credentials(self):
        for name in ('telegram', 'gmail', 'model'):
            cfg = {'token': 'NEVER_EXPOSE_THIS_TOKEN'} if name == 'telegram' else (
                {'email': 'private@example.invalid', 'app_password': 'NEVER_EXPOSE_THIS_PASSWORD'} if name == 'gmail' else
                {'endpoint': 'https://example.invalid/v1/chat/completions', 'model': 'initial', 'api_key': 'NEVER_EXPOSE_THIS_KEY'})
            a.save_secret(name, cfg)
            fingerprint = a.fingerprint(name)
            with self.db:
                a.put(self.db, name + '_verified', fingerprint)

    def draft(self):
        return {'endpoint': 'https://example.invalid/v1/chat/completions', 'model': 'next-model',
                'prompt': 'List action items only.', 'model_options': ['initial', 'next-model']}

    def test_public_responses_never_expose_credentials(self):
        self.credentials()
        value = json.dumps([d.settings(self.db), d.overview(self.db), d.logs(self.db,{})])
        self.assertNotIn('NEVER_EXPOSE', value)
        self.assertNotIn('api_key"', value)
        self.assertIn('api_key_configured', value)

    def test_save_draft_does_not_change_active_model_or_prompt(self):
        self.credentials()
        d.save_settings(self.db, {'draft':self.draft(), 'revision':0})
        self.assertEqual(a.model_config()['model'], 'initial')
        self.assertEqual(a.model_config()['prompt'], a.DEFAULT_PROMPT)
        self.assertEqual(d.settings(self.db)['draft']['model'], 'next-model')
        with self.assertRaisesRegex(a.Failure, 'settings_conflict_refresh'):
            d.save_settings(self.db, {'draft':self.draft(), 'revision':0})

    def test_prompt_can_be_saved_before_credentials_exist(self):
        draft = {**self.draft(), 'endpoint':'', 'model':''}
        d.save_settings(self.db, {'draft':draft, 'revision':0})
        self.assertEqual(d.settings(self.db)['draft']['prompt'], draft['prompt'])

    def test_activate_runs_loop_then_atomically_updates_runtime(self):
        self.credentials()
        draft = self.draft()
        d.save_settings(self.db, {'draft':draft, 'revision':0})
        def summary(mail, record, cfg):
            self.assertEqual(cfg['model'], 'next-model')
            self.assertEqual(cfg['api_key'], 'NEVER_EXPOSE_THIS_KEY')
            self.assertEqual(a.model_config()['model'], 'initial')
            record('tool', 'read_mail')
            record('tool', 'submit_summary')
            return 'Submit meeting notes by Friday.'
        with patch.object(a, 'agent_summary', side_effect=summary):
            result = d.activate(self.db, {'revision':1})
        self.assertEqual(result['trace'], ['read_mail', 'submit_summary'])
        self.assertEqual(a.model_config()['prompt'], draft['prompt'])
        self.assertEqual(a.model_config()['model'], 'next-model')
        a.require_verified(self.db, 'model')

    def test_failed_activation_keeps_previous_model(self):
        self.credentials()
        d.save_settings(self.db, {'draft':self.draft(), 'revision':0})
        with patch.object(a, 'agent_summary', side_effect=a.Failure('http_401')):
            with self.assertRaises(a.Failure):
                d.activate(self.db, {'revision':1})
        self.assertEqual(a.model_config()['model'], 'initial')
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='model_test_failed'").fetchone()[0], 1)

    def test_rotating_key_invalidates_active_override(self):
        self.test_activate_runs_loop_then_atomically_updates_runtime()
        credential = a.secret('model')
        a.save_secret('model', {**credential, 'api_key':'ROTATED'})
        self.assertEqual(a.model_config()['model'], 'initial')
        with self.assertRaisesRegex(a.Failure, 'verify_model_first'):
            a.require_verified(self.db, 'model')

    def test_settings_reject_embedded_key_and_insecure_endpoint(self):
        for endpoint in ('http://example.invalid', 'https://example.invalid?key=secret', 'https://user:secret@example.invalid'):
            with self.assertRaises(a.Failure):
                d.save_settings(self.db, {'draft':{**self.draft(), 'endpoint':endpoint}, 'revision':0})
        with self.assertRaises(a.Failure):
            d.save_settings(self.db, {'draft':{**self.draft(), 'api_key':'secret'}, 'revision':0})

    def test_records_paginated_filterable_and_without_raw_body(self):
        with self.db:
            for i in range(25):
                self.db.execute('INSERT INTO jobs(id,received,discovered,mail,subject,sender) VALUES(?,?,?,?,?,?)',
                                (str(i), time.time(), time.time(), '{"body":"PRIVATE RAW BODY"}', 'Test '+str(i), 'sender@example.invalid'))
        result = d.records(self.db, {})
        self.assertEqual(len(result['items']), 20)
        self.assertEqual(result['total'], 25)
        self.assertNotIn('PRIVATE RAW BODY', json.dumps(result))
        self.assertEqual(len(d.records(self.db, {'page':['1']})['items']),5)
        self.assertEqual(d.records(self.db, {'q':['Test 24']})['total'],1)
        self.assertEqual(d.records(self.db, {'state':['sent']})['total'],0)

    def test_http_authentication_csrf_logout_and_demo_read_only(self):
        salt = '00'*16
        a.save_secret('dashboard', {'salt':salt, 'hash':d.password_hash('test-password-123', salt)})
        server = d.Server(('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = 'http://127.0.0.1:'+str(server.server_port)
        def request(path, body=None, headers=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base+path, data=data, headers=headers or {'Content-Type':'application/json','X-Requested-With':'MailConsole'})
            try:
                with client.open(req) as response:
                    return response.status, json.load(response), response.headers
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc), exc.headers
        try:
            self.assertEqual(request('/api/mail')[0], 401)
            self.assertEqual(request('/api/login', {'password':'test-password-123'}, {'Content-Type':'application/json'})[0], 403)
            status, _, headers = request('/api/login', {'password':'test-password-123'})
            self.assertEqual(status, 200)
            self.assertIn('HttpOnly', headers['Set-Cookie'])
            self.assertIn('SameSite=Strict', headers['Set-Cookie'])
            self.assertEqual(request('/api/mail')[0], 200)
            self.assertEqual(request('/api/settings', {'draft':self.draft(),'revision':0})[0], 200)
            self.assertEqual(request('/api/session', headers={'Host':'attacker.invalid'})[0], 403)
            self.assertEqual(request('/api/logout', {})[0], 200)
            self.assertEqual(request('/api/settings')[0], 401)
            server.demo = True
            self.assertEqual(request('/api/settings', {'draft':self.draft(),'revision':1})[0], 403)
            self.assertEqual(request('/api/mail')[0], 200)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
