"""Single-user session lifecycle, remote control, and explicit TCP forwards."""
import asyncio
import contextlib
import functools
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import devbox_store as store
import environments
import spawn as child_session
import auth_relay
import message_router
import messages
import pr_watcher
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

    async def call(self, data, timeout=15):
        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                raise RuntimeError('Target control connection is offline; reconnect explicitly')
            self.proc.stdin.write(json.dumps(data).encode() + b'\n')
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
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
        self._deferred = set()
        # Connected when the last server stopped (or died): recreated by restore().
        self.to_restore = [n for n, e in self.sessions.items()
                           if e.get('restore') or e.get('connection') == 'connected']
        for entry in self.sessions.values():
            entry['connection'] = 'stopped'
            entry['restore'] = False
        self.children = {}
        self.remotes = {}
        self.remote_states = {}
        self.responses = {}
        self.forwards = {}
        self.lock = asyncio.Lock()
        self.browser = Browser()
        self.auth_relay = auth_relay.AuthRelay()
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

    async def restore(self):
        """Bring back every local session that was connected before the restart.

        The zellij session is created here (resuming the last conversation when
        there is one), so no dashboard click or open page is needed. Remote targets
        stay stopped; their Reconnect needs the control connection.
        """
        for name in self.to_restore:
            entry = self.sessions.get(name)
            if not entry or entry['target']['type'] != 'local':
                continue
            try:
                cwd = str(child_session.existing_dir(entry['cwd'])[1])  # a removed worktree must not block restore
                await self.ensure(name, entry['environment'], cwd)
                await asyncio.to_thread(child_session.restore_session, name, cwd)
            except Exception as exc:
                print('Could not restore session %s: %s' % (name, exc), flush=True)
        self.to_restore = []

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

    def _finished_children(self, caller):
        """Live children of CALLER that are done or idle with no unread urgent messages."""
        return [c for c in store.children(caller) if c in self.sessions
                and self.status(c)['state'] in ('done', 'idle')
                and not (self.sessions[c]['target']['type'] == 'local' and messages.pending(c, None, urgent_only=True))]

    async def kill_sessions(self, caller, args):
        """End sessions on behalf of CALLER. The restrictions live here, where the
        caller is known: ending yourself needs `self`; `children_only` (the MCP
        tool) limits targets to the caller's own children and itself.
        With `dry_run` nothing is ended and the resolved list is returned."""
        names = list(dict.fromkeys(args.get('names') or []))
        if args.get('children'):
            names += [c for c in self._finished_children(caller) if c not in names]
        if not names:
            raise ValueError('Name a session to end, or use --children with finished children')
        own = set(store.children(caller))
        for target in names:
            if target not in self.sessions:
                raise ValueError(f'Unknown session: {target}')
            if target == caller:
                if not args.get('self'):
                    raise ValueError(f'{target} is the calling session; pass --self to end it')
            elif args.get('children_only') and target not in own:
                raise ValueError(f'{target} is not a child of {caller}; you can only end your own children and yourself')
        if args.get('dry_run'):
            return {'would_end': names}
        ended = []
        for target in [n for n in names if n != caller]:
            await self.kill(target)
            ended.append(target)
        if caller in names:
            # reply first: ending the caller also ends the agent waiting on this answer
            loop = asyncio.get_running_loop()
            loop.call_later(args.get('delay', 1), self._end_later, caller)
            ended.append(caller)
        return {'ended': ended}

    def _end_later(self, name):
        task = asyncio.get_running_loop().create_task(self.kill(name))
        self._deferred.add(task)
        task.add_done_callback(self._deferred.discard)

    def status(self, name):
        entry = self.sessions[name]
        proc = self.children.get(name)
        if proc and proc.returncode is not None:
            entry['connection'] = 'disconnected'
        if entry['target']['type'] == 'local':
            result = {**store.session_status(name), 'unread': messages.unread_count(name)}
        else:
            result = self.remote_states.get(name, {'state': 'unavailable', 'agents': []})
            result = {**result, 'unread': result.get('unread', 0) + message_router.queued_count(name)}
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

    async def spawn(self, parent, args):
        """Start a child session for `parent` (None for a top-level spawn)."""
        spec = child_session.plan(args)
        link = self._parent_link(parent, args.get('pane'))
        self._check_spawn_limit(parent)
        spec['environment'] = spec['environment'] if args.get('environment') else self._default_environment(parent)
        name = spec['name'] or child_session.child_name(parent, set(self.sessions))
        if name in self.sessions:
            raise ValueError('Session already exists')
        worktree = None
        try:
            cwd, worktree = await self._spawn_cwd(parent, spec, args.get('origin_cwd', ''))
            await self.ensure(name, spec['environment'], cwd)
            if link:
                store.update_meta(name, {'parent': link})
            await self._start_agent(name, spec, child_session.child_prompt(spec['prompt'], link))
        except BaseException:
            await self._discard_spawn(name, parent, worktree)
            raise
        return {'name': name, 'url': self._session_url(name), 'parent': link,
                'cwd': self.sessions[name]['cwd'], 'environment': spec['environment']}

    @staticmethod
    def _parent_link(parent, pane):
        if not parent:
            return ''
        return parent + (':' + str(pane) if str(pane or '').isdigit() else '')

    def _check_spawn_limit(self, parent):
        limit = int(os.environ.get('DEVBOX_SPAWN_LIMIT', '5'))
        live = [c for c in store.children(parent) if c in self.sessions] if parent else []
        if parent and limit > 0 and len(live) >= limit:
            raise ValueError('%s already has %d child sessions (DEVBOX_SPAWN_LIMIT=%d); end one first' % (parent, len(live), limit))

    def _default_environment(self, parent):
        return self.sessions[parent]['environment'] if parent in self.sessions else 'local'

    async def _spawn_cwd(self, parent, spec, origin_cwd):
        """Resolve the child's directory; returns (cwd, created worktree path or None)."""
        if not spec['worktree']:
            return spec['cwd'], None
        if not parent or self.sessions[parent]['environment'] != spec['environment']:
            raise ValueError('--worktree needs the child in the same environment as the session that spawns it')
        if self.sessions[parent]['target']['type'] != 'local' and parent not in self.remotes:
            raise ValueError('Reconnect the target before spawning into it')
        origin = origin_cwd or self.sessions[parent]['cwd']
        if self.sessions[parent]['target']['type'] == 'local':
            path = await asyncio.to_thread(child_session.create_worktree, origin, spec['worktree'])
        else:
            path = (await self.remotes[parent].call({'operation': 'worktree', 'cwd': origin, 'branch': spec['worktree']}))['path']
        return path, path

    async def _start_agent(self, name, spec, prompt):
        entry = self.sessions[name]
        if entry['target']['type'] == 'local':
            await asyncio.to_thread(child_session.start_agent, name, entry['cwd'], spec, prompt)
        else:
            await self.remotes[name].call({'operation': 'spawn', 'session': name, 'cwd': entry['cwd'],
                                           'spec': spec, 'prompt': prompt}, timeout=30)

    async def _discard_spawn(self, name, parent, worktree):
        """Undo a half-made spawn: the session if it got far enough, and the worktree we created."""
        if name in self.sessions:
            with contextlib.suppress(Exception):
                await self.kill(name)
        if worktree and parent in self.sessions and self.sessions[parent]['target']['type'] == 'local':
            await asyncio.to_thread(child_session.remove_worktree, self.sessions[parent]['cwd'], worktree)

    @staticmethod
    def _session_url(name):
        return store.public_view_url(name) or 'http://localhost:' + os.environ.get('DEVBOX_PORT', '7680') + '/view/' + name

    async def operation(self, name, operation, args):
        if name not in self.sessions:
            raise ValueError('Unknown session')
        if operation == 'doctor':
            report = await self.remotes[name].call({'operation': 'doctor'}) if name in self.remotes else {}
            return {**report, 'session': name, 'environment': self.sessions[name]['environment'], **self.status(name)}
        if operation == 'open':
            return await self.browser.open(name, args['url'], args.get('target', os.environ.get('DEVBOX_OPEN_TARGET', 'remote')))
        if operation == 'forward':
            return await self.forward(name, args['port'])
        if operation == 'auth-register':
            self.auth_relay.register(name, args.get('port'), args.get('url'))
            return {'ok': True}
        if operation == 'spawn':
            return await self.spawn(name, args)
        if operation == 'kill':
            return await self.kill_sessions(name, args)
        if operation == 'msg-send':
            return await message_router.send(self, name, args)
        if operation == 'msg-config':
            return message_router.config(self, name, args)
        if operation == 'msg-who':
            return message_router.who(self, name, args)
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

    def _nudge_local(self):
        """Nudge idle local agents about urgent unread messages (the remote
        helper does the same for its sessions inside each snapshot)."""
        for name, entry in list(self.sessions.items()):
            if entry['target']['type'] == 'local':
                messages.nudge_due(name, functools.partial(store.session_status, name))

    async def poll(self):
        ticks = 0
        while True:
            ticks += 1
            if ticks % 5 == 0:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._nudge_local)
            if ticks % pr_watcher.CYCLE_TICKS == 0:
                with contextlib.suppress(Exception):
                    await pr_watcher.cycle(self)
            for name, remote in list(self.remotes.items()):
                if self.sessions[name]['connection'] != 'connected':
                    continue
                try:
                    await message_router.flush_outbox(self, name)
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

    async def loopback_port(self, name, port):
        """A port on devbox's own loopback that reaches `port` in the session's environment."""
        if self.sessions[name]['target']['type'] == 'local':
            return port
        return (await self.forward(name, port))['port']

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
        for entry in self.sessions.values():
            entry['restore'] = entry['connection'] == 'connected'
        for name in list(self.children):
            await self.disconnect(name)
