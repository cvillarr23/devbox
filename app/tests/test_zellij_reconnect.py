"""Reconnect after a container/pod replacement starts a fresh terminal, never a resurrected one."""
import subprocess
import sys
from pathlib import Path

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


def test_exited_session_offers_suspended_claude_resume(monkeypatch, tmp_path):
    _resume(monkeypatch, tmp_path)
    layout = Path(spawn.terminal_command('stale', '/layout.kdl')[-3])
    text = layout.read_text()
    assert layout.name == 'resume-layout.kdl'
    assert 'claude --resume %s --model opus --permission-mode plan' % UUID in text
    assert 'start_suspended true' in text
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
