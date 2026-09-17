import base64
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import mailagent as a
import webhook as w


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(a, DATA=Path(self.temp.name)/'data', SECRETS=Path(self.temp.name)/'secrets')
        self.paths.start()
        self.db = a.connect()
        self.cfg = {'audience': 'https://mail.example.com:8005/webhooks/gmail',
                    'service_account': 'push@example.iam.gserviceaccount.com',
                    'subscription': 'projects/example/subscriptions/gmail'}
        a.save_secret('webhook', self.cfg)
        a.save_secret('gmail', {'email':'test@example.com','app_password':'not-used'})

    def tearDown(self):
        self.db.close()
        self.paths.stop()
        self.temp.cleanup()

    def payload(self, message='123', email='test@example.com', history='99'):
        data = base64.urlsafe_b64encode(json.dumps({'emailAddress':email,'historyId':history}).encode()).decode().rstrip('=')
        return {'subscription':self.cfg['subscription'], 'message':{'messageId':message,'data':data}}

    def test_duplicate_notifications_survive_reopen(self):
        w.ingest(self.db, self.payload(), self.cfg)
        self.db.close()
        self.db = a.connect()
        w.ingest(self.db, self.payload(), self.cfg)
        self.assertEqual(self.db.execute('SELECT count(*) FROM webhook_events').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertIsNone(self.db.execute('SELECT processed FROM webhook_events').fetchone()[0])
        self.assertIsNone(a.meta(self.db, 'cursor'))

    def test_wrong_mailbox_subscription_and_malformed_payload_rejected(self):
        for payload in [self.payload(email='other@example.com'), self.payload(history='not-a-number'),
                        {**self.payload(), 'subscription':'projects/other/subscriptions/test'},
                        {'subscription':self.cfg['subscription'],'message':{'messageId':'1','data':'!'}},
                        []]:
            with self.assertRaises(a.Failure):
                w.ingest(self.db,payload,self.cfg)
        self.assertEqual(self.db.execute('SELECT count(*) FROM webhook_events').fetchone()[0],0)

    def test_http_acknowledges_only_authenticated_committed_events(self):
        def verify(header,cfg):
            if header!='Bearer test-only':
                raise a.Failure('unauthorized')
        server=w.Server(('127.0.0.1',0),verifier=verify)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        def post(header):
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/webhooks/gmail',
                data=json.dumps(self.payload()).encode(),headers={'Content-Type':'application/json','Authorization':header})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
        try:
            self.assertEqual(post(''),401)
            self.assertEqual(self.db.execute('SELECT count(*) FROM webhook_events').fetchone()[0],0)
            self.assertEqual(post('Bearer test-only'),204)
            self.assertEqual(post('Bearer test-only'),204)
            self.assertEqual(self.db.execute('SELECT count(*) FROM webhook_events').fetchone()[0],1)
            with patch.object(w,'ingest',side_effect=RuntimeError('database unavailable')):
                self.assertEqual(post('Bearer test-only'),503)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_successful_poll_marks_notification_processed(self):
        w.ingest(self.db,self.payload(),self.cfg)
        checks=[]
        class Stop:
            stopped=False
            def is_set(self): return self.stopped
            def wait(self,seconds):
                checks.append(seconds)
                if len(checks)>=1: self.stopped=True
                return self.stopped
        client=unittest.mock.Mock()
        with patch.object(a,'STOP',Stop()),patch.object(a,'mail_connection',return_value=client),patch.object(a,'initialize_mail'),patch.object(a,'poll') as poll:
            a.collect()
        poll.assert_called_once()
        self.assertIsNotNone(self.db.execute('SELECT processed FROM webhook_events').fetchone()[0])

    def test_notification_during_poll_triggers_another_poll_after_one_second(self):
        checks=[]
        class Stop:
            stopped=False
            def is_set(self): return self.stopped
            def wait(self,seconds): checks.append(seconds); return self.stopped
        stop=Stop()
        def poll(db,client):
            if not checks:
                w.ingest(db,self.payload(),self.cfg)
            else:
                stop.stopped=True
        with patch.object(a,'STOP',stop),patch.object(a,'mail_connection'),patch.object(a,'initialize_mail'),patch.object(a,'poll',side_effect=poll) as run_poll:
            a.collect()
        self.assertEqual(run_poll.call_count,2)
        self.assertEqual(checks,[1,1])
        self.assertIsNotNone(self.db.execute('SELECT processed FROM webhook_events').fetchone()[0])

    def test_failed_poll_keeps_notification_pending(self):
        w.ingest(self.db,self.payload(),self.cfg)
        class Stop:
            stopped=False
            def is_set(self): return self.stopped
            def wait(self,seconds): self.stopped=True; return True
        with patch.object(a,'STOP',Stop()),patch.object(a,'mail_connection',side_effect=OSError()):
            a.collect()
        self.assertIsNone(self.db.execute('SELECT processed FROM webhook_events').fetchone()[0])

    def test_real_rsa_token_verification_checks_signature_audience_and_identity(self):
        try:
            from google.auth import crypt, jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            self.skipTest('Install requirements.txt to run cryptographic verification tests')
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        signer=crypt.RSASigner(key,key_id='local-test-key')
        class Response:
            status=200
            data=json.dumps({'local-test-key':pem}).encode()
        request=lambda *args,**kwargs:Response()
        claims={'iss':'https://accounts.google.com','aud':self.cfg['audience'],'iat':int(time.time())-1,
                'exp':int(time.time())+600,'email':self.cfg['service_account'],'email_verified':True,'sub':'test'}
        def token(payload): return 'Bearer '+jwt.encode(signer,payload).decode()
        w.authenticate(token(claims),self.cfg,request)
        for changes in [{'aud':'https://attacker.invalid'}, {'email':'other@example.iam.gserviceaccount.com'},
                        {'email_verified':False},{'iss':'https://attacker.invalid'}, {'exp':int(time.time())-20}]:
            with self.assertRaisesRegex(a.Failure,'unauthorized'):
                w.authenticate(token({**claims,**changes}),self.cfg,request)
        signed=token(claims)
        with self.assertRaisesRegex(a.Failure,'unauthorized'):
            w.authenticate(signed[:-20]+'A'*20,self.cfg,request)

    def test_watch_renewal_keeps_imap_cursor_and_records_expiration(self):
        a.save_secret('gmail_oauth',{'topic':'projects/example/topics/gmail','client_id':'test-client',
                                    'client_secret':'test-secret','refresh_token':'test-refresh'})
        with self.db:
            a.put(self.db,'cursor',456)
        class Response:
            def __init__(self,value): self.value=value
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return json.dumps(self.value).encode()
        opener=unittest.mock.Mock()
        opener.open.side_effect=[Response({'access_token':'test-token'}),Response({'emailAddress':'test@example.com'})]
        expiry=int((time.time()+86400*7)*1000)
        with patch.object(w.urllib.request,'build_opener',return_value=opener),patch.object(a,'post',return_value={'expiration':str(expiry),'historyId':'1000'}) as post:
            w.renew_watch()
        self.assertEqual(a.meta(self.db,'cursor'),'456')
        self.assertEqual(float(a.meta(self.db,'gmail_watch_expiration')),expiry/1000)
        self.assertLess(float(a.meta(self.db,'gmail_watch_renew_at')),time.time()+86401)
        self.assertEqual(post.call_args.args[1]['labelIds'],['INBOX'])

    def test_watch_rejects_different_oauth_mailbox(self):
        a.save_secret('gmail_oauth',{'topic':'projects/example/topics/gmail','client_id':'test',
                                    'client_secret':'test','refresh_token':'test'})
        class Response:
            def __init__(self,value): self.value=value
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return json.dumps(self.value).encode()
        opener=unittest.mock.Mock()
        opener.open.side_effect=[Response({'access_token':'test'}),Response({'emailAddress':'other@example.com'})]
        with patch.object(w.urllib.request,'build_opener',return_value=opener),patch.object(a,'post') as post:
            with self.assertRaisesRegex(a.Failure,'oauth_mailbox_mismatch'):
                w.renew_watch()
        post.assert_not_called()


if __name__=='__main__':
    unittest.main()
