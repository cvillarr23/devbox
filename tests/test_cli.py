import json

import cli
import diagnostics
import remote

DETECTION_VARS = ('DEVBOX_SESSION', 'ZELLIJ_SESSION_NAME', 'CLAUDE_CODE_SESSION_ID',
                  'CODEX_THREAD_ID', 'CODEX_SESSION_ID')


def run_remote_doctor(monkeypatch, capsys, *argv):
    for var in DETECTION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('DEVBOX_REMOTE', '1')
    monkeypatch.setattr('sys.argv', ['cli.py', 'devbox', 'doctor', *argv])
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_remote_doctor_without_session_reports_the_target(state, monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, 'report', lambda: {'platform': {'machine': 'x'}})
    monkeypatch.setattr(remote, 'queue_request', lambda *a: (_ for _ in ()).throw(AssertionError('relayed')))
    assert run_remote_doctor(monkeypatch, capsys) == {'platform': {'machine': 'x'}, 'session': None}


def test_remote_doctor_uses_explicit_session_through_the_relay(state, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(remote, 'queue_request', lambda op, args: seen.append((op, args)) or {'session': 'work'})
    assert run_remote_doctor(monkeypatch, capsys, '--session', 'work') == {'session': 'work'}
    assert seen == [('doctor', {})]
