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


def _linked_home(tmp_path, name, content='{"theme": "dark"}'):
    repo = tmp_path / 'dotfiles'
    repo.mkdir()
    target = repo / name
    target.write_text(content)
    home = tmp_path / 'home'
    (home / f'.{"codex" if name == "hooks.json" else "claude"}').mkdir(parents=True)
    link = home / f'.{"codex" if name == "hooks.json" else "claude"}' / name
    link.symlink_to(target)
    return home, target


def _untouched(target, content):
    assert target.read_text() == content
    assert not list(target.parent.glob('*.pre-devbox'))


def test_symlinked_settings_are_not_written_through(tmp_path):
    home, claude = _linked_home(tmp_path, 'settings.json')
    codex_home = home / '.codex'
    codex_home.mkdir()
    codex = tmp_path / 'dotfiles/hooks.json'
    codex.write_text('{}')
    (codex_home / 'hooks.json').symlink_to(codex)
    notes = install_hooks(tmp_path / 'root', home, remote_control=True)
    _untouched(claude, '{"theme": "dark"}')
    _untouched(codex, '{}')
    assert not list((home / '.claude').glob('*.pre-devbox'))
    assert len(notes) == 2 and 'managed by' in notes[0]


def test_ensure_remote_control_skips_symlink(tmp_path):
    home, target = _linked_home(tmp_path, 'settings.json')
    ensure_remote_control(home / '.claude/settings.json', True)
    _untouched(target, '{"theme": "dark"}')


def test_symlinked_agent_dir_is_not_written_through(tmp_path):
    repo = tmp_path / 'dotfiles/claude'
    repo.mkdir(parents=True)
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.claude').symlink_to(repo)
    install_hooks(tmp_path / 'root', home, remote_control=True)
    assert list(repo.iterdir()) == [repo / 'skills'] or not (repo / 'settings.json').exists()


def test_plain_files_still_get_hooks_and_no_notes(tmp_path):
    notes = install_hooks(tmp_path / 'root', tmp_path, remote_control=True)
    assert notes == []
    assert 'devbox-status' in (tmp_path / '.claude/settings.json').read_text()


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


# The marketplace and plugin names are a public interface (dotfiles#11): do not rename.
def test_marketplace_contract():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    market = json.loads((repo / '.claude-plugin/marketplace.json').read_text())
    assert market['name'] == 'devbox'
    entry = market['plugins'][0]
    assert entry['name'] == 'devbox'
    plugin = repo / entry['source']
    assert json.loads((plugin / '.claude-plugin/plugin.json').read_text())['name'] == 'devbox'
    from bootstrap import SKILLS
    assert all((plugin / 'skills' / s / 'SKILL.md').is_file() for s in SKILLS)


def make_bundle(tmp_path):
    base = tmp_path / '.local/share/devbox'
    (base / '.claude-plugin').mkdir(parents=True)
    for s in ('devbox-notes', 'devbox-browser', 'devbox-spawn', 'devbox-msg'):
        (base / 'plugins/devbox/skills' / s).mkdir(parents=True)
    return base


def test_enable_plugin_keeps_user_choices(tmp_path):
    from bootstrap import enable_plugin
    path = tmp_path / 's.json'
    path.write_text(json.dumps({'enabledPlugins': {'devbox@devbox': False, 'x@y': True}, 'extraKnownMarketplaces': {'y': {}}}))
    assert enable_plugin(path, tmp_path / 'b')
    data = json.loads(path.read_text())
    assert data['enabledPlugins'] == {'devbox@devbox': False, 'x@y': True}
    assert data['extraKnownMarketplaces']['y'] == {}
    assert data['extraKnownMarketplaces']['devbox']['source']['path'] == str(tmp_path / 'b')
    assert (tmp_path / 's.json.pre-devbox').exists()


def test_enable_plugin_fresh_and_idempotent(tmp_path):
    from bootstrap import enable_plugin
    path = tmp_path / '.claude/settings.json'
    enable_plugin(path, tmp_path / 'b')
    first = path.read_text()
    enable_plugin(path, tmp_path / 'b')
    assert path.read_text() == first
    assert json.loads(first)['enabledPlugins'] == {'devbox@devbox': True}
    assert not (tmp_path / '.claude/settings.json.pre-devbox').exists()


def test_enable_plugin_skips_symlinked_settings(tmp_path):
    from bootstrap import enable_plugin
    real = tmp_path / 'dot/settings.json'
    real.parent.mkdir()
    real.write_text('{"a": 1}')
    link = tmp_path / '.claude/settings.json'
    link.parent.mkdir()
    link.symlink_to(real)
    assert enable_plugin(link, tmp_path / 'b') is False
    assert real.read_text() == '{"a": 1}'
    assert not list(tmp_path.rglob('*.pre-devbox'))


def test_install_skills_replaces_only_owned_links(tmp_path):
    from bootstrap import install_skills
    base = make_bundle(tmp_path)
    home = tmp_path / 'home'
    codex, claude = home / '.codex/skills', home / '.claude/skills'
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    (codex / 'devbox-notes').symlink_to('/opt/devbox/app/skills/devbox-notes')  # stale image link
    (codex / 'devbox-msg').mkdir()                                              # user's own dir
    foreign = tmp_path / 'mine/devbox-spawn'
    foreign.mkdir(parents=True)
    (codex / 'devbox-spawn').symlink_to(foreign)                                # foreign link
    (claude / 'devbox-browser').symlink_to(base / 'skills/devbox-browser')      # legacy owned
    (claude / 'devbox-msg').symlink_to(foreign)                                 # foreign
    install_skills(base, home)
    skills = base / 'plugins/devbox/skills'
    assert (codex / 'devbox-notes').resolve() == (skills / 'devbox-notes').resolve()
    assert (codex / 'devbox-browser').resolve() == (skills / 'devbox-browser').resolve()
    assert (codex / 'devbox-msg').is_dir() and not (codex / 'devbox-msg').is_symlink()
    assert (codex / 'devbox-spawn').resolve() == foreign.resolve()
    # plugin not installed yet: the owned Claude link is re-pointed, not removed
    assert (claude / 'devbox-browser').resolve() == (skills / 'devbox-browser').resolve()
    assert (claude / 'devbox-msg').resolve() == foreign.resolve()


def test_owned_claude_links_removed_once_plugin_installed(tmp_path):
    from bootstrap import install_skills
    base = make_bundle(tmp_path)
    home = tmp_path / 'home'
    claude = home / '.claude/skills'
    claude.mkdir(parents=True)
    (claude / 'devbox-msg').symlink_to('/opt/devbox/app/skills/devbox-msg')
    (home / '.claude/plugins').mkdir()
    (home / '.claude/plugins/installed_plugins.json').write_text(json.dumps({'version': 2, 'plugins': {'devbox@devbox': [{}]}}))
    install_skills(base, home)
    assert not (claude / 'devbox-msg').is_symlink()
