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
