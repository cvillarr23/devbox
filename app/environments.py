"""Validated deployment targets and argument-only remote transports."""
import asyncio
import json
import os
import re
import shlex
from pathlib import Path

import yaml

NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
HELPER = '.local/share/devbox/remote.py'


def load(path=None):
    path = path or os.environ.get('DEVBOX_ENVIRONMENTS', '/etc/devbox/environments.yaml')
    raw = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
    targets = (raw or {}).get('environments', {})
    if not isinstance(targets, dict):
        raise ValueError('environments must be a mapping')
    result = {'local': {'type': 'local'}}
    for name, config in targets.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(config, dict):
            raise ValueError('invalid environment name or configuration')
        kind = config.get('type')
        required = {'local': (), 'ssh': ('host',), 'kubernetes': ('context', 'namespace', 'selector', 'container')}
        if kind not in required:
            raise ValueError(f'{name}: unknown environment type')
        for key in required[kind]:
            value = config.get(key)
            if not isinstance(value, str) or not value or value.startswith('-') or '\n' in value or '\0' in value:
                raise ValueError(f'{name}: invalid {key}')
        result[name] = dict(config)
    return result


async def run(argv, data=None, timeout=20):
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(err.decode(errors='replace')[-2000:] or f'{argv[0]} exited {proc.returncode}')
    return out


async def resolve(config):
    config = dict(config)
    if config['type'] == 'kubernetes':
        out = await run(['kubectl', '--context', config['context'], '-n', config['namespace'],
                         'get', 'pods', '-l', config['selector'], '-o', 'json'])
        pods = [p for p in json.loads(out)['items'] if not p['metadata'].get('deletionTimestamp')
                and p.get('status', {}).get('phase') == 'Running'
                and any(c.get('name') == config['container'] and c.get('ready')
                        for c in p.get('status', {}).get('containerStatuses', []))]
        if len(pods) != 1:
            raise ValueError(f'Expected one ready target pod, found {len(pods)}; narrow the selector')
        config.update(pod=pods[0]['metadata']['name'], pod_uid=pods[0]['metadata']['uid'])
    return config


def transport(config, command, tty=False):
    if config['type'] == 'ssh':
        return ['ssh', '-tt' if tty else '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                config['host'], shlex.join(command)]
    if config['type'] == 'kubernetes':
        return ['kubectl', '--context', config['context'], '-n', config['namespace'],
                'exec', '-it' if tty else '-i', config['pod'], '-c', config['container'], '--', *command]
    return command


def helper_command(*args):
    # The fixed shell fragment expands HOME remotely, independent of remote cwd.
    return ['sh', '-c', 'exec python3 "$HOME/.local/share/devbox/remote.py" "$@"', 'devbox', *args]
