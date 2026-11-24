from pathlib import Path

import pytest

import home_setup
from home_setup import main, run_installer, setup


def make_dotfiles(path, script='#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" > "$HOME/installer-ran"\n'):
    path.mkdir(parents=True)
    installer = path / 'install.sh'
    installer.write_text(script)
    installer.chmod(0o755)
    return installer


def run_main(monkeypatch, home, *argv):
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr('sys.argv', ['devbox-setup-home', *argv])
    main()


def test_existing_home_is_preserved(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.write_text('custom shell config\n')
    setup(tmp_path)
    assert rc.read_text() == 'custom shell config\n'


def test_seeded_shell_files_know_nothing_about_dotfiles(tmp_path):
    setup(tmp_path)
    for name in ('.zshrc', '.zshenv'):
        text = (tmp_path / name).read_text()
        assert 'dotfiles' not in text
        assert 'command claude' in text or name == '.zshenv'
    assert not (tmp_path / '.config/devbox/dotfiles-path').exists()


def test_no_dotfiles_runs_no_installer(tmp_path, monkeypatch):
    run_main(monkeypatch, tmp_path)
    assert (tmp_path / '.zshrc').is_file()
    assert not (tmp_path / 'installer-ran').exists()


def test_installer_runs_in_checkout_with_forwarded_args(tmp_path, monkeypatch):
    repo = tmp_path / 'dev/dotfiles'
    make_dotfiles(repo)
    run_main(monkeypatch, tmp_path, '--dotfiles', str(repo),
             '--dotfiles-arg=--no-roo', '--dotfiles-arg', 'plugins=a,b')
    assert (tmp_path / 'installer-ran').read_text() == f'{repo.resolve()}|--no-roo plugins=a,b\n'


def test_custom_installer_path_is_relative_to_checkout(tmp_path, monkeypatch):
    repo = tmp_path / 'dotfiles'
    make_dotfiles(repo)
    (repo / 'scripts').mkdir()
    custom = repo / 'scripts/work.sh'
    custom.write_text('#!/bin/sh\ntouch "$HOME/custom-ran"\n')
    custom.chmod(0o755)
    run_main(monkeypatch, tmp_path, '--dotfiles', str(repo), '--dotfiles-installer', 'scripts/work.sh')
    assert (tmp_path / 'custom-ran').exists()
    assert not (tmp_path / 'installer-ran').exists()


def test_existing_shell_files_are_backed_up_before_installer(tmp_path, monkeypatch):
    repo = tmp_path / 'dotfiles'
    make_dotfiles(repo)
    (tmp_path / '.zshrc').write_text('custom shell config\n')
    run_main(monkeypatch, tmp_path, '--dotfiles', str(repo))
    assert next(tmp_path.glob('.zshrc.pre-devbox-*')).read_text() == 'custom shell config\n'


def test_installer_may_replace_shell_files_and_restart_keeps_them(tmp_path, monkeypatch):
    repo = tmp_path / 'dotfiles'
    make_dotfiles(repo, '#!/bin/sh\nprintf "mine\\n" > "$HOME/.zshrc"\n')
    run_main(monkeypatch, tmp_path, '--dotfiles', str(repo))
    run_main(monkeypatch, tmp_path)
    assert (tmp_path / '.zshrc').read_text() == 'mine\n'


def test_missing_installer_fails_before_touching_shell(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.write_text('original\n')
    (tmp_path / 'empty').mkdir()
    with pytest.raises(ValueError, match='install.sh'):
        run_installer(tmp_path / 'empty', None, [])
    assert rc.read_text() == 'original\n'


def test_missing_checkout_fails(tmp_path):
    with pytest.raises(ValueError, match='not a directory'):
        run_installer(tmp_path / 'nope', None, [])


def test_failing_installer_raises(tmp_path):
    repo = tmp_path / 'dotfiles'
    make_dotfiles(repo, '#!/bin/sh\nexit 3\n')
    with pytest.raises(Exception):
        run_installer(repo, None, [])


def test_roo_coder_flag_is_deprecated_noop(tmp_path, monkeypatch, capsys):
    run_main(monkeypatch, tmp_path, '--roo-coder')
    assert 'deprecated' in capsys.readouterr().err


def test_devbox_has_no_dotfiles_layout_knowledge():
    source = Path(home_setup.__file__).read_text()
    assert 'zsh/zsh' not in source
    assert 'claude/mcp-servers' not in source
    assert not hasattr(home_setup, 'setup_roo')
