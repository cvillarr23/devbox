"""OpenBao OIDC login owned by devbox: the dashboard starts it, /oidc/callback finishes it.

The token never leaves the server: it is written to ~/.vault-token (0600), the
file `bao` reads in every session, and the browser only sees a status.
"""
import asyncio
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp

from auth_relay import CALLBACK_PATH, Expired, Unknown

TTL_SECONDS = 300
MAX_PENDING = 16
TIMEOUT = aiohttp.ClientTimeout(total=10)
_NAME = re.compile(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*')


class BaoError(Exception):
    """OpenBao refused or could not be reached. The message is safe to show."""


def from_env():
    """A BaoLogin from DEVBOX_BAO_*, or None when DEVBOX_BAO_ADDR is unset (feature off)."""
    addr = os.environ.get('DEVBOX_BAO_ADDR', '').strip().rstrip('/')
    if not addr:
        return None
    if urlsplit(addr).scheme not in ('http', 'https') or not urlsplit(addr).hostname:
        raise ValueError('DEVBOX_BAO_ADDR must be an http(s) URL')
    role = os.environ.get('DEVBOX_BAO_ROLE', '').strip() or 'admin'
    mount = os.environ.get('DEVBOX_BAO_MOUNT', '').strip().strip('/') or 'oidc'
    for key, value in (('DEVBOX_BAO_ROLE', role), ('DEVBOX_BAO_MOUNT', mount)):
        if not _NAME.fullmatch(value) or '..' in value.split('/'):
            raise ValueError(f'{key} must be letters, digits, ".", "_", "-" and "/"')
    return BaoLogin(addr, role, mount)


def token_path():
    return Path.home() / '.vault-token'


def write_token(token):
    """Atomically replace ~/.vault-token, never readable by anyone but the owner."""
    path = token_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.vault-token.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _errors(body):
    """OpenBao's error text, trimmed; it never contains the code or a token."""
    errors = body.get('errors') if isinstance(body, dict) else None
    text = '; '.join(str(e) for e in errors) if isinstance(errors, list) else ''
    return text[:300] or 'no error message'


class BaoLogin:
    def __init__(self, addr, role, mount, clock=time.monotonic):
        self.addr, self.role, self.mount = addr, role, mount
        self.clock = clock
        self.pending = {}

    def _url(self, path):
        return f'{self.addr}/v1/{path}'

    async def _call(self, client, method, path, **kwargs):
        try:
            async with client.request(method, self._url(path), timeout=TIMEOUT, **kwargs) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise BaoError(f'OpenBao is unreachable ({type(exc).__name__})') from None

    async def start(self, client, redirect_uri):
        """Ask OpenBao for an authorization URL and remember its state for TTL_SECONDS."""
        nonce = secrets.token_urlsafe(32)
        status, body = await self._call(client, 'POST', f'auth/{self.mount}/oidc/auth_url', json={
            'role': self.role, 'redirect_uri': redirect_uri, 'client_nonce': nonce})
        if status != 200:
            raise BaoError(f'OpenBao refused the login ({status}: {_errors(body)})')
        url = ((body or {}).get('data') or {}).get('auth_url') or ''
        # OpenBao answers an unregistered redirect_uri with an empty auth_url, not an error.
        if not url:
            raise BaoError(f'OpenBao returned no sign-in URL; is {redirect_uri} an allowed redirect URI of role {self.role}?')
        state = (parse_qs(urlsplit(url).query).get('state') or [''])[0]
        if not state:
            raise BaoError('OpenBao returned a sign-in URL without a state')
        self._purge()
        while len(self.pending) >= MAX_PENDING:
            self.pending.pop(next(iter(self.pending)))
        self.pending[state] = {'nonce': nonce, 'expires': self.clock() + TTL_SECONDS}
        return url

    def owns(self, state):
        return bool(state) and state in self.pending

    def claim(self, state):
        """Single use: the entry is removed whether or not it has expired. Returns the client nonce."""
        entry = self.pending.pop(state, None) if state else None
        if entry is None:
            raise Unknown()
        if self.clock() >= entry['expires']:
            raise Expired()
        return entry['nonce']

    async def finish(self, client, state, nonce, query):
        """Exchange the callback with OpenBao and store the token. Returns its TTL in seconds."""
        params = {'state': state, 'client_nonce': nonce}
        for key in ('code', 'error', 'error_description'):
            if query.get(key):
                params[key] = query[key]
        status, body = await self._call(client, 'GET', f'auth/{self.mount}/oidc/callback', params=params)
        auth = (body or {}).get('auth') if isinstance(body, dict) else None
        token = auth.get('client_token') if isinstance(auth, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise BaoError(f'OpenBao rejected the login ({status}: {_errors(body)})')
        await asyncio.to_thread(write_token, token)
        return auth.get('lease_duration')

    async def status(self, client):
        """Who ~/.vault-token belongs to and how long it lasts. Never includes the token."""
        try:
            token = (await asyncio.to_thread(token_path().read_text)).strip()
        except FileNotFoundError:
            token = ''
        if not token:
            return {'enabled': True, 'logged_in': False}
        status, body = await self._call(client, 'GET', 'auth/token/lookup-self', headers={'X-Vault-Token': token})
        if status in (401, 403):
            return {'enabled': True, 'logged_in': False}
        if status != 200:
            raise BaoError(f'OpenBao token lookup failed ({status})')
        data = (body or {}).get('data') or {}
        ttl = data.get('ttl')
        return {'enabled': True, 'logged_in': True,
                'expires_in': ttl if isinstance(ttl, int) and ttl > 0 else None,
                'display_name': str(data.get('display_name') or '')[:120]}

    def _purge(self):
        # Expired entries linger one more TTL so a late click reports "expired", not "unknown".
        now = self.clock()
        self.pending = {s: e for s, e in self.pending.items() if e['expires'] + TTL_SECONDS > now}


def redirect_uri(base):
    return f'{base}{CALLBACK_PATH}'
