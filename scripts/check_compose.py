"""Validate the deployment constraints without starting containers."""
import json
import sys


def check(config):
    for name, service in config['services'].items():
        assert service['network_mode'] == 'host', name + ': host networking required'
        assert not service.get('ports'), name + ': ports must be absent'
        assert service['restart'] == 'unless-stopped', name + ': restart policy changed'
        mounts = {v['target']: v for v in service['volumes']}
        assert mounts['/data']['type'] == 'bind', name + ': persistent data required'
        assert mounts['/run/agent-secrets']['read_only'], name + ': credentials must be read-only'
    assert config['services']['dashboard']['command'] == ['serve', '--bind', '127.0.0.1', '--port', '8787']


if __name__ == '__main__':
    check(json.load(sys.stdin))
    print('COMPOSE_CONSTRAINTS_OK')
