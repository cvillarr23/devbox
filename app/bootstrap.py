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


def remote_control_enabled(env=None):
    value = (os.environ if env is None else env).get('DEVBOX_CLAUDE_REMOTE_CONTROL', '1')
    return value.strip().lower() not in ('0', 'false', 'no', 'off')


def _backup_once(path):
    backup = path.with_suffix(path.suffix + '.pre-devbox')
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())


def managed_link(path):
    """Return where a symlinked settings file (or its directory) points, else None.

    Dotfiles installers link agent settings into their checkout; writing through the
    link would put devbox's entries in the user's repo.
    """
    path = Path(path)
    for candidate in (path, path.parent):
        if candidate.is_symlink():
            return candidate.resolve()
    return None


def ensure_remote_control(path, enabled):
    """Set remoteControlAtStartup in user settings unless the user already chose a value.

    Managed settings cannot carry it: they are root-owned and fixed at image build,
    and they would override the user's own choice.
    """
    path = Path(path)
    if managed_link(path):
        return
    content = json.loads(path.read_text()) if path.exists() else {}
    if 'remoteControlAtStartup' in content:
        return
    content['remoteControlAtStartup'] = bool(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    path.write_text(json.dumps(content, indent=2) + '\n')


def _managed_note(agent, path, target):
    return f'{agent}: hooks not installed: {path.name} is managed by {target}; add the devbox hooks there'


def install_hooks(root, home, managed=False, remote_control=None):
    """Install hooks; return notes for user settings left alone because they are symlinked."""
    home = Path(home)
    notes = []
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
            target = managed_link(path)
            if target:
                notes.append(_managed_note(agent, path, target))
                continue
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
        _backup_once(path)
        path.write_text(json.dumps(content, indent=2) + '\n')
        if agent == 'claude' and not managed and remote_control is not None:
            ensure_remote_control(path, remote_control)
    for agent in ('claude', 'codex'):
        for skill in ('devbox-notes', 'devbox-browser', 'devbox-spawn', 'devbox-msg'):
            target = home / f'.{agent}/skills/{skill}'
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() and not target.is_symlink():
                target.symlink_to(root / 'skills' / skill)
    return notes


def archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in ('remote.py', 'diagnostics.py', 'bootstrap.py', 'devbox_store.py', 'agent_process.py', 'codex_daemon.py', 'devbox-status', 'cli.py', 'current_session.py', 'spawn.py', 'messages.py', 'pane_input.py'):
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
    return (await run(transport(config, ['python3', '-c', program, '1' if remote_control_enabled() else '0']), payload.encode(), timeout=60)).decode()
