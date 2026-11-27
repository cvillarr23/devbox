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


def run_kill(monkeypatch, capsys, *argv, answer='y'):
    seen = []
    monkeypatch.setattr(cli, 'current_session', lambda explicit=None: {'session': 'pm', 'pane': '1'})
    monkeypatch.setattr(cli, 'invoke', lambda s, op, args: seen.append((s, op, args)) or (
        {'would_end': ['a']} if args.get('dry_run') else {'ended': list(args['names']) or ['a']}))
    monkeypatch.setattr('builtins.input', lambda prompt='': answer)
    monkeypatch.setattr('sys.argv', ['cli.py', 'devbox', 'kill', *argv])
    cli.main()
    return seen, capsys.readouterr().out


def test_kill_names_invokes_runtime_kill(monkeypatch, capsys):
    seen, out = run_kill(monkeypatch, capsys, 'a', 'b')
    assert seen == [('pm', 'kill', {'names': ['a', 'b'], 'self': False, 'children': False})]
    assert 'ended a' in out


def test_kill_self_adds_caller(monkeypatch, capsys):
    seen, out = run_kill(monkeypatch, capsys, '--self')
    assert seen[0][2]['names'] == ['pm'] and seen[0][2]['self'] is True


def test_kill_children_confirms(monkeypatch, capsys):
    seen, out = run_kill(monkeypatch, capsys, '--children')
    assert [a.get('dry_run') for _, _, a in seen] == [True, None] and 'Will end: a' in out
    seen, _ = run_kill(monkeypatch, capsys, '--children', '--yes')
    assert len(seen) == 1
    import pytest
    with pytest.raises(ValueError, match='Cancelled'):
        run_kill(monkeypatch, capsys, '--children', answer='n')


def test_kill_needs_a_target(monkeypatch, capsys):
    import pytest
    with pytest.raises(ValueError):
        run_kill(monkeypatch, capsys)
