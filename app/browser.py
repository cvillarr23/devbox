"""On-demand browser processes and acknowledged local-viewer link requests."""
import asyncio
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError('invalid URL')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials are supported')
    return url


class Browser:
    def __init__(self):
        self.processes = []
        self.lock = asyncio.Lock()
        self.requests = {}
        self.viewers = {}
        self.last_open = {}
        self.env = {**os.environ, 'DISPLAY': ':99'}

    def chrome_command(self):
        command = [os.environ.get('DEVBOX_CHROME', 'google-chrome'), '--no-first-run',
                   '--no-default-browser-check', '--password-store=basic',
                   '--user-data-dir=' + str(Path.home() / '.config/devbox-chrome')]
        # Default Docker seccomp prevents Chrome from creating its own namespaces.
        # The browser shares the trusted single-user container; deployments with
        # compatible namespace policy can opt into Chrome's internal sandbox.
        if os.environ.get('DEVBOX_CHROME_SANDBOX', '0') != '1':
            command.append('--no-sandbox')
        return command

    async def start(self):
        async with self.lock:
            if self.processes and all(p.returncode is None for p in self.processes):
                return
            await self._stop()
            commands = [
                ['Xvfb', ':99', '-screen', '0', '1440x900x24', '-nolisten', 'tcp'],
                ['openbox'],
                ['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900', '-forever', '-shared', '-nopw'],
                ['websockify', '--web', '/usr/share/novnc', '127.0.0.1:6080', '127.0.0.1:5900'],
                [*self.chrome_command(), 'about:blank'],
            ]
            try:
                for cmd in commands:
                    proc = await asyncio.create_subprocess_exec(*cmd, env=self.env,
                                                               stdout=asyncio.subprocess.DEVNULL)
                    self.processes.append(proc)
                    await asyncio.sleep(.4)
                    if proc.returncode is not None:
                        raise RuntimeError(f'{cmd[0]} failed to start')
            except BaseException:
                await self._stop()
                raise

    async def _stop(self):
        for proc in reversed(self.processes):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 4)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        self.processes.clear()

    async def stop(self):
        async with self.lock:
            await self._stop()

    def status(self):
        return {'running': bool(self.processes) and all(p.returncode is None for p in self.processes)}

    async def open(self, session, url, target):
        validate_url(url)
        if target not in ('local', 'remote'):
            raise ValueError('target must be local or remote')
        if target == 'remote':
            await self.start()
            proc = await asyncio.create_subprocess_exec(*self.chrome_command(), '--new-tab', url,
                env=self.env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 10)
            if proc.returncode:
                raise RuntimeError('Chrome could not open the URL')
            self.last_open[session] = time.time()
            return {'status': 'opened', 'target': target}
        now = time.time()
        self.requests = {k: v for k, v in self.requests.items() if now - v['created_at'] < 3600}
        if len(self.requests) >= 500:
            raise ValueError('Too many pending links; dismiss links before adding more')
        ident = uuid.uuid4().hex
        request = {'id': ident, 'session': session, 'url': url, 'created_at': now,
                   'status': 'pending', 'owner': None, 'lease_until': 0}
        self.requests[ident] = request
        return {'id': ident, 'status': 'pending', 'target': target,
                'message': 'Queued for the session viewer; opening requires browser acknowledgment'}

    def pending(self, session, viewer, active):
        now = time.time()
        if active:
            self.viewers[session] = (viewer, now)
        items = []
        for item in self.requests.values():
            if item['session'] != session or item['status'] in ('opened', 'dismissed'):
                continue
            items.append({**item, 'can_claim': active and (item['owner'] in (None, viewer) or item['lease_until'] < now)})
        return {'links': items, 'browser': self.status(), 'last_remote_open': self.last_open.get(session, 0)}

    def claim(self, session, ident, viewer):
        item = self.requests.get(ident)
        if not item or item['session'] != session:
            raise ValueError('Unknown link request')
        if item['status'] in ('opened', 'dismissed') or (item['status'] == 'claimed' and item['lease_until'] > time.time()) or (item['owner'] not in (None, viewer) and item['lease_until'] > time.time()):
            return {'claimed': False}
        item.update(owner=viewer, lease_until=time.time() + 30, status='claimed')
        return {'claimed': True, 'url': item['url']}

    def ack(self, session, ident, viewer, state):
        item = self.requests.get(ident)
        if not item or item['session'] != session or item['owner'] != viewer:
            raise ValueError('Link is not claimed by this viewer')
        if state not in ('opened', 'blocked', 'dismissed'):
            raise ValueError('Invalid link acknowledgment')
        item.update(status=state)
        return {'status': state}
