"""Private, SSH-tunnel-only React dashboard; no third-party runtime dependencies."""
import argparse
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import mailagent as a

STATIC = Path(__file__).parent / 'frontend' / 'dist'
STATES = ('pending', 'processing', 'sending', 'sent', 'retry')


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()


def setup():
    password = a.hidden('Dashboard password (at least 12 characters, hidden): ')
    if len(password) < 12 or password != a.hidden('Confirm password: '):
        raise a.Failure('password_short_or_mismatch')
    salt = secrets.token_hex(16)
    a.save_secret('dashboard', {'salt': salt, 'hash': password_hash(password, salt)})
    print('DASHBOARD_PASSWORD_OK')


def optional_secret(name):
    try:
        return a.secret(name)
    except a.Failure:
        return {}


def settings(db):
    base = optional_secret('model')
    active = a.model_config() if base else {'endpoint': '', 'model': '', 'prompt': a.DEFAULT_PROMPT}
    draft = json.loads(a.meta(db, 'dashboard_draft', 'null')) or {
        'endpoint': active['endpoint'], 'model': active['model'], 'prompt': active['prompt'],
        'model_options': [active['model']] if active['model'] else []}
    return {'draft': draft, 'active': {k: active[k] for k in ('endpoint', 'model', 'prompt')},
            'api_key_configured': bool(base.get('api_key')),
            'revision': int(a.meta(db, 'dashboard_revision', 0)),
            'activated_at': a.meta(db, 'model_activated_at')}


def validate_draft(value, complete=False):
    if not isinstance(value, dict) or set(value) != {'endpoint', 'model', 'prompt', 'model_options'}:
        raise a.Failure('invalid_settings')
    for key, limit in [('endpoint', 500), ('model', 120), ('prompt', 5000)]:
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise a.Failure('invalid_' + key)
    endpoint = urlsplit(value['endpoint'])
    if ((complete or value['endpoint']) and (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment)):
        raise a.Failure('invalid_model_endpoint')
    if (complete and not value['model'].strip()) or not value['prompt'].strip():
        raise a.Failure('model_and_prompt_required')
    options = value['model_options']
    if (not isinstance(options, list) or len(options) > 30
            or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in options)):
        raise a.Failure('invalid_model_options')
    return {**value, 'model_options': list(dict.fromkeys(options))}


def save_settings(db, body):
    draft = validate_draft(body.get('draft'))
    with db:
        db.execute('BEGIN IMMEDIATE')
        revision = int(a.meta(db, 'dashboard_revision', 0))
        if body.get('revision') != revision:
            raise a.Failure('settings_conflict_refresh')
        a.put(db, 'dashboard_draft', json.dumps(draft, ensure_ascii=False))
        a.put(db, 'dashboard_revision', revision + 1)
        a.event(db, None, 'settings_saved')
    return settings(db)


def activate(db, body):
    a.require_verified(db, 'telegram')
    a.require_verified(db, 'gmail')
    current = settings(db)
    if body.get('revision') != current['revision']:
        raise a.Failure('settings_conflict_refresh')
    draft = validate_draft(current['draft'], complete=True)
    credential = a.secret('model')
    cfg = {**credential, **{k: draft[k] for k in ('endpoint', 'model', 'prompt')}}
    trace = []
    sample = {'from': 'test@example.invalid', 'subject': 'Summary test', 'body': 'Please submit the meeting notes by Friday at 3 PM.',
              'truncated': False, 'attachments_read': False}
    try:
        summary = a.agent_summary(sample, lambda kind, code: trace.append(code), cfg=cfg)
    except Exception:
        with db:
            a.event(db, None, 'model_test_failed', 'model_test_failed')
        raise
    with db:
        db.execute('BEGIN IMMEDIATE')
        if (int(a.meta(db, 'dashboard_revision', 0)) != current['revision']
                or a.credential_digest(a.secret('model')) != a.credential_digest(credential)):
            raise a.Failure('configuration_changed_retry')
        a.put(db, 'model_active', json.dumps({**draft, 'credential_digest': a.credential_digest(credential)}, ensure_ascii=False))
        a.put(db, 'model_verified', a.credential_digest(cfg))
        a.put(db, 'model_activated_at', time.time())
        a.event(db, None, 'model_activated')
    return {'settings': settings(db), 'trace': trace, 'summary': summary}


