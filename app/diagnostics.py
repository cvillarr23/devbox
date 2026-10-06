"""Tool versions and hook permissions; never inspect login state or prompts."""
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def report():
    tools = {}
    for name, args in (
        ('codex', ['--version']), ('claude', ['--version']), ('zellij', ['--version']),
        ('ttyd', ['--version']), ('google-chrome', ['--version']), ('node', ['--version']),
        ('python3', ['--version']), ('git', ['--version']),
        ('kubectl', ['version', '--client', '-o', 'json']),
    ):
        executable = shutil.which(name)
        entry = {'available': bool(executable)}
        if executable:
            try:
                result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=3)
                output = result.stdout.strip() or result.stderr.strip()
                if name == 'kubectl' and result.returncode == 0:
                    output = json.loads(output)['clientVersion']['gitVersion']
                entry.update(version=output.splitlines()[0][:300] if output else '', ok=result.returncode == 0)
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                entry['ok'] = False
        tools[name] = entry
    sandbox = {'supported': False}
    if shutil.which('codex'):
        try:
            result = subprocess.run(['codex', 'sandbox', '--', 'true'], capture_output=True, text=True, timeout=5)
            sandbox['supported'] = result.returncode == 0
            if result.returncode:
                sandbox['error'] = result.stderr.strip().splitlines()[0][:300] if result.stderr.strip() else 'Sandbox probe failed'
        except (OSError, subprocess.TimeoutExpired):
            sandbox['error'] = 'Sandbox probe failed or timed out'
    return {'sandbox': sandbox, 'tools': tools, 'hook_permissions': {
        'helper_readable': os.access(ROOT / 'devbox-status', os.R_OK),
        'codex_managed_readable': os.access('/etc/codex/requirements.toml', os.R_OK),
        'codex_user_readable': os.access(Path.home() / '.codex/hooks.json', os.R_OK),
        'claude_managed_readable': os.access('/etc/claude-code/managed-settings.json', os.R_OK),
        'claude_user_readable': os.access(Path.home() / '.claude/settings.json', os.R_OK),
    }}
