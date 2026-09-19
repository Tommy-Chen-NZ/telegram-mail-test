"""Small, literal dotenv reader/writer. Never executes or expands values."""
import json
import os
from pathlib import Path
import re
import tempfile


class ConfigError(Exception):
    pass


FIELDS = {
    'telegram': {'token': 'TELEGRAM_BOT_TOKEN', 'chat_id': 'TELEGRAM_CHAT_ID'},
    'gmail': {'email': 'GMAIL_ADDRESS', 'app_password': 'GMAIL_APP_PASSWORD'},
    'gmail_oauth': {'topic': 'GMAIL_PUBSUB_TOPIC', 'client_id': 'GMAIL_CLIENT_ID',
                    'client_secret': 'GMAIL_CLIENT_SECRET', 'refresh_token': 'GMAIL_REFRESH_TOKEN'},
    'model': {'endpoint': 'MODEL_API_URL', 'model': 'MODEL_NAME', 'api_key': 'MODEL_API_KEY',
              'prompt': 'MODEL_BASE_PROMPT'},
    'webhook': {'audience': 'GMAIL_WEBHOOK_AUDIENCE', 'service_account': 'GMAIL_PUSH_SERVICE_ACCOUNT',
                'subscription': 'GMAIL_PUBSUB_SUBSCRIPTION'},
}
OPTIONAL = {'GMAIL_APP_PASSWORD', 'MODEL_BASE_PROMPT'}
ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def read(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open('rb') as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ConfigError('env_file_too_large')
        lines = raw.decode('utf-8-sig').splitlines()
        result = {}
        for line in lines:
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = ASSIGNMENT.fullmatch(line)
            if not match or match[1] in result:
                raise ConfigError('env_syntax_or_duplicate_key')
            key, value = match.groups()
            if value.startswith("'"):
                quoted = re.fullmatch(r"'((?:\\'|[^'])*)'\s*(?:#.*)?", value)
                if not quoted:
                    raise ConfigError('env_invalid_single_quote')
                value = quoted[1].replace("\\'", "'")
            elif value.startswith('"'):
                value, end = json.JSONDecoder().raw_decode(value)
                # JSON strings are used for escaped newlines, never literal multiline entries.
                tail = match[2][end:].strip()
                if not isinstance(value, str) or (tail and not tail.startswith('#')):
                    raise ConfigError('env_invalid_double_quote')
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
            if '\x00' in value:
                raise ConfigError('env_invalid_value')
            result[key] = value
        return result
    except (OSError, UnicodeError, ValueError):
        raise ConfigError('env_file_unreadable_or_invalid') from None


def quote(value):
    if not isinstance(value, str) or '\x00' in value:
        raise ConfigError('env_invalid_value')
    # Single quotes prevent Compose expansion of dollar signs in credentials.
    if '\n' in value or '\r' in value:
        if '$' in value:
            raise ConfigError('env_multiline_dollar_unsupported')
        return json.dumps(value, ensure_ascii=False)
    return "'" + value.replace("'", "\\'") + "'"


def update(path, changes):
    path = Path(path)
    read(path)  # Refuse to rewrite malformed/ambiguous input.
    encoded = {key: quote(value) for key, value in changes.items()}
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) for key in encoded):
        raise ConfigError('env_invalid_key')
    lines = path.read_text('utf-8-sig').splitlines() if path.exists() else []
    lines = [line for line in lines if not ((match := ASSIGNMENT.fullmatch(line)) and match[1] in changes)]
    content = '\n'.join(lines + [key + '=' + value for key, value in encoded.items()]) + '\n'
    if len(content.encode('utf-8')) > 131072:
        raise ConfigError('env_file_too_large')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.env-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def credential(values, name):
    fields = FIELDS[name]
    if any(not values.get(key) for key in fields.values() if key not in OPTIONAL):
        raise ConfigError('missing_or_invalid_' + name)
    return {field: values[key] for field, key in fields.items() if key in values}


def migrate(path, secrets, effective_model=None):
    """Keep legacy files; never overwrite conflicting manually entered values."""
    path, secrets = Path(path), Path(secrets)
    values = read(path)
    changes = {}
    def add(key, value):
        value = str(value)
        if key in values and values[key] != value:
            raise ConfigError('env_migration_conflict')
        changes[key] = value
    try:
        for name, fields in FIELDS.items():
            source = secrets / (name + '.json')
            if source.exists():
                cfg = json.loads(source.read_text('utf-8'))
                if not isinstance(cfg, dict) or set(cfg) - set(fields):
                    raise ConfigError('env_unsupported_legacy_config')
                if name == 'model' and effective_model:
                    cfg.update({key: effective_model[key] for key in ('model', 'endpoint')})
                for field, value in cfg.items():
                    add(fields[field], value)
        if effective_model and 'SUMMARY_PROMPT' not in values:
            changes['SUMMARY_PROMPT'] = effective_model['prompt']
        tunnel = secrets / 'ngrok/config.yml'
        if tunnel.exists():
            cfg = json.loads(tunnel.read_text('utf-8'))
            add('NGROK_AUTHTOKEN', cfg['agent']['authtoken'])
            add('NGROK_DOMAIN', cfg['endpoints'][0]['url'].removeprefix('https://').rstrip('/'))
        merged = values | changes
        for name in FIELDS:
            credential(merged, name)
        if not merged.get('NGROK_AUTHTOKEN') or not merged.get('NGROK_DOMAIN'):
            raise ConfigError('missing_or_invalid_ngrok')
        changes['ENV_CONFIG_VERSION'] = '1'
        update(path, changes)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ConfigError('env_migration_failed') from None
