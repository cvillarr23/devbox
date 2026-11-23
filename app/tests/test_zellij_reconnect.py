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
