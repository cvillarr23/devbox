"""Single-user session lifecycle, remote control, and explicit TCP forwards."""
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import devbox_store as store
import environments
from browser import Browser

ROOT = Path(__file__).resolve().parent


class Remote:
    def __init__(self, config):
        self.config = config
        self.proc = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.proc = await asyncio.create_subprocess_exec(
            *environments.transport(self.config, environments.helper_command('rpc')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=2_000_000)
        await self.call({'operation': 'hello'})

    async def call(self, data):
        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                raise RuntimeError('Target control connection is offline; reconnect explicitly')
            self.proc.stdin.write(json.dumps(data).encode() + b'\n')
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), 15)
            if not line:
                raise RuntimeError('Target control connection closed')
            result = json.loads(line)
            if 'error' in result:
                raise ValueError(result['error'])
            return result

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 3)
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()


class Runtime:
    def __init__(self):
        self.targets = environments.load()
        self.path = Path(store.state_dir()) / 'runtime.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions = json.loads(self.path.read_text()) if self.path.exists() else {}
        for entry in self.sessions.values():
            entry['connection'] = 'stopped'
        self.children = {}
        self.remotes = {}
        self.remote_states = {}
        self.responses = {}
        self.forwards = {}
        self.lock = asyncio.Lock()
        self.browser = Browser()
        self.save()

    def save(self):
        store._atomic_write(str(self.path), json.dumps(self.sessions, indent=2))
        store._atomic_write(str(self.path.parent / 'sessions.json'),
                            json.dumps({n: v['port'] for n, v in self.sessions.items()}))

    async def ensure(self, name, target='local', cwd=''):
        if not store.valid_name(name) or target not in self.targets:
            raise ValueError('Invalid session or environment')
        async with self.lock:
            old = self.sessions.get(name)
            if old and self.children.get(name) and self.children[name].returncode is None:
                if old['connection'] == 'connected':
                    return old
                raise ValueError('Disconnect the existing terminal before reconnecting')
            config = await environments.resolve(self.targets[target])
            if config['type'] == 'local':
                cwd = cwd or os.environ.get('DEVBOX_WORKSPACE', '/workspace')
                if not os.path.isabs(cwd) or not os.path.isdir(cwd):
                    raise ValueError('Working directory does not exist')
            else:
                remote = Remote(config)
                try:
                    await remote.connect()
                    cwd = (await remote.call({'operation': 'check', 'cwd': cwd}))['cwd']
                except BaseException:
                    await remote.close()
                    raise
                previous = self.remotes.pop(name, None)
                if previous:
                    await previous.close()
                self.remotes[name] = remote
            used = {v['port'] for n, v in self.sessions.items() if n != name}
            port = old['port'] if old else next((p for p in range(7690, 7790) if p not in used), None)
            if port is None:
                raise ValueError('Session capacity reached')
            entry = {'name': name, 'environment': target, 'cwd': cwd, 'port': port,
                     'target': config, 'connection': 'starting', 'started_at': time.time()}
            self.sessions[name] = entry
            self.save()
            command = [os.environ.get('DEVBOX_TTYD', 'ttyd'), '-W', '-i', '127.0.0.1', '-p', str(port),
                       '-b', f'/s/{port - 7690}', sys.executable, str(ROOT / 'terminal.py'), name]
            try:
                proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
                self.children[name] = proc
                for _ in range(50):
                    if proc.returncode is not None:
                        raise RuntimeError('Terminal process failed to start')
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close()
                        await writer.wait_closed()
                        entry['connection'] = 'connected'
                        self.save()
                        return entry
                    except OSError:
                        await asyncio.sleep(.1)
                raise RuntimeError('Terminal startup timed out')
            except BaseException:
                await self.disconnect(name)
                entry['connection'] = 'failed'
                self.save()
                raise

    async def close_forwards(self, name):
        for ident, item in list(self.forwards.items()):
            if item['session'] == name:
                item['server'].close()
                await item['server'].wait_closed()
                for task in list(item['tasks']):
                    task.cancel()
                await asyncio.gather(*item['tasks'], return_exceptions=True)
                del self.forwards[ident]

    async def disconnect(self, name):
        proc = self.children.pop(name, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        remote = self.remotes.pop(name, None)
        if remote:
            await remote.close()
        await self.close_forwards(name)
        self.sessions[name]['connection'] = 'disconnected'
        self.save()

    async def kill(self, name):
        entry = self.sessions[name]
        if entry['target']['type'] == 'local':
            with contextlib.suppress(RuntimeError):
                await environments.run(['zellij', 'delete-session', '-f', name])
        else:
            remote = self.remotes.get(name)
            if not remote:
                raise ValueError('Reconnect the target before ending its remote session')
            await remote.call({'operation': 'kill', 'session': name})
        await self.disconnect(name)
        self.sessions.pop(name)
        folder = Path(store.session_dir(name))
        if folder.exists():
            archive = folder.parent / '.archive'
            archive.mkdir(exist_ok=True)
            folder.rename(archive / f'{name}-{time.time_ns()}')
        self.save()

    def status(self, name):
        entry = self.sessions[name]
        proc = self.children.get(name)
        if proc and proc.returncode is not None:
            entry['connection'] = 'disconnected'
        if entry['target']['type'] == 'local':
            result = store.session_status(name)
        else:
            result = self.remote_states.get(name, {'state': 'unavailable', 'agents': []})
        return {**result, 'connection': entry['connection']}

    async def ack(self, name):
        if self.sessions[name]['target']['type'] == 'local':
            store.ack_done(name)
        elif name in self.remotes:
            await self.remotes[name].call({'operation': 'ack', 'session': name})
            for agent in self.remote_states.get(name, {}).get('agents', []):
                if agent['state'] == 'done':
                    agent['acked'] = True
            self.remote_states[name]['state'] = 'idle' if all(a.get('acked') or a['state'] == 'idle'
                for a in self.remote_states[name]['agents']) else self.remote_states[name]['state']
        return self.status(name)

    async def operation(self, name, operation, args):
        if name not in self.sessions:
            raise ValueError('Unknown session')
        if operation == 'open':
            return await self.browser.open(name, args['url'], args.get('target', os.environ.get('DEVBOX_OPEN_TARGET', 'remote')))
        if operation == 'forward':
            return await self.forward(name, args['port'])
        if operation == 'notes':
            action = args['action']
            if action == 'cat':
                content, version = store.read_notes(name)
                return {'content': content, 'version': version}
            if action == 'append':
                store.append_notes(name, args['content'])
            elif action == 'write':
                store.write_notes(name, args['content'])
            else:
                raise ValueError('Unsupported remote notes action')
            return {'ok': True}
        raise ValueError('Unknown operation')

    async def poll(self):
        while True:
            for name, remote in list(self.remotes.items()):
                if self.sessions[name]['connection'] != 'connected':
                    continue
                try:
                    result = await remote.call({'operation': 'snapshot', 'sessions': [name],
                                                'responses': self.responses.pop(name, {})})
                    self.remote_states[name] = result['states'][name]
                    responses = {}
                    for request in result['requests']:
                        if request['session'] != name:
                            continue
                        try:
                            responses[request['id']] = await self.operation(name, request['operation'], request['args'])
                        except Exception as exc:
                            responses[request['id']] = {'error': str(exc)}
                    self.responses[name] = responses
                except Exception:
                    self.sessions[name]['connection'] = 'disconnected'
                    self.remote_states[name] = {'state': 'unavailable', 'agents': []}
                    await remote.close()
                    await self.close_forwards(name)
                    self.save()
            await asyncio.sleep(1)

    async def forward(self, name, port):
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError('Port must be between 1 and 65535')
        entry = self.sessions[name]
        if entry['connection'] != 'connected':
            raise ValueError('Session is disconnected')
        if entry['target']['type'] == 'local':
            return {'url': f'http://127.0.0.1:{port}', 'port': port, 'target': 'remote'}
        ident = f'{name}:{port}'
        if ident not in self.forwards:
            tasks = set()
            async def connect(reader, writer):
                task = asyncio.current_task()
                tasks.add(task)
                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(*environments.transport(entry['target'],
                        environments.helper_command('forward', str(port))), stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    async def pipe(source, dest):
                        while chunk := await source.read(65536):
                            dest.write(chunk)
                            await dest.drain()
                        if dest.can_write_eof():
                            dest.write_eof()
                    await asyncio.wait_for(asyncio.gather(pipe(reader, proc.stdin), pipe(proc.stdout, writer)), 300)
                except (OSError, asyncio.TimeoutError, ConnectionError):
                    pass
                finally:
                    tasks.discard(task)
                    writer.close()
                    if proc and proc.returncode is None:
                        proc.terminate()
                        await proc.wait()
            server = await asyncio.start_server(connect, '127.0.0.1', 0)
            self.forwards[ident] = {'session': name, 'server': server, 'tasks': tasks, 'port': server.sockets[0].getsockname()[1]}
        return {'url': f'http://127.0.0.1:{self.forwards[ident]["port"]}',
                'port': self.forwards[ident]['port'], 'target': 'remote'}

    async def close(self):
        await self.browser.stop()
        for name in list(self.children):
            await self.disconnect(name)
