"""Authenticated Gmail Pub/Sub notifications; durable IMAP wakeups."""
import argparse
import base64
import contextlib
import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import mailagent as a

STOP = threading.Event()


def setup():
    audience = input('Public HTTPS webhook URL (ending /webhooks/gmail): ').strip()
    parsed = urlsplit(audience)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.path != '/webhooks/gmail'
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise a.Failure('invalid_webhook_url')
    service_account = input('Pub/Sub push authentication service account email: ').strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com', service_account):
        raise a.Failure('invalid_service_account')
    subscription = input('Subscription (projects/PROJECT/subscriptions/NAME): ').strip()
    if not re.fullmatch(r'projects/[^/\s]+/subscriptions/[^/\s]+', subscription):
        raise a.Failure('invalid_subscription')
    a.save_secret('webhook', {'audience': audience, 'service_account': service_account, 'subscription': subscription})
    print('WEBHOOK_CONFIG_OK')


def setup_watch():
    # OAuth tokens are entered in the terminal, never passed as CLI arguments.
    topic = input('Topic (projects/PROJECT/topics/NAME): ').strip()
    if not re.fullmatch(r'projects/[^/\s]+/topics/[^/\s]+', topic):
        raise a.Failure('invalid_topic')
    cfg = {'topic': topic, 'client_id': a.hidden('OAuth client ID (hidden): '),
           'client_secret': a.hidden('OAuth client secret (hidden): '),
           'refresh_token': a.hidden('OAuth refresh token with gmail.readonly scope (hidden): ')}
    if not all(cfg.values()):
        raise a.Failure('empty_configuration')
    a.save_secret('gmail_oauth', cfg)
    print('GMAIL_OAUTH_CONFIG_OK')


