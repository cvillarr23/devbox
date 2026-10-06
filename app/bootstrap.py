"""Explicit helper installation; user hooks retain the agent's trust review."""
import base64
import io
import json
import os
import shlex
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PermissionRequest', 'Stop', 'SessionEnd', 'Interrupt']


def install_hooks(root, home, managed=False):
    home = Path(home)
    for agent in ('claude', 'codex'):
        command = shlex.join(['python3', str(root / 'devbox-status'), 'hook', '--agent', agent])
        if managed:
            if agent == 'codex':
                path = Path('/etc/codex/requirements.toml')
                text = '[features]\nhooks = true\n\n[hooks]\nmanaged_dir = ' + json.dumps(str(root)) + '\n'
                for event in EVENTS:
                    text += f'\n[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = "command"\ncommand = {json.dumps(command)}\ntimeout = {3 if event in ("SessionEnd", "Interrupt") else 5}\n'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                continue
            path = Path('/etc/claude-code/managed-settings.json')
        else:
            path = home / f'.{agent}' / ('hooks.json' if agent == 'codex' else 'settings.json')
        content = json.loads(path.read_text()) if path.exists() else {}
        hooks = content.setdefault('hooks', {})
        events = EVENTS if agent == 'codex' else EVENTS[:-1] + ['Notification']
        for event in events:
            groups = [g for g in hooks.get(event, []) if not any('devbox-status' in h.get('command', '') for h in g.get('hooks', []))]
            group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 3 if event in ('SessionEnd', 'Interrupt') else 5}]}
            if event == 'Notification':
                group['matcher'] = 'permission_prompt|elicitation_dialog'
            hooks[event] = groups + [group]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.with_suffix(path.suffix + '.pre-devbox').exists():
            path.with_suffix(path.suffix + '.pre-devbox').write_bytes(path.read_bytes())
        path.write_text(json.dumps(content, indent=2) + '\n')
    for agent in ('claude', 'codex'):
        for skill in ('devbox-notes', 'devbox-browser'):
            target = home / f'.{agent}/skills/{skill}'
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() and not target.is_symlink():
                target.symlink_to(root / 'skills' / skill)


def archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in ('remote.py', 'diagnostics.py', 'bootstrap.py', 'devbox_store.py', 'agent_process.py', 'codex_daemon.py', 'devbox-status', 'cli.py'):
            tar.add(ROOT / name, arcname=name)
        tar.add(ROOT.parent / 'config/zellij.kdl', arcname='zellij.kdl')
        tar.add(ROOT.parent / 'config/layout.kdl', arcname='layout.kdl')
        tar.add(ROOT / 'skills', arcname='skills')
    return base64.b64encode(buf.getvalue()).decode()


async def bootstrap(config):
    from environments import run, transport
    payload = archive()
    # Fixed program; data is transferred on stdin, never interpolated in shell commands.
    program = (ROOT / 'remote_install.py').read_text()
    return (await run(transport(config, ['python3', '-c', program]), payload.encode(), timeout=60)).decode()
