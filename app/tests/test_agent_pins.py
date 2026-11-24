import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bootstrap
import diagnostics


class AgentPinsTest(unittest.TestCase):
    def test_managed_install_disables_agent_self_update(self):
        real = bootstrap.Path
        with tempfile.TemporaryDirectory() as tmp:
            redirect = lambda p, *a: real(tmp + p) if str(p).startswith('/etc') else real(p, *a)
            with patch.object(bootstrap, 'Path', side_effect=redirect):
                bootstrap.install_hooks(real(ROOT), tmp, managed=True)
            claude = json.loads(real(tmp, 'etc/claude-code/managed-settings.json').read_text())
            self.assertEqual(claude['env']['DISABLE_AUTOUPDATER'], '1')
            self.assertIn('check_for_update_on_startup = false', real(tmp, 'etc/codex/config.toml').read_text())

    def test_report_flags_stale_agent_pin(self):
        with patch.object(diagnostics, 'latest_version', return_value='9.9.9'):
            tools = diagnostics.report()['tools']
        for name in ('claude', 'codex'):
            if tools[name].get('version'):
                self.assertEqual(tools[name]['latest'], '9.9.9')
                self.assertTrue(tools[name]['stale'])


if __name__ == '__main__':
    unittest.main()
