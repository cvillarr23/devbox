"""Tool versions, platform, confinement and hook permissions; never inspect login state or prompts."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None


def confinement():
    """AppArmor label and seccomp mode of this process, as the kernel reports them.

    Docker Desktop's VM has no AppArmor, so the label reads `unconfined` there;
    seccomp mode 2 means the custom filter is loaded.
    """
    apparmor = _read('/proc/self/attr/current')
    status = _read('/proc/self/status') or ''
    seccomp = next((line.split(':', 1)[1].strip() for line in status.splitlines()
                    if line.startswith('Seccomp:')), None)
    return {'apparmor': apparmor.strip('\x00\n ') if apparmor is not None else None,
            'seccomp': int(seccomp) if seccomp and seccomp.isdigit() else None}


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
    return {'platform': {'machine': platform.machine()}, 'confinement': confinement(),
            'sandbox': sandbox, 'tools': tools, 'hook_permissions': {
        'helper_readable': os.access(ROOT / 'devbox-status', os.R_OK),
        'codex_managed_readable': os.access('/etc/codex/requirements.toml', os.R_OK),
        'codex_user_readable': os.access(Path.home() / '.codex/hooks.json', os.R_OK),
        'claude_managed_readable': os.access('/etc/claude-code/managed-settings.json', os.R_OK),
        'claude_user_readable': os.access(Path.home() / '.claude/settings.json', os.R_OK),
    }}