def overview(db):
    now = time.time()
    day = now - 86400
    counts = dict(db.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())
    metrics = db.execute('''SELECT count(*) AS sent, round(avg(sent-received),1) AS average,
        sum(CASE WHEN sent-received<=60 THEN 1 ELSE 0 END) AS on_time
        FROM jobs WHERE state='sent' AND sent>=?''', (day,)).fetchone()
    connections = {}
    for name in ('telegram', 'gmail', 'model'):
        cfg = optional_secret(name)
        verified = False
        if cfg:
            try:
                a.require_verified(db, name)
                verified = True
            except a.Failure:
                pass
        connections[name] = {'configured': bool(cfg), 'verified': verified}
    heartbeat = {}
    for key in ('worker_ok', 'collector_ok'):
        value = a.meta(db, key)
        heartbeat[key] = round(now - float(value), 1) if value else None
    return {'counts': counts, 'sent_24h': metrics['sent'], 'average_seconds': metrics['average'],
            'on_time_percent': round(100 * metrics['on_time'] / metrics['sent'], 1) if metrics['sent'] else None,
            'connections': connections, 'heartbeat': heartbeat,
            'model': settings(db)['active']['model'], 'server_time': now}


def integer(query, name, default, maximum):
    try:
        return max(0, min(maximum, int(query.get(name, [str(default)])[0])))
    except ValueError:
        raise a.Failure('invalid_pagination') from None


def records(db, query):
    page = integer(query, 'page', 0, 100000)
    limit = 20
    state = query.get('state', [''])[0]
    term = query.get('q', [''])[0][:100]
    where, args = ['1=1'], []
    if state:
        if state not in STATES:
            raise a.Failure('invalid_state')
        where.append('state=?')
        args.append(state)
    if term:
        where.append('(instr(lower(subject),lower(?))>0 OR instr(lower(sender),lower(?))>0 OR instr(id,?)>0)')
        args.extend([term] * 3)
    clause = ' AND '.join(where)
    total = db.execute('SELECT count(*) FROM jobs WHERE ' + clause, args).fetchone()[0]
    rows = db.execute('''SELECT id,sender,subject,received,discovered,state,attempts,summary,
        telegram_id,sent,next_try,last_error,round(sent-received,2) AS latency
        FROM jobs WHERE ''' + clause + ' ORDER BY discovered DESC LIMIT ? OFFSET ?', [*args, limit, page * limit])
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit}


def logs(db, query):
    page = integer(query, 'page', 0, 100000)
    errors = query.get('errors', ['0'])[0] == '1'
    clause = " WHERE kind IN ('retry','collector_retry','model_test_failed','command_failed','gmail_watch_retry')" if errors else ''
    total = db.execute('SELECT count(*) FROM events' + clause).fetchone()[0]
    rows = db.execute('SELECT * FROM events' + clause + ' ORDER BY seq DESC LIMIT 30 OFFSET ?', (page * 30,))
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': 30}


class Server(HTTPServer):
    def __init__(self, address, demo=False):
        super().__init__(address, Handler)
        self.demo = demo
        self.sessions = {}
        self.login_failures = []

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address


