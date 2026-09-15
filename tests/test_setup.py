"""Release bootstrap must be safe to repeat and independent of the maintainer's paths."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='akari-bootstrap-')
        self.base = Path(self.temp.name)
        for name in ('scripts/init_config.py', 'scripts/render_services.py', 'config.example.json', 'music/config.example.json'):
            destination = self.base / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(self.base / 'scripts' / name), *args],
                              capture_output=True, text=True, timeout=10)

    def test_initialization_absolutizes_paths_without_enabling_optional_services(self):
        self.assertEqual(self.run_script('init_config.py').returncode, 0)
        config = json.loads((self.base / 'config.json').read_text())
        self.assertEqual(config['music_config'], str(self.base / 'music/config.json'))
        self.assertEqual(config['agent_workdir'], str(self.base / 'work'))
        self.assertEqual(config['agent_allowed_openids'], [])
        self.assertFalse(config['agent_enabled'])
        self.assertFalse(config['music_enabled'])
        self.assertEqual((self.base / 'config.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.base / 'secrets/llm.key').exists())

    def test_reinitialization_preserves_existing_credentials(self):
        self.run_script('init_config.py')
        (self.base / 'config.json').write_text('{"app_secret":"local-private-value"}')
        self.assertEqual(self.run_script('init_config.py').returncode, 0)
        self.assertEqual(json.loads((self.base / 'config.json').read_text()), {'app_secret':'local-private-value'})

    def test_service_generation_is_local_and_uses_selected_port(self):
        result = self.run_script('render_services.py', '--user', 'akari', '--port', '19763')
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.base / '.local/systemd/akari-admin.service').read_text()
        self.assertIn('User=akari', unit)
        self.assertIn('--port 19763', unit)
        self.assertIn('WorkingDirectory=' + str(self.base) + '\n', unit)
        self.assertNotIn('/home/user/.akari', unit)
        self.assertNotEqual(self.run_script('render_services.py', '--user', 'bad\nUser=root').returncode, 0)
