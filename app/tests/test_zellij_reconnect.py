"""Reconnect after a container/pod replacement starts a fresh terminal, never a resurrected one."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spawn

LISTING = ('live [Created 2m ago] (current)\n'
           'stale [Created 1h ago] (EXITED - attach to resurrect)\n')


def _fake_zellij(calls, listing=LISTING):
    def run(argv, **kwargs):
        calls.append(argv)
        stdout = listing if argv[1] == 'list-sessions' else ''
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')
    return run


def test_live_sessions_skip_exited(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', _fake_zellij([]))
    assert spawn.live_sessions() == ['live']


def test_live_session_is_attached(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', _fake_zellij(calls))
    command = spawn.terminal_command('live', '/layout.kdl')
    assert command[-2:] == ['attach', 'live']
    assert not any('delete-session' in argv for argv in calls)


def test_exited_session_is_deleted_and_started_fresh(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', _fake_zellij(calls))
    command = spawn.terminal_command('stale', '/layout.kdl')
    assert ['zellij', 'delete-session', 'stale'] in calls
    assert command[-4:] == ['--new-session-with-layout', '/layout.kdl', '--session', 'stale']


def test_unknown_session_is_started_fresh(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', _fake_zellij([], listing=''))
    assert 'attach' not in spawn.terminal_command('new', '/layout.kdl')


def test_session_serialization_is_off():
    config = (Path(__file__).resolve().parents[2] / 'config/zellij.kdl').read_text()
    assert 'session_serialization false' in config.splitlines()


UUID = '0b7f3c52-8a1e-4d2b-9c61-5e4f7a8b9c10'


def _resume(monkeypatch, tmp_path, **fields):
    import devbox_store as store
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path))
    store.write_spawn('stale', {'agent': fields.get('agent', 'claude')})
    resume = {'agent': 'claude', 'session_id': UUID, 'model': 'opus', 'permission_mode': 'plan',
              'cwd': str(tmp_path), 'updated_at': 1, **fields}
    (tmp_path / 'sessions/stale/resume.json').write_text(__import__('json').dumps(resume))
    monkeypatch.setattr(subprocess, 'run', _fake_zellij([]))


def test_exited_session_resumes_claude_immediately(monkeypatch, tmp_path):
    _resume(monkeypatch, tmp_path)
    layout = Path(spawn.terminal_command('stale', '/layout.kdl')[-3])
    text = layout.read_text()
    assert layout.name == 'resume-layout.kdl'
    assert 'claude --resume %s --model opus --permission-mode plan' % UUID in text
    assert 'start_suspended' not in text.split('htop')[0]
    assert 'initial prompt' not in text and 'prompt.md' not in text


def test_codex_resume_command(monkeypatch, tmp_path):
    _resume(monkeypatch, tmp_path, agent='codex', model='gpt-5', permission_mode='')
    text = Path(spawn.terminal_command('stale', '/layout.kdl')[-3]).read_text()
    assert 'codex resume -m gpt-5 %s' % UUID in text


def test_resume_failure_falls_back_to_shell_with_hint():
    script = spawn.resume_script({'agent': 'claude', 'session_id': UUID})
    assert "|| echo 'Could not resume; run '\\''claude --resume'\\'' to pick" in script
    assert script.endswith('exec "${SHELL:-sh}"')


def test_bad_or_missing_resume_uses_default_layout(monkeypatch, tmp_path):
    _resume(monkeypatch, tmp_path, session_id='x; rm -rf ~')
    assert spawn.terminal_command('stale', '/layout.kdl')[-3] == '/layout.kdl'
    (tmp_path / 'sessions/stale/resume.json').unlink()
    assert spawn.terminal_command('stale', '/layout.kdl')[-3] == '/layout.kdl'


def test_hook_records_resume_for_primary_agent_only(monkeypatch, tmp_path):
    import devbox_store as store
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path))
    store.write_spawn('s', {'agent': 'claude', 'model': 'sonnet'})
    event = {'hook_event_name': 'Stop', 'session_id': UUID, 'cwd': '/w', 'permission_mode': 'default'}
    store.record_agent_event('s', '1', 'codex', event, 1)
    assert store.read_resume('s') is None
    store.record_agent_event('s', '1', 'claude', event, 1)
    assert store.read_resume('s')['model'] == 'sonnet'
    store.record_agent_event('s', '1', 'claude', {**event, 'hook_event_name': 'SessionEnd'}, 1)
    assert store.read_resume('s')['session_id'] == UUID


OTHER_UUID = '9c2d4e10-3b7a-4f61-8d25-1a6b9e0c4f73'


def _report(store, event, session_id=UUID, **fields):
    store.record_agent_event('s', '1', 'claude',
                             {'hook_event_name': event, 'session_id': session_id, 'cwd': '/w', **fields}, 1)
    return store.read_resume('s')['permission_mode']


@pytest.fixture
def state(monkeypatch, tmp_path):
    import devbox_store as store
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path))
    return store


def test_report_on_resume_keeps_the_remembered_permission_mode(state):
    _report(state, 'Stop', permission_mode='plan')
    # Claude's SessionStart on resume names no mode; a second restart must still get it.
    assert _report(state, 'SessionStart', source='resume') == 'plan'


def test_latest_reported_permission_mode_beats_the_spawn_mode(state):
    state.write_spawn('s', {'agent': 'claude', 'permission_mode': 'auto'})
    assert _report(state, 'Stop', permission_mode='default') == 'default'
    assert _report(state, 'SessionStart', source='resume') == 'default'


def test_spawn_permission_mode_is_used_until_a_report_names_one(state):
    state.write_spawn('s', {'agent': 'claude', 'permission_mode': 'plan'})
    assert _report(state, 'SessionStart', source='startup') == 'plan'


def test_another_conversation_does_not_inherit_the_permission_mode(state):
    _report(state, 'Stop', permission_mode='plan')
    assert _report(state, 'SessionStart', session_id=OTHER_UUID, source='startup') == ''


@pytest.mark.parametrize('mode', ['default', 'manual', 'acceptEdits', 'dontAsk', 'plan', 'auto'])
def test_resume_puts_claude_back_in_its_permission_mode(mode):
    script = spawn.resume_script({'agent': 'claude', 'session_id': UUID, 'permission_mode': mode})
    assert 'claude --resume %s --permission-mode %s ||' % (UUID, mode) in script


@pytest.mark.parametrize('mode', ['bypassPermissions', '', 'plan; rm -rf ~', None])
def test_resume_never_turns_on_bypass_or_passes_unknown_modes(mode):
    script = spawn.resume_script({'agent': 'claude', 'session_id': UUID, 'permission_mode': mode})
    assert '--permission-mode' not in script


def test_restore_session_recreates_with_resume_layout(monkeypatch, tmp_path):
    _resume(monkeypatch, tmp_path)
    made = []
    monkeypatch.setattr(spawn, '_create_detached', lambda name, cwd, layout, env: made.append((name, cwd, Path(layout).name)))
    spawn.restore_session('stale', str(tmp_path))
    assert made == [('stale', str(tmp_path), 'resume-layout.kdl')]


def test_restore_session_leaves_live_session_alone(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', _fake_zellij([]))
    monkeypatch.setattr(spawn, '_create_detached', lambda *a: (_ for _ in ()).throw(AssertionError('recreated')))
    spawn.restore_session('live', '/')