class Handler(BaseHTTPRequestHandler):
    server_version = 'MailConsole'

    def log_message(self, *_):
        pass  # No request bodies, query strings, cookies or access tokens in logs.

    def send(self, status, value, content_type='application/json; charset=utf-8', cookie=None):
        body = json.dumps(value, ensure_ascii=False).encode() if content_type.startswith('application/json') else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        if self.server.demo:
            return True
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['mail_session'].value
            expiry, credential = self.server.sessions.get(token, (0, ''))
            cfg = optional_secret('dashboard')
            return expiry > time.time() and hmac.compare_digest(credential, cfg.get('hash', 'missing'))
        except Exception:
            return False

    def host_ok(self):
        host = self.headers.get('Host', '')
        try:
            return urlsplit('http://' + host).hostname in ('localhost', '127.0.0.1', '::1')
        except ValueError:
            return False

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, write):
        try:
            if not self.host_ok():
                return self.send(403, {'error': 'localhost_only_use_ssh_tunnel'})
            path = urlsplit(self.path)
            if write:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send(400, {'error': 'invalid_request_body'})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    return self.send(400, {'error': 'invalid_request_body'})
                origin = self.headers.get('Origin')
                if (self.headers.get('X-Requested-With') != 'MailConsole'
                        or (origin and origin != 'http://' + self.headers.get('Host'))):
                    return self.send(403, {'error': 'invalid_request_origin'})
            if path.path == '/api/session' and not write:
                return self.send(200, {'authenticated': self.authenticated(), 'demo': self.server.demo,
                                       'configured': bool(optional_secret('dashboard'))})
            if path.path == '/api/login' and write:
                return self.login(body)
            if path.path.startswith('/api/'):
                if not self.authenticated():
                    return self.send(401, {'error': 'login_required'})
                if path.path == '/api/logout' and write:
                    cookie = SimpleCookie(self.headers.get('Cookie', ''))
                    if 'mail_session' in cookie:
                        self.server.sessions.pop(cookie['mail_session'].value, None)
                    return self.send(200, {'ok': True}, cookie='mail_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if self.server.demo:
                    if write:
                        return self.send(403, {'error': 'demo_read_only'})
                    from demo_dashboard import response
                    return self.send(200, response(path.path, parse_qs(path.query)))
                with contextlib.closing(a.connect()) as db:
                    if write:
                        if path.path == '/api/settings':
                            return self.send(200, save_settings(db, body))
                        if path.path == '/api/activate':
                            return self.send(200, activate(db, body))
                    else:
                        actions = {'/api/overview': lambda: overview(db), '/api/settings': lambda: settings(db),
                                   '/api/mail': lambda: records(db, parse_qs(path.query)),
                                   '/api/logs': lambda: logs(db, parse_qs(path.query))}
                        if path.path in actions:
                            return self.send(200, actions[path.path]())
                return self.send(404, {'error': 'not_found'})
            if write:
                return self.send(404, {'error': 'not_found'})
            target = (STATIC / ('index.html' if path.path == '/' else path.path.lstrip('/'))).resolve()
            if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                return self.send(404, {'error': 'not_found'})
            mime = 'text/javascript' if target.suffix == '.js' else (mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            return self.send(200, target.read_bytes(), mime)
        except a.Failure as exc:
            return self.send(400, {'error': exc.code})
        except (ValueError, TypeError):
            return self.send(400, {'error': 'invalid_request'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            return self.send(500, {'error': 'dashboard_operation_failed'})

    def login(self, body):
        now = time.time()
        self.server.login_failures = [t for t in self.server.login_failures if now - t < 300]
        if len(self.server.login_failures) >= 10:
            return self.send(429, {'error': 'login_rate_limited'})
        cfg = optional_secret('dashboard')
        password = body.get('password', '')
        if not cfg:
            return self.send(503, {'error': 'setup_dashboard_password_in_terminal'})
        if not isinstance(password, str) or len(password) > 1024:
            return self.send(400, {'error': 'invalid_password'})
        if not hmac.compare_digest(password_hash(password, cfg['salt']), cfg['hash']):
            self.server.login_failures.append(now)
            return self.send(401, {'error': 'incorrect_password'})
        self.server.sessions = {k: v for k, v in self.server.sessions.items() if v[0] > now}
        if len(self.server.sessions) >= 20:
            self.server.sessions.pop(next(iter(self.server.sessions)))
        token = secrets.token_urlsafe(32)
        self.server.sessions[token] = (now + 43200, cfg['hash'])
        return self.send(200, {'ok': True}, cookie=f'mail_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('setup', 'serve'))
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', default=8787, type=int)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'setup':
        return setup()
    if args.demo and args.bind != '127.0.0.1':
        raise a.Failure('demo_localhost_only')
    if not STATIC.joinpath('index.html').is_file():
        raise a.Failure('build_frontend_first')
    with Server((args.bind, args.port), args.demo) as server:
        print(f'DASHBOARD_READY http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        a.log('dashboard_failed', code=exc.code if isinstance(exc, a.Failure) else 'operation_failed')
        raise SystemExit(1)
