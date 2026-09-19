"""Validate the deployment constraints without starting containers."""
import json
import sys


def check(config):
    assert set(config['services']) == {'agent', 'webhook'}, 'Only worker and webhook services expected'
    for name, service in config['services'].items():
        assert service['network_mode'] == 'host', name + ': host networking required'
        assert not service.get('ports'), name + ': ports must be absent'
        assert service['restart'] == 'unless-stopped', name + ': restart policy changed'
        mounts = {v['target']: v for v in service['volumes']}
        assert mounts['/data']['type'] == 'bind', name + ': persistent data required'
        assert mounts['/run/agent-secrets']['read_only'], name + ': credentials must be read-only'
        assert mounts['/run/agent-config.env']['read_only'], name + ': dotenv must be read-only'
        assert service['environment']['AGENT_ENV_FILE'] == '/run/agent-config.env'
        assert set(service['environment']) == {'AGENT_DATA', 'AGENT_SECRETS', 'AGENT_ENV_FILE'}, 'No credentials in container environment'


if __name__ == '__main__':
    check(json.load(sys.stdin))
    print('COMPOSE_CONSTRAINTS_OK')
