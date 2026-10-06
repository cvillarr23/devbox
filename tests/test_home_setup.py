from pathlib import Path
from unittest.mock import patch

import pytest

from home_setup import setup, setup_roo


def test_existing_home_is_preserved(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.write_text('custom shell config\n')
    setup(tmp_path)
    assert rc.read_text() == 'custom shell config\n'


def test_explicit_dotfiles_backs_up_config_and_survives_restart(tmp_path):
    repo = tmp_path / 'dev/dotfiles'
    (repo / 'zsh').mkdir(parents=True)
    (repo / 'zsh/zshrc').write_text('plugins=(git)\n')
    rc = tmp_path / '.zshrc'
    rc.write_text('custom shell config\n')
    setup(tmp_path, str(repo))
    configured = rc.read_text()
    assert 'dotfiles-path' in configured
    assert 'command claude' in configured
    assert (tmp_path / '.config/devbox/dotfiles-path').read_text() == str(repo)
    assert next(tmp_path.glob('.zshrc.pre-devbox-*')).read_text() == 'custom shell config\n'
    setup(tmp_path)
    assert rc.read_text() == configured


def test_invalid_dotfiles_fails_before_replacing_shell(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.write_text('original\n')
    with pytest.raises(ValueError, match='zsh/zshrc'):
        setup(tmp_path, str(tmp_path / 'missing'))
    assert rc.read_text() == 'original\n'


def test_roo_requires_initialized_submodule(tmp_path):
    with pytest.raises(ValueError, match='submodule'):
        setup_roo(tmp_path)


def test_roo_preserves_existing_registrations(tmp_path):
    project = tmp_path / 'claude/mcp-servers/roo-coder'
    project.mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\n')
    with patch('home_setup.shutil.which', return_value='/bin/tool'), patch('home_setup.subprocess.run') as run:
        run.return_value.returncode = 0
        setup_roo(tmp_path)
    assert len(run.call_args_list) == 3
    assert run.call_args_list[0].args[0][:2] == ['uv', 'sync']
    assert all('add' not in call.args[0] for call in run.call_args_list)
