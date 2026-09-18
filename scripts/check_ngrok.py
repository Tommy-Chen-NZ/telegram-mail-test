"""Offline checks and a fake credential fixture for the pinned ngrok CLI parser."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ngrok_setup


if __name__ == '__main__':
    config = json.load(sys.stdin)
    service = config['services']['ngrok']
    assert service['network_mode'] == 'host' and not service.get('ports')
    assert service['restart'] == 'unless-stopped' and service['read_only']
    assert '@sha256:' in service['image']
    assert len(service['volumes']) == 1
    mount = service['volumes'][0]
    assert mount['target'] == '/run/ngrok' and mount['read_only']
    assert not mount.get('bind', {}).get('create_host_path', False)
    assert service['logging']['driver'] == 'none'
    assert int(service['mem_limit']) == 64 * 1024 * 1024
    fixture = Path(sys.argv[1])
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / 'config.yml').write_text(json.dumps(ngrok_setup.config('example.ngrok.app', 'test-only-token')))
    print('NGROK_COMPOSE_OK')
