"""Attach falls back to the nearest existing parent directory when a session's cwd was deleted."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote
import spawn


def test_existing_cwd_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir('/')
    spawn.enter_cwd(str(tmp_path))
    assert os.getcwd() == str(tmp_path)
    assert capsys.readouterr().out == ''


def test_deleted_cwd_uses_parent_and_prints_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir('/')
    gone = tmp_path / 'worktree'
    spawn.enter_cwd(str(gone))
    assert os.getcwd() == str(tmp_path)
    assert capsys.readouterr().out == f'devbox: {gone} no longer exists; starting in {tmp_path}\n'


def test_deleted_parents_fall_back_further(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir('/')
    spawn.enter_cwd(str(tmp_path / 'a' / 'b' / 'c'))
    assert os.getcwd() == str(tmp_path)
    assert 'no longer exists' in capsys.readouterr().out


def test_relative_missing_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.chdir('/')
    spawn.enter_cwd('relative-missing/x')
    assert os.getcwd() == str(tmp_path)


def test_remote_attach_with_deleted_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir('/')
    execs = []
    monkeypatch.setattr(spawn, 'terminal_command', lambda name, layout: ['zellij', 'attach', name])
    monkeypatch.setattr(os, 'execvp', lambda f, a: execs.append(a))
    remote.attach('sess', str(tmp_path / 'gone'))
    assert os.getcwd() == str(tmp_path)
    assert execs == [['zellij', 'attach', 'sess']]
    assert 'no longer exists' in capsys.readouterr().out
