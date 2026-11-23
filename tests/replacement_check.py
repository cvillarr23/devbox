#!/usr/bin/env python3
"""Check that persistent state survives a container/pod replacement and nothing replays.

Run `seed` against a disposable devbox, replace the container or pod (upgrade, rollback,
restore or a plain delete), then run `verify` against the new one. The token is read
internally and is never printed.
"""
import argparse
import asyncio
import json
import subprocess

import aiohttp

SESSION = 'replacement-check'
NOTE = 'replacement-check note'
MARKER = '$HOME/replacement-check/replayed'
PROJECT = '/workspace/replacement-check'
CHROME_MARKER = '.config/google-chrome/replacement-check'
RUNNER = '$HOME/replacement-check/run.sh'
# A script path survives Zellij's word-splitting of a serialized command, so a resurrected pane would really rerun it.
REPLAY_COMMAND = f'sh {RUNNER}'


def _exec_prefix(args):
    if args.namespace:
        return ['kubectl', '-n', args.namespace, 'exec', 'deployment/' + args.deployment, '--']
    return ['docker', 'exec', args.container]


def _token_path(args):
    return '/data/state/access-token' if args.namespace else '/data/access-token'


def _sh(args, script):
    return subprocess.run([*_exec_prefix(args), 'sh', '-c', script], check=True,
                          capture_output=True, text=True).stdout


async def _wait_ready(base):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as startup:
        for _ in range(240):
            try:
                async with startup.get(base + '/healthz') as response:
                    if response.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.25)
    raise RuntimeError('Devbox did not become ready')


class Api:
    def __init__(self, client, base):
        self.client, self.base = client, base

    async def __call__(self, method, path, data=None):
        async with self.client.request(method, self.base + path, json=data) as response:
            if response.status == 204:
                return None
            result = await response.json()
            if response.status >= 400:
                raise RuntimeError(f'{method} {path}: {result.get("error", response.status)}')
            return result

    async def type(self, port, keys, settle=1.0):
        """Send keystrokes to the session's terminal over the ttyd WebSocket."""
        async with self.client.ws_connect(self.base + f'/s/{port - 7690}/ws', protocols=['tty']) as ws:
            await ws.send_str(json.dumps({'AuthToken': '', 'columns': 100, 'rows': 30}))
            await asyncio.sleep(1.5)
            await ws.send_bytes(b'0' + keys)
            await asyncio.sleep(settle)


def _marker_count(args):
    return int(_sh(args, f'[ -f {MARKER} ] && wc -l < {MARKER} || echo 0').strip())


def _seed_files(args):
    """A project with an uncommitted change and a Chrome-profile file, both expected to persist."""
    _sh(args, f'mkdir -p {PROJECT} $(dirname {MARKER}) && cd {PROJECT} && git init -q && '
              'git -c user.name=check -c user.email=check@example.invalid commit -q --allow-empty -m seed && '
              f'echo uncommitted > change.txt && rm -f {MARKER} && '
              f'printf "echo ran >> {MARKER}; sleep 600\\n" > {RUNNER} && '
              f'mkdir -p "$HOME/.config/google-chrome" && echo profile > "$HOME/{CHROME_MARKER}"')


async def seed(args, api):
    _seed_files(args)
    entry = await api('POST', '/api/sessions', {'name': SESSION})
    await api('PUT', f'/api/sessions/{SESSION}/notes', {'content': NOTE + '\n'})
    await api('POST', '/api/browser', {})
    await api('DELETE', '/api/browser')
    await api.type(entry['port'], REPLAY_COMMAND.encode() + b'\r', settle=3)
    assert _marker_count(args) == 1, 'the seeded command did not run'
    # Zellij writes its session cache about once a minute; give it time so an image that
    # still serializes sessions has something to resurrect.
    await asyncio.sleep(args.serialize_wait)
    print('Seeded session, notes, project, Chrome profile and a running command: passed')


def _assert_files_persisted(args):
    status = _sh(args, f'cd {PROJECT} && git status --porcelain && git log --oneline | wc -l')
    assert '?? change.txt' in status and status.strip().endswith('1'), status
    assert _sh(args, f'cat "$HOME/{CHROME_MARKER}"').strip() == 'profile'


async def _assert_stopped(api):
    """After replacement the session is listed but has no terminal until an explicit reconnect."""
    entry = next(s for s in await api('GET', '/api/sessions') if s['name'] == SESSION)
    assert entry['connection'] in ('stopped', 'disconnected'), entry['connection']
    async with api.client.get(api.base + f'/s/{entry["port"] - 7690}/') as response:
        assert response.status == 503, response.status
    return entry


async def verify(args, api):
    _assert_files_persisted(args)
    notes = await api('GET', f'/api/sessions/{SESSION}/notes')
    assert NOTE in notes['content'], notes
    print('Notes, project with uncommitted change and Chrome profile persisted: passed')
    entry = await _assert_stopped(api)
    print('Replaced session waits for explicit reconnect: passed')
    await api('POST', f'/api/sessions/{SESSION}/reconnect', {})
    # Enter is what a user presses first in a fresh terminal; it must not start a resurrected command.
    await api.type(entry['port'], b'\r', settle=3)
    assert _marker_count(args) == 1, 'a previous shell command ran again after reconnect'
    print('Reconnect starts a new terminal without replaying commands: passed')
    if args.cleanup:
        await api('DELETE', f'/api/sessions/{SESSION}')
        _sh(args, f'rm -rf {PROJECT} $(dirname {MARKER}) "$HOME/{CHROME_MARKER}"')


async def main(args):
    base = args.url.rstrip('/')
    await _wait_ready(base)
    token = subprocess.check_output([*_exec_prefix(args), 'cat', _token_path(args)], text=True).strip()
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}) as client:
        await (seed if args.phase == 'seed' else verify)(args, Api(client, base))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['seed', 'verify'])
    parser.add_argument('--url', default='http://127.0.0.1:17680')
    parser.add_argument('--container', default='devbox-ci')
    parser.add_argument('--namespace')
    parser.add_argument('--deployment', default='devbox')
    parser.add_argument('--serialize-wait', type=float, default=70, help='seconds to wait after seeding')
    parser.add_argument('--cleanup', action='store_true', help='remove the check session and files after verify')
    asyncio.run(main(parser.parse_args()))
