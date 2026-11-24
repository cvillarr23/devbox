"""Explicit helper installation; user hooks retain the agent's trust review."""
import base64
import io
import json
import os
import re
import shlex
import sys
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


def ensure_remote_control(path, enabled):
    """Set remoteControlAtStartup in user settings unless the user already chose a value.

    Managed settings cannot carry it: they are root-owned and fixed at image build,
    and they would override the user's own choice.
    """
    path = Path(path)
    content = json.loads(path.read_text()) if path.exists() else {}
    if 'remoteControlAtStartup' in content:
        return
    content['remoteControlAtStartup'] = bool(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    path.write_text(json.dumps(content, indent=2) + '\n')


CODEX_MCP_TABLE = re.compile(r'^\s*\[mcp_servers\.("devbox"|devbox)(\.[^\]]*)?\]', re.M)


def _write_atomic(path, text):
    """Replace path in one step so a reader never sees a half-written file, keeping its mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(f'.{path.name}.devbox-tmp')
    try:
        tmp.write_text(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _symlinked(path, hint):
    """A symlinked config is managed elsewhere (dotfiles), so never write through it."""
    if not path.is_symlink():
        return False
    print(f'devbox: not registering MCP server, {path} is a symlink; add this there:\n{hint}', file=sys.stderr)
    return True


def install_mcp(root, home):
    """Register the devbox MCP server for Claude and Codex in the user's own config.

    Same rule as the hooks: other entries are never touched, and an existing
    `devbox` entry is the user's to keep. Symlinked configs are skipped.
    Managed config is not used because Claude treats managed-mcp.json as the
    only MCP source.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    script = str(Path(root) / 'mcp_server.py')
    entry = {'type': 'stdio', 'command': 'python3', 'args': [script], 'env': {}}
    claude = home / '.claude.json'
    if not _symlinked(claude, f'"mcpServers": {{"devbox": {json.dumps(entry)}}}'):
        try:
            content = json.loads(claude.read_text()) if claude.exists() else {}
            servers = content.setdefault('mcpServers', {})
            if 'devbox' not in servers:
                servers['devbox'] = entry
                _backup_once(claude)
                _write_atomic(claude, json.dumps(content, indent=2) + '\n')
        except (ValueError, AttributeError):
            print(f'devbox: not registering MCP server, cannot parse {claude}', file=sys.stderr)
    codex = home / '.codex/config.toml'
    table = f'[mcp_servers.devbox]\ncommand = "python3"\nargs = [{json.dumps(script)}]\n'
    if not _symlinked(codex, table):
        text = codex.read_text() if codex.exists() else ''
        if not CODEX_MCP_TABLE.search(text):
            codex.parent.mkdir(parents=True, exist_ok=True)
            _backup_once(codex)
            _write_atomic(codex, text + ('\n' if text and not text.endswith('\n\n') else '') + table)


def install_hooks(root, home, managed=False, remote_control=None):
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
                # Agent versions are pinned in the image; self-update cannot write /usr/local.
                (path.parent / 'config.toml').write_text('check_for_update_on_startup = false\n')
                continue
            path = Path('/etc/claude-code/managed-settings.json')
        else:
            path = home / f'.{agent}' / ('hooks.json' if agent == 'codex' else 'settings.json')
        content = json.loads(path.read_text()) if path.exists() else {}
        if managed:
            content.setdefault('env', {})['DISABLE_AUTOUPDATER'] = '1'
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
    if not managed:
        install_mcp(root, home)


def archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in ('remote.py', 'diagnostics.py', 'bootstrap.py', 'devbox_store.py', 'agent_process.py', 'codex_daemon.py', 'devbox-status', 'cli.py', 'current_session.py', 'spawn.py', 'messages.py', 'pane_input.py', 'mcp_server.py'):
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
