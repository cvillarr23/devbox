"""Pending OIDC logins that devbox's public callback replays to a session's loopback port."""
import os
import time
from urllib.parse import parse_qs, urlsplit

import devbox_store as store

CALLBACK_PATH = '/oidc/callback'
TTL_SECONDS = 300
MIN_PORT = 1024
SESSION_PORTS = range(7690, 7790)
BROWSER_PORT = 6080


class Unknown(Exception):
    """No pending login for this state, or it was already used."""


class Expired(Exception):
    """The pending login outlived its TTL."""


def _origin(url):
    """Scheme, host and port, with the scheme's default port made explicit."""
    parts = urlsplit(url)
    default = {'https': 443, 'http': 80}.get(parts.scheme)
    try:
        return parts.scheme, parts.hostname, parts.port or default
    except ValueError:
        return None


def _query_value(url, key):
    values = parse_qs(urlsplit(url).query).get(key, [])
    return values[0] if values else ''


class AuthRelay:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = {}

    def register(self, session, port, auth_url):
        """Record a login whose authorization URL redirects to this devbox's callback."""
        self._check_port(port)
        state = _query_value(auth_url, 'state')
        if not state:
            raise ValueError('authorization URL has no state')
        self._check_redirect(_query_value(auth_url, 'redirect_uri'))
        self._purge()
        self.pending = {s: e for s, e in self.pending.items() if (e['session'], e['port']) != (session, port)}
        self.pending[state] = {'session': session, 'port': port, 'expires': self.clock() + TTL_SECONDS}

    def claim(self, state):
        """Single use: the entry is removed whether or not it has expired."""
        entry = self.pending.pop(state, None)
        if entry is None:
            raise Unknown()
        if self.clock() >= entry['expires']:
            raise Expired()
        return entry['session'], entry['port']

    def _purge(self):
        now = self.clock()
        self.pending = {s: e for s, e in self.pending.items() if e['expires'] > now}

    @staticmethod
    def _check_port(port):
        if isinstance(port, bool) or not isinstance(port, int) or not MIN_PORT <= port <= 65535:
            raise ValueError(f'port must be an integer from {MIN_PORT} to 65535')
        reserved = {int(os.environ.get('DEVBOX_PORT', '7680')), BROWSER_PORT}
        if port in reserved or port in SESSION_PORTS:
            raise ValueError('port belongs to devbox itself')

    @staticmethod
    def _check_redirect(redirect_uri):
        base = store.public_base_url()
        if not base:
            raise ValueError('DEVBOX_PUBLIC_URL is not set; devbox has no public callback')
        parts = urlsplit(redirect_uri)
        if not redirect_uri or parts.path != CALLBACK_PATH or _origin(redirect_uri) != _origin(base):
            raise ValueError(f'redirect_uri must be {base}{CALLBACK_PATH}')
