"""Initialize a persistent shell home without importing host credentials."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from bootstrap import ensure_remote_control, remote_control_enabled

MARKER = '# Managed by devbox home setup\n'

ZSHRC = '''export ZSH="$HOME/.oh-my-zsh"
export ZSH_CACHE_DIR="$HOME/.cache/oh-my-zsh"
zstyle ':omz:update' mode disabled
ZSH_THEME=robbyrussell
plugins=(git)
source "$ZSH/oh-my-zsh.sh"
# Keep the image's npm Claude launcher working.
claude() { command claude "$@"; }
# Inside devbox, this command controls this instance rather than the host.
devbox() { command devbox "$@"; }
'''
ZSHENV = '''export SHELL=/bin/zsh
export PATH="/opt/devbox/bin:/opt/venv/bin:$PATH"
'''


def write_config(path, content):
    """Write a managed file; refresh our own earlier copy, never a user's file."""
    if path.exists() or path.is_symlink():
        if not (path.is_file() and path.read_text().startswith(MARKER)):
            return
    path.write_text(MARKER + content)


def setup(home, omz=Path('/opt/oh-my-zsh')):
    """Seed a dotfiles-agnostic shell home; never reads any dotfiles checkout."""
    home.mkdir(parents=True, exist_ok=True)
    (home / '.cache/oh-my-zsh').mkdir(parents=True, exist_ok=True)
    omz_link = home / '.oh-my-zsh'
    if not omz_link.exists() and not omz_link.is_symlink():
        omz_link.symlink_to(omz)
    write_config(home / '.zshrc', ZSHRC)
    write_config(home / '.zshenv', ZSHENV)
    ensure_remote_control(home / '.claude/settings.json', remote_control_enabled())


def _resolve_installer(checkout, installer):
    """Return the executable to run, failing before any shell file is touched."""
    checkout = Path(checkout).resolve()
    if not checkout.is_dir():
        raise ValueError(f'Dotfiles path is not a directory: {checkout}')
    script = checkout / (installer or 'install.sh')
    if not script.is_file() or not os.access(script, os.X_OK):
        raise ValueError(f'Dotfiles installer not found or not executable: {script} (expected install.sh)')
    return checkout, script


def _backup_shell_files(home):
    """Copy user-owned shell files aside, since the installer may replace them."""
    for name in ('.zshrc', '.zshenv'):
        path = home / name
        if not path.is_file() or path.read_text().startswith(MARKER):
            continue
        text = path.read_text()
        if any(b.read_text() == text for b in home.glob(f'{name}.pre-devbox-*')):
            continue
        shutil.copy2(path, path.with_name(f'{name}.pre-devbox-{time.time_ns()}'))


def run_installer(checkout, installer, args):
    """Hand off to the checkout's own installer as the current user."""
    checkout, script = _resolve_installer(checkout, installer)
    subprocess.run([str(script), *args], cwd=checkout, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dotfiles', help='Checkout whose own installer is run after shell seeding')
    parser.add_argument('--dotfiles-installer', help='Installer path relative to the checkout (default: install.sh)')
    parser.add_argument('--dotfiles-arg', action='append', default=[], metavar='ARG',
                        help='Argument forwarded to the installer; repeatable')
    parser.add_argument('--roo-coder', action='store_true', help='Deprecated no-op; the dotfiles installer handles Roo Coder')
    args = parser.parse_args()
    if args.roo_coder:
        print('warning: --roo-coder is deprecated and does nothing; '
              'the dotfiles installer installs Roo Coder. It will be removed in the next release.',
              file=sys.stderr)
    if args.dotfiles:
        _resolve_installer(args.dotfiles, args.dotfiles_installer)
    home = Path.home()
    setup(home)
    if args.dotfiles:
        _backup_shell_files(home)
        run_installer(args.dotfiles, args.dotfiles_installer, args.dotfiles_arg)


if __name__ == '__main__':
    main()
