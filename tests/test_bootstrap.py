import json

import pytest

from bootstrap import ensure_remote_control, install_hooks, remote_control_enabled


@pytest.mark.parametrize('value,expected', [(None, True), ('1', True), ('yes', True), ('0', False), ('false', False), ('OFF', False), (' no ', False)])
def test_remote_control_env(value, expected):
    env = {} if value is None else {'DEVBOX_CLAUDE_REMOTE_CONTROL': value}
    assert remote_control_enabled(env) is expected


def test_fresh_settings_get_key(tmp_path):
    path = tmp_path / '.claude/settings.json'
    ensure_remote_control(path, True)
    assert json.loads(path.read_text()) == {'remoteControlAtStartup': True}


def test_disabled_writes_false(tmp_path):
    path = tmp_path / 's.json'
    ensure_remote_control(path, False)
    assert json.loads(path.read_text())['remoteControlAtStartup'] is False


@pytest.mark.parametrize('existing', [True, False])
def test_existing_value_and_keys_are_kept(tmp_path, existing):
    path = tmp_path / 's.json'
    path.write_text(json.dumps({'remoteControlAtStartup': existing, 'theme': 'dark'}))
    ensure_remote_control(path, not existing)
    assert json.loads(path.read_text()) == {'remoteControlAtStartup': existing, 'theme': 'dark'}


def test_other_keys_kept_and_backup_made_once(tmp_path):
    path = tmp_path / 's.json'
    path.write_text('{"theme": "dark"}')
    ensure_remote_control(path, True)
    backup = tmp_path / 's.json.pre-devbox'
    assert json.loads(backup.read_text()) == {'theme': 'dark'}
    assert json.loads(path.read_text()) == {'theme': 'dark', 'remoteControlAtStartup': True}
    path.write_text('{"x": 1}')
    ensure_remote_control(path, True)
    assert json.loads(backup.read_text()) == {'theme': 'dark'}


def test_install_hooks_user_settings_only_with_flag(tmp_path):
    root = tmp_path / 'root'
    install_hooks(root, tmp_path / 'a')
    assert 'remoteControlAtStartup' not in json.loads((tmp_path / 'a/.claude/settings.json').read_text())
    install_hooks(root, tmp_path / 'b', remote_control=False)
    data = json.loads((tmp_path / 'b/.claude/settings.json').read_text())
    assert data['remoteControlAtStartup'] is False and 'hooks' in data


def test_install_mcp_registers_both_agents(tmp_path):
    from bootstrap import install_mcp
    install_mcp(tmp_path / 'app', tmp_path)
    server = json.loads((tmp_path / '.claude.json').read_text())['mcpServers']['devbox']
    assert server['command'] == 'python3' and server['args'] == [str(tmp_path / 'app/mcp_server.py')]
    assert '[mcp_servers.devbox]' in (tmp_path / '.codex/config.toml').read_text()


def test_install_mcp_keeps_existing_registrations(tmp_path):
    from bootstrap import install_mcp
    (tmp_path / '.claude.json').write_text(json.dumps({'theme': 'dark', 'mcpServers': {'other': {'command': 'x'}}}))
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/config.toml').write_text('model = "gpt"\n[mcp_servers.other]\ncommand = "y"\n')
    install_mcp(tmp_path / 'app', tmp_path)
    claude = json.loads((tmp_path / '.claude.json').read_text())
    assert claude['theme'] == 'dark' and claude['mcpServers']['other'] == {'command': 'x'} and 'devbox' in claude['mcpServers']
    codex = (tmp_path / '.codex/config.toml').read_text()
    assert codex.startswith('model = "gpt"\n[mcp_servers.other]\ncommand = "y"\n') and '[mcp_servers.devbox]' in codex
    assert json.loads((tmp_path / '.claude.json.pre-devbox').read_text())['theme'] == 'dark'


def test_install_mcp_keeps_user_devbox_entry_and_is_idempotent(tmp_path):
    from bootstrap import install_mcp
    (tmp_path / '.claude.json').write_text(json.dumps({'mcpServers': {'devbox': {'command': 'mine'}}}))
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/config.toml').write_text('[mcp_servers."devbox"]\ncommand = "mine"\n')
    install_mcp(tmp_path / 'app', tmp_path)
    assert json.loads((tmp_path / '.claude.json').read_text())['mcpServers']['devbox'] == {'command': 'mine'}
    assert (tmp_path / '.codex/config.toml').read_text() == '[mcp_servers."devbox"]\ncommand = "mine"\n'
    fresh = tmp_path / 'fresh'
    install_mcp(tmp_path / 'app', fresh)
    before = [(fresh / '.claude.json').read_text(), (fresh / '.codex/config.toml').read_text()]
    install_mcp(tmp_path / 'app', fresh)
    assert before == [(fresh / '.claude.json').read_text(), (fresh / '.codex/config.toml').read_text()]


def test_install_mcp_leaves_unparseable_claude_json(tmp_path):
    from bootstrap import install_mcp
    (tmp_path / '.claude.json').write_text('{broken')
    install_mcp(tmp_path / 'app', tmp_path)
    assert (tmp_path / '.claude.json').read_text() == '{broken'


def test_user_install_hooks_registers_mcp(tmp_path):
    install_hooks(tmp_path / 'app', tmp_path)
    assert 'devbox' in json.loads((tmp_path / '.claude.json').read_text())['mcpServers']
    assert '[mcp_servers.devbox]' in (tmp_path / '.codex/config.toml').read_text()


@pytest.mark.parametrize('name', ['.claude.json', '.codex/config.toml'])
def test_install_mcp_skips_symlinked_config(tmp_path, capsys, name):
    from bootstrap import install_mcp
    target = tmp_path / 'dotfiles/target'
    target.parent.mkdir()
    target.write_text('{}' if name.endswith('json') else '')
    link = tmp_path / 'home' / name
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    install_mcp(tmp_path / 'app', tmp_path / 'home')
    assert link.is_symlink() and target.read_text() == ('{}' if name.endswith('json') else '')
    assert str(link) in capsys.readouterr().err
    other = tmp_path / 'home' / ('.codex/config.toml' if name.endswith('json') else '.claude.json')
    assert other.exists() and not other.is_symlink()


def test_install_mcp_writes_atomically_and_keeps_mode(tmp_path):
    from bootstrap import install_mcp
    (tmp_path / '.claude.json').write_text('{"theme": "dark"}')
    (tmp_path / '.claude.json').chmod(0o600)
    install_mcp(tmp_path / 'app', tmp_path)
    assert (tmp_path / '.claude.json').stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir() if 'tmp' in p.name) == []
    fresh = tmp_path / 'fresh'
    install_mcp(tmp_path / 'app', fresh)
    assert (fresh / '.claude.json').stat().st_mode & 0o777 == 0o600
