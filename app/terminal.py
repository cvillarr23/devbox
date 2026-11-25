#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
import environments
import remote
import spawn

entry = json.loads((Path(os.environ['DEVBOX_STATE_DIR']) / 'runtime.json').read_text())[sys.argv[1]]
if entry['target']['type'] == 'local':
    # Match remote helper's fixed config path without modifying mounted home.
    name = entry['name']
    spawn.enter_cwd(entry['cwd'])
    os.environ.update(DEVBOX_SESSION=name, BROWSER='/opt/devbox/bin/devbox-open', SHELL='/bin/zsh')
    spawn.color_env(os.environ)
    args = spawn.terminal_command(name, '/opt/devbox/config/layout.kdl')
    os.execvp(args[0], args)
else:
    command = environments.transport(entry['target'], environments.helper_command('attach', entry['name'], entry['cwd']), tty=True)
    os.execvp(command[0], command)
