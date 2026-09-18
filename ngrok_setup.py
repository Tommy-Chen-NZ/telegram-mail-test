"""Configure a fixed ngrok HTTPS endpoint without exposing the authtoken."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

import mailagent as a


def private_write(path, content):
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def config(domain, token):
    domain = domain.strip().lower().removeprefix('https://').rstrip('/')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:ngrok-free\.app|ngrok-free\.dev|ngrok\.app|ngrok\.dev)', domain):
        raise a.Failure('expected_assigned_ngrok_domain')
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        raise a.Failure('invalid_ngrok_token')
    return {'version': '3', 'agent': {
        'authtoken': token, 'console_ui': False, 'web_addr': False,
        'inspect_db_size': -1, 'log': 'stdout', 'log_format': 'json',
        'log_level': 'warn', 'update_check': False, 'remote_management': False,
    }, 'endpoints': [{'name': 'mail-webhook', 'url': 'https://' + domain,
                      'upstream': {'url': 'http://127.0.0.1:8080'}}]}


def configure(root, domain, token, project):
    cfg = config(domain, token)
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', project):
        raise a.Failure('invalid_google_project_id')
    if not (root / '.env').is_file():
        raise a.Failure('run_from_prepared_project_directory')
    webhook = {'audience': cfg['endpoints'][0]['url'] + '/webhooks/gmail',
               'service_account': 'gmail-webhook-push@' + project + '.iam.gserviceaccount.com',
               'subscription': 'projects/' + project + '/subscriptions/gmail-push'}
    secret_dir = root / 'secrets'
    existing = secret_dir / 'webhook.json'
    if existing.exists() and json.loads(existing.read_text()) != webhook:
        raise a.Failure('existing_webhook_configuration_conflict')
    tunnel_dir = secret_dir / 'ngrok'
    tunnel_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secret_dir, 0o700)
    os.chmod(tunnel_dir, 0o700)
    private_write(tunnel_dir / 'config.yml', json.dumps(cfg, indent=2) + '\n')
    private_write(existing, json.dumps(webhook) + '\n')
    env = root / '.env'
    lines = [line for line in env.read_text().splitlines() if not line.startswith('WEBHOOK_BIND=')]
    private_write(env, '\n'.join(lines + ['WEBHOOK_BIND=127.0.0.1']) + '\n')
    return webhook['audience']


def request_result(url, data=None):
    request = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', 'User-Agent': 'MailAgentTunnelCheck/1.0'})
    try:
        with urllib.request.build_opener(a.NoRedirect()).open(request, timeout=15) as response:
            return response.status, response.read(4096), response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096), exc.headers.get_content_type()


def verify(root):
    # Fixed local target and constrained ngrok host; do not probe arbitrary URLs.
    cfg = json.loads((root / 'secrets/ngrok/config.yml').read_text())
    public = config(cfg['endpoints'][0]['url'], 'validation-only')['endpoints'][0]['url']
    expected_health = (200, b'', 'application/json')
    if request_result('http://127.0.0.1:8080/healthz') != expected_health:
        raise a.Failure('local_webhook_health_failed')
    print('LOCAL_WEBHOOK_OK')
    if request_result(public + '/healthz') != expected_health:
        raise a.Failure('public_webhook_health_failed')
    print('PUBLIC_WEBHOOK_OK: HTTP 200, empty JSON response matches localhost')
    status, body, content_type = request_result(public + '/webhooks/gmail', b'{}')
    if status != 401 or content_type != 'application/json' or json.loads(body) != {'error': 'unauthorized'}:
        raise a.Failure('unsigned_webhook_not_rejected')
    print('UNSIGNED_WEBHOOK_REJECTED: HTTP 401, unauthorized')
    print('TUNNEL_VERIFIED: ' + public + '/webhooks/gmail')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('setup', 'verify'))
    args = parser.parse_args()
    root = Path.cwd()
    os.umask(0o077)
    if args.command == 'setup':
        domain = input('Assigned ngrok domain (no token): ').strip()
        project = input('Google Cloud project ID: ').strip()
        token = a.hidden('ngrok authtoken (hidden): ')
        url = configure(root, domain, token, project)
        print('NGROK_CONFIG_OK: ' + url)
    else:
        verify(root)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        a.log('ngrok_setup_failed', code=exc.code if isinstance(exc, a.Failure) else 'operation_failed')
        sys.exit(1)
