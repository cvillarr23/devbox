"""Cloudflare Access JWT verification for `DEVBOX_AUTH_MODE=cloudflare-access`."""
import asyncio
import os
import time
from dataclasses import dataclass

import aiohttp
import jwt

HEADER = 'Cf-Access-Jwt-Assertion'
COOKIE = 'CF_Authorization'
LEEWAY_SECONDS = 30
MIN_REFETCH_SECONDS = 30


def _csv(value):
    return [item.strip() for item in value.split(',') if item.strip()]


def _team_domain(value):
    return value.strip().removeprefix('https://').rstrip('/')


@dataclass(frozen=True)
class AccessConfig:
    team_domain: str
    audiences: list
    emails: frozenset

    @classmethod
    def from_env(cls):
        team = _team_domain(os.environ.get('DEVBOX_ACCESS_TEAM_DOMAIN', ''))
        audiences = _csv(os.environ.get('DEVBOX_ACCESS_AUD', ''))
        if not team or not audiences:
            raise ValueError('DEVBOX_AUTH_MODE=cloudflare-access requires DEVBOX_ACCESS_TEAM_DOMAIN and DEVBOX_ACCESS_AUD')
        emails = frozenset(e.lower() for e in _csv(os.environ.get('DEVBOX_ACCESS_EMAILS', '')))
        return cls(team, audiences, emails)

    @property
    def issuer(self):
        return f'https://{self.team_domain}'

    @property
    def certs_url(self):
        return f'{self.issuer}/cdn-cgi/access/certs'


class JwksCache:
    """Signing keys by `kid`. Cloudflare rotates keys, so an unknown `kid`
    triggers a refetch, but at most one per `min_refetch` seconds so garbage
    tokens cannot turn devbox into a request amplifier."""

    def __init__(self, url, min_refetch=MIN_REFETCH_SECONDS, clock=time.monotonic):
        self.url = url
        self.min_refetch = min_refetch
        self.clock = clock
        self.keys = {}
        self._last_fetch = None
        self._lock = asyncio.Lock()

    async def key(self, session, kid):
        if kid in self.keys:
            return self.keys[kid]
        async with self._lock:
            if kid not in self.keys and self._may_refetch():
                await self._refetch(session)
        return self.keys.get(kid)

    def _may_refetch(self):
        return self._last_fetch is None or self.clock() - self._last_fetch >= self.min_refetch

    async def _refetch(self, session):
        """On any failure keep the previous keys; with none cached, every token is rejected."""
        self._last_fetch = self.clock()
        try:
            async with session.get(self.url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                response.raise_for_status()
                document = await response.json(content_type=None)
            self.keys = _parse_jwks(document)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError, TypeError):
            return


def _parse_jwks(document):
    keys = {}
    for jwk in document['keys']:
        if jwk.get('kty') != 'RSA' or not isinstance(jwk.get('kid'), str):
            continue
        keys[jwk['kid']] = jwt.PyJWK(jwk).key
    return keys


class CloudflareAccess:
    def __init__(self, config, jwks=None):
        self.config = config
        self.jwks = jwks or JwksCache(config.certs_url)

    @classmethod
    def from_env(cls):
        return cls(AccessConfig.from_env())

    async def verify(self, request, session):
        token = request.headers.get(HEADER) or request.cookies.get(COOKIE)
        if not token:
            return False
        try:
            claims = await self._claims(token, session)
        except jwt.PyJWTError:
            return False
        return self._email_allowed(claims)

    async def _claims(self, token, session):
        kid = jwt.get_unverified_header(token).get('kid')
        key = await self.jwks.key(session, kid) if isinstance(kid, str) else None
        if key is None:
            raise jwt.InvalidKeyError('Unknown signing key')
        return jwt.decode(token, key, algorithms=['RS256'], audience=self.config.audiences,
                          issuer=self.config.issuer, leeway=LEEWAY_SECONDS,
                          options={'require': ['exp', 'iss', 'aud']})

    def _email_allowed(self, claims):
        if not self.config.emails:
            return True
        email = claims.get('email')
        return isinstance(email, str) and email.lower() in self.config.emails
