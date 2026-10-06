"""Initialize a persistent shell home without importing host credentials."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time

MARKER = '# Managed by devbox home setup\n'


def write_config(path, content, replace=False):
    if path.exists() or path.is_symlink():
        if path.is_file() and path.read_text().startswith(MARKER):
            if not replace:
                return
        elif not replace:
            return
        else:
            path.rename(path.with_name(path.name + f'.pre-devbox-{time.time_ns()}'))
    path.write_text(MARKER + content)


def setup(home, dotfiles=None, omz=Path('/opt/oh-my-zsh')):
    home.mkdir(parents=True, exist_ok=True)
    cache = home / '.cache/oh-my-zsh'
    cache.mkdir(parents=True, exist_ok=True)
    if not (home / '.oh-my-zsh').exists() and not (home / '.oh-my-zsh').is_symlink():
        (home / '.oh-my-zsh').symlink_to(omz)
    repo = Path(dotfiles).resolve() if dotfiles else home / 'dev/dotfiles'
    use_dotfiles = (repo / 'zsh/zshrc').is_file()
    if dotfiles and not use_dotfiles:
        raise ValueError('Dotfiles directory must contain zsh/zshrc')
    # Keep this stable across restarts; updates to the mounted repo are sourced.
    if use_dotfiles:
        path_file = home / '.config/devbox/dotfiles-path'
        path_file.parent.mkdir(parents=True, exist_ok=True)
        path_file.write_text(str(repo))
    prelude = '''export ZSH="$HOME/.oh-my-zsh"
export ZSH_CACHE_DIR="$HOME/.cache/oh-my-zsh"
zstyle ':omz:update' mode disabled
'''
    dotfile_source = '''if [[ -r "$HOME/.config/devbox/dotfiles-path" ]]; then
  _devbox_dotfiles=$(<"$HOME/.config/devbox/dotfiles-path")
  source "$_devbox_dotfiles/zsh/zshrc"
  unset _devbox_dotfiles
else
  ZSH_THEME=robbyrussell
  plugins=(git)
  source "$ZSH/oh-my-zsh.sh"
fi
# The dotfiles' native Claude launcher needs a fallback for the npm image CLI.
if [[ ! -d "$HOME/.local/share/claude/versions" ]]; then
  claude() { command claude "$@"; }
fi
# Inside devbox, this command controls this instance rather than the host.
devbox() { command devbox "$@"; }
'''
    write_config(home / '.zshrc', prelude + dotfile_source, replace=bool(dotfiles))
    write_config(home / '.zshenv', '''if [[ -r "$HOME/.config/devbox/dotfiles-path" ]]; then
  _devbox_dotfiles=$(<"$HOME/.config/devbox/dotfiles-path")
  [[ -r "$_devbox_dotfiles/zsh/zshenv" ]] && source "$_devbox_dotfiles/zsh/zshenv"
  unset _devbox_dotfiles
fi
export SHELL=/bin/zsh
export PATH="/opt/devbox/bin:/opt/venv/bin:$PATH"
''', replace=bool(dotfiles))
    return repo


def setup_roo(repo):
    project = repo / 'claude/mcp-servers/roo-coder'
    if not (project / 'pyproject.toml').is_file():
        raise ValueError('Initialize the Roo Coder submodule in the supplied dotfiles first')
    if not shutil.which('uv'):
        raise ValueError('Roo Coder setup requires uv')
    subprocess.run(['uv', 'sync', '--project', str(project), '--python', '3.12',
                    '--frozen', '--no-dev'], check=True)
    command = ['uv', 'run', '--frozen', '--no-dev', '--project', str(project), 'roo-coder']
    for agent in ('claude', 'codex'):
        if not shutil.which(agent):
            continue
        exists = subprocess.run([agent, 'mcp', 'get', 'roo-coder'], capture_output=True)
        if exists.returncode == 0:
            print(f'{agent}: preserving existing roo-coder registration')
            continue
        args = [agent, 'mcp', 'add']
        if agent == 'claude':
            args += ['--scope', 'user']
        subprocess.run([*args, 'roo-coder', '--', *command], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dotfiles', help='Runtime-only checkout; existing shell files are backed up')
    parser.add_argument('--roo-coder', action='store_true', help='Install and register the supplied Roo Coder submodule')
    args = parser.parse_args()
    repo = setup(Path.home(), args.dotfiles or os.environ.get('DEVBOX_DOTFILES_DIR'))
    if args.roo_coder:
        setup_roo(repo)


if __name__ == '__main__':
    main()