def renew_watch():
    cfg = a.secret('gmail_oauth')
    receiver = a.secret('webhook')
    if cfg['topic'].split('/')[1] != receiver['subscription'].split('/')[1]:
        raise a.Failure('topic_subscription_project_mismatch')
    # No access token is persisted, printed, or placed in the URL.
    token_request = urllib.request.Request('https://oauth2.googleapis.com/token', data=urllib.parse.urlencode({
        'client_id': cfg['client_id'], 'client_secret': cfg['client_secret'],
        'refresh_token': cfg['refresh_token'], 'grant_type': 'refresh_token'}).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.build_opener(a.NoRedirect()).open(token_request, timeout=15) as response:
        token = json.loads(response.read(16384))['access_token']
    request = urllib.request.Request('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                                     headers={'Authorization': 'Bearer ' + token})
    with urllib.request.build_opener(a.NoRedirect()).open(request, timeout=15) as response:
        profile = json.loads(response.read(16384))
    if profile.get('emailAddress', '').lower() != a.secret('gmail')['email'].lower():
        raise a.Failure('oauth_mailbox_mismatch')
    result = a.post('https://gmail.googleapis.com/gmail/v1/users/me/watch', {
        'topicName': cfg['topic'], 'labelIds': ['INBOX'], 'labelFilterBehavior': 'include'}, token)
    expiration = int(result['expiration']) / 1000
    if expiration <= time.time():
        raise a.Failure('invalid_watch_expiration')
    with contextlib.closing(a.connect()) as db:
        with db:
            a.put(db, 'gmail_watch_expiration', expiration)
            a.put(db, 'gmail_watch_renew_at', min(time.time() + 86400, expiration - 3600))
            a.put(db, 'gmail_watch_history', result['historyId'])
            a.event(db, None, 'gmail_watch_renewed')
    print('GMAIL_WATCH_OK', flush=True)


def maintain_watch():
    while not STOP.is_set():
        # An externally managed watch is also supported; OAuth setup is optional.
        if (a.SECRETS / 'gmail_oauth.json').is_file():
            try:
                with contextlib.closing(a.connect()) as db:
                    due = float(a.meta(db, 'gmail_watch_renew_at', 0))
                if time.time() >= due:
                    renew_watch()
            except Exception:
                with contextlib.suppress(Exception):
                    with contextlib.closing(a.connect()) as db:
                        with db:
                            a.event(db, None, 'gmail_watch_retry', 'watch_renewal_failed')
                a.log('gmail_watch_retry', code='watch_renewal_failed')
        STOP.wait(300)


class CertificateRequest:
    """Cache Google's public certificates, never JWTs or request payloads."""
    def __init__(self):
        from google.auth.transport.requests import Request
        self.request = Request()
        self.cached = None
        self.expires = 0

    def __call__(self, url, method='GET', **kwargs):
        if self.cached and time.monotonic() < self.expires and url == self.cached[0]:
            return self.cached[1]
        response = self.request(url=url, method=method, timeout=10, **kwargs)
        if response.status == 200:
            cache = response.headers.get('cache-control', '')
            match = re.search(r'max-age=(\d+)', cache)
            ttl = min(3600, int(match[1])) if match else 300
            self.cached, self.expires = (url, response), time.monotonic() + ttl
        return response


def authenticate(authorization, cfg, request):
    if not authorization.startswith('Bearer ') or len(authorization) > 12000:
        raise a.Failure('unauthorized')
    from google.oauth2 import id_token
    from google.auth.exceptions import TransportError
    try:
        claims = id_token.verify_oauth2_token(authorization[7:], request, audience=cfg['audience'])
    except TransportError:
        raise a.Failure('verification_unavailable') from None
    except Exception:
        raise a.Failure('unauthorized') from None
    if (claims.get('email') != cfg['service_account'] or claims.get('email_verified') is not True
            or claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com')):
        raise a.Failure('unauthorized')


def ingest(db, envelope, cfg):
    if not isinstance(envelope, dict) or envelope.get('subscription') != cfg['subscription']:
        raise a.Failure('invalid_subscription')
    try:
        message = envelope['message']
        message_id, encoded = message['messageId'], message['data']
        if (not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', message_id)
                or not isinstance(encoded, str) or len(encoded) > 8192):
            raise ValueError()
        payload = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
        email, history = payload['emailAddress'], payload['historyId']
        if not isinstance(history, str) or not re.fullmatch(r'\d{1,40}', history):
            raise ValueError()
        if not isinstance(email, str):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise a.Failure('invalid_pubsub_message') from None
    if email.strip().lower() != a.secret('gmail')['email'].strip().lower():
        raise a.Failure('unexpected_mailbox')
    with db:
        result = db.execute('INSERT OR IGNORE INTO webhook_events(message_id,history_id,received) VALUES(?,?,?)',
                            (message_id, history, time.time()))
        if result.rowcount:
            a.event(db, None, 'gmail_webhook_received')
    # The 204 is returned only after durable commit. historyId is metadata,
    # never used as a Gmail message ID or as the IMAP processing checkpoint.


class Server(HTTPServer):
    def __init__(self, address, verifier=None):
        super().__init__(address, Handler)
        self.certificates = None
        self.verifier = verifier or self.verify

    def verify(self, header, cfg):
        if self.certificates is None:
            self.certificates = CertificateRequest()
        authenticate(header, cfg, self.certificates)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address


class Handler(BaseHTTPRequestHandler):
    server_version = 'MailWebhook'

    def log_message(self, *_):
        pass

    def respond(self, status, code=None):
        body = json.dumps({'error': code}).encode() if code else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self.respond(200 if self.path == '/healthz' else 404)

    def do_POST(self):
        if self.path != '/webhooks/gmail':
            return self.respond(404, 'not_found')
        try:
            cfg = a.secret('webhook')
            if (self.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                    or self.headers.get('Transfer-Encoding')):
                return self.respond(400, 'invalid_content_type')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self.respond(413, 'invalid_payload_size')
            raw = self.rfile.read(length)
            self.server.verifier(self.headers.get('Authorization', ''), cfg)
            envelope = json.loads(raw)
            with contextlib.closing(a.connect()) as db:
                ingest(db, envelope, cfg)
            self.respond(204)
        except a.Failure as exc:
            code = exc.code
            status = 401 if code == 'unauthorized' else (503 if code.startswith('missing_') or code == 'verification_unavailable' else 400)
            self.respond(status, code)
        except (ValueError, TypeError, TimeoutError):
            self.respond(400, 'invalid_payload')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(503, 'temporarily_unavailable')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('setup', 'setup-watch', 'watch', 'serve'))
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'setup':
        return setup()
    if args.command == 'setup-watch':
        return setup_watch()
    if args.command == 'watch':
        return renew_watch()
    a.secret('webhook')
    with Server((args.bind, args.port)) as server:
        threading.Thread(target=maintain_watch, daemon=True).start()
        print(f'WEBHOOK_READY port={args.port} path=/webhooks/gmail', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        a.log('webhook_failed', code=exc.code if isinstance(exc, a.Failure) else 'operation_failed')
        raise SystemExit(1)
