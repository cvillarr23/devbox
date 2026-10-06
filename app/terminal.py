#!/usr/bin/env python3
import json
import os
import sys
import subprocess
from pathlib import Path
import environments
import remote

entry = json.loads((Path(os.environ['DEVBOX_STATE_DIR']) / 'runtime.json').read_text())[sys.argv[1]]
if entry['target']['type'] == 'local':
    # Match remote helper's fixed config path without modifying mounted home.
    name = entry['name']
    os.chdir(entry['cwd'])
    os.environ.update(DEVBOX_SESSION=name, BROWSER='/opt/devbox/bin/devbox-open', SHELL='/bin/zsh')
    existing = subprocess.run(['zellij', 'list-sessions', '-s', '-n'], capture_output=True, text=True).stdout.splitlines()
    args = ['zellij', '--config', '/opt/devbox/config/zellij.kdl']
    args += ['attach', name] if name in existing else ['--new-session-with-layout', '/opt/devbox/config/layout.kdl', '--session', name]
    os.execvp('zellij', args)
else:
    command = environments.transport(entry['target'], environments.helper_command('attach', entry['name'], entry['cwd']), tty=True)
    os.execvp(command[0], command)
