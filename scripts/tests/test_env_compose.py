"""Validate secret isolation in real Compose config, without starting containers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
import env_config


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
class EnvironmentComposeTests(unittest.TestCase):
    def test_private_dotenv_values_are_not_expanded_into_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            values = {'ENV_CONFIG_VERSION': '1', 'TELEGRAM_BOT_TOKEN': "PRIVATE-$TOKEN#with'apostrophe\\end",
                      'GMAIL_CLIENT_SECRET': 'PRIVATE-GMAIL', 'MODEL_API_KEY': 'PRIVATE-MODEL',
                      'NGROK_AUTHTOKEN': 'PRIVATE-NGROK', 'SUMMARY_PROMPT': 'Private summary preferences'}
            env_config.update(path, values)
            result = subprocess.run(['docker', 'compose', '--env-file', str(path),
                                     '-f', str(SOURCE / 'compose.yaml'), '--profile', 'webhook',
                                     'config', '--format', 'json'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, 'Compose could not parse the test dotenv file')
            for value in values.values():
                if value != '1':
                    self.assertNotIn(value, result.stdout + result.stderr)
            self.assertNotIn('TOKEN', result.stderr)
            services = json.loads(result.stdout)['services']
            for service in services.values():
                self.assertEqual(service['environment']['AGENT_ENV_FILE'], '/run/agent-config.env')
                self.assertNotIn('TELEGRAM_BOT_TOKEN', service['environment'])
                mount = next(v for v in service['volumes'] if v['target'] == '/run/agent-config.env')
                self.assertTrue(mount['read_only'])
            self.assertEqual(env_config.read(path), values)


if __name__ == '__main__':
    unittest.main()
