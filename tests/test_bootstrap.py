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
