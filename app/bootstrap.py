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
SKILLS = ('devbox-notes', 'devbox-browser', 'devbox-spawn', 'devbox-msg')
MARKETPLACE = 'devbox'
PLUGIN_ID = 'devbox@devbox'
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


def plugin_base(root):
    """Directory holding .claude-plugin/ and plugins/: the bundle root remotely, /opt/devbox in the image."""
    root = Path(root)
    return root if (root / '.claude-plugin').is_dir() else root.parent


def plugin_settings(base):
    return {
        'extraKnownMarketplaces': {MARKETPLACE: {'source': {'source': 'directory', 'path': str(base)}}},
        'enabledPlugins': {PLUGIN_ID: True},
    }


def enable_plugin(path, base):
    """Register the devbox marketplace and enable its plugin in user settings.

    Values the user already set (including enabledPlugins false) are kept. A symlinked
    settings file is not ours to write through, so it is skipped; returns False then.
    """
    path = Path(path)
    if managed_link(path):
        return False
    content = json.loads(path.read_text()) if path.exists() else {}
    wanted = plugin_settings(base)
    before = json.dumps(content, sort_keys=True)
    # The marketplace path is devbox-owned and moves with the bundle, so always refresh it.
    content.setdefault('extraKnownMarketplaces', {})[MARKETPLACE] = wanted['extraKnownMarketplaces'][MARKETPLACE]
    content.setdefault('enabledPlugins', {}).setdefault(PLUGIN_ID, True)
    if json.dumps(content, sort_keys=True) != before:
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup_once(path)
        path.write_text(json.dumps(content, indent=2) + '\n')
    return True


def _owned_link(target, base):
    """True for a symlink into a devbox bundle: this one, the image's, or a remote one."""
    if not target.is_symlink():
        return False
    dest = os.readlink(target)
    in_bundle = dest.startswith((str(base) + '/', '/opt/devbox/')) or '/.local/share/devbox/' in dest
    return in_bundle and Path(dest).name in SKILLS


def plugin_installed(home):
    try:
        data = json.loads((Path(home) / '.claude/plugins/installed_plugins.json').read_text())
        return bool(data.get('plugins', {}).get(PLUGIN_ID))
    except (OSError, ValueError, AttributeError):
        return False


def install_skills(root, home):
    """Link skills for Codex and migrate the old Claude links to the plugin.

    Owned Claude links are re-pointed at the bundle until Claude Code has actually
    installed the plugin, then removed, so no session is left without skills.
    Only symlinks into a devbox bundle are replaced or removed; user skills are kept.
    """
    home = Path(home)
    base = plugin_base(root)
    skills = base / 'plugins/devbox/skills'
    for skill in SKILLS:
        legacy = home / '.claude/skills' / skill
        if _owned_link(legacy, base):
            legacy.unlink()
            if not plugin_installed(home):
                legacy.symlink_to(skills / skill)
        target = home / '.codex/skills' / skill
        if _owned_link(target, base):
            target.unlink()
        if not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(skills / skill)


def _plugin_note(path):
    return f'devbox plugin not enabled: {path} is a symlink; add enabledPlugins "{PLUGIN_ID}" and the devbox marketplace there'


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
                # Agent versions are pinned in the image; self-update cannot write /usr/local.
                (path.parent / 'config.toml').write_text('check_for_update_on_startup = false\n')
                continue
            path = Path('/etc/claude-code/managed-settings.json')
            managed_plugin = plugin_settings(plugin_base(root))
        else:
            path = home / f'.{agent}' / ('hooks.json' if agent == 'codex' else 'settings.json')
            target = managed_link(path)
            if target:
                notes.append(_managed_note(agent, path, target))
                if agent == 'claude':
                    print(_plugin_note(path), file=sys.stderr)
                continue
        content = json.loads(path.read_text()) if path.exists() else {}
        if managed:
            content.setdefault('env', {})['DISABLE_AUTOUPDATER'] = '1'
        if managed and agent == 'claude':
            # Marketplace only: enabling lives in user settings (home_setup) so users can disable it.
            content.setdefault('extraKnownMarketplaces', {}).update(managed_plugin['extraKnownMarketplaces'])
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
        if agent == 'claude' and not managed:
            if remote_control is not None:
                ensure_remote_control(path, remote_control)
            if not enable_plugin(path, plugin_base(root)):
                print(_plugin_note(path), file=sys.stderr)
    install_skills(root, home)
    if not managed:
        install_mcp(root, home)
    return notes


def archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in ('remote.py', 'diagnostics.py', 'bootstrap.py', 'devbox_store.py', 'agent_process.py', 'codex_daemon.py', 'devbox-status', 'cli.py', 'current_session.py', 'spawn.py', 'messages.py', 'pane_input.py', 'mcp_server.py'):
            tar.add(ROOT / name, arcname=name)
        tar.add(ROOT.parent / 'config/zellij.kdl', arcname='zellij.kdl')
        tar.add(ROOT.parent / 'config/layout.kdl', arcname='layout.kdl')
        tar.add(ROOT.parent / '.claude-plugin', arcname='.claude-plugin')
        tar.add(ROOT.parent / 'plugins', arcname='plugins')
    return base64.b64encode(buf.getvalue()).decode()


async def bootstrap(config):
    from environments import run, transport
    payload = archive()
    # Fixed program; data is transferred on stdin, never interpolated in shell commands.
    program = (ROOT / 'remote_install.py').read_text()
    return (await run(transport(config, ['python3', '-c', program, '1' if remote_control_enabled() else '0']), payload.encode(), timeout=60)).decode()
