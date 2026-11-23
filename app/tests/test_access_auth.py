import time

import aiohttp
import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from unittest.mock import AsyncMock

import runtime
import server

TEAM = 'team.cloudflareaccess.com'
AUD = 'aud-one'


class Signer:
    def __init__(self, kid):
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self):
        data = jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**data, 'kid': self.kid, 'alg': 'RS256', 'use': 'sig'}

    def token(self, headers=None, **overrides):
        now = int(time.time())
        claims = {'aud': [AUD], 'iss': f'https://{TEAM}', 'exp': now + 300, 'iat': now,
                  'email': 'me@example.com', **overrides}
        claims = {k: v for k, v in claims.items() if v is not None}
        pem = self.private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
        return jwt.encode(claims, pem, algorithm='RS256', headers={'kid': self.kid, **(headers or {})})


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Jwks:
    def __init__(self):
        self.signers = []
        self.hits = 0
        self.fail = False

    async def handler(self, _):
        self.hits += 1
        if self.fail:
            raise web.HTTPServiceUnavailable()
        return web.json_response({'keys': [s.jwk() for s in self.signers]})


@pytest_asyncio.fixture
async def access(tmp_path, monkeypatch):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    monkeypatch.setenv('DEVBOX_AUTH_MODE', 'cloudflare-access')
    monkeypatch.setenv('DEVBOX_ACCESS_TEAM_DOMAIN', TEAM)
    monkeypatch.setenv('DEVBOX_ACCESS_AUD', AUD)
    jwks = Jwks()
    jwks.signers.append(Signer('k1'))
    jwks_app = web.Application()
    jwks_app.router.add_get('/certs', jwks.handler)
    async with TestServer(jwks_app) as jwks_server:
        rt = runtime.Runtime()
        rt.ensure = AsyncMock(side_effect=AssertionError('Listing must not start a terminal'))
        app = server.create_app(rt)
        app['access'].jwks.url = str(jwks_server.make_url('/certs'))
        app['access'].jwks.clock = Clock()
        async with TestClient(TestServer(app)) as client:
            yield client, jwks, app


def headers(token):
    return {'Cf-Access-Jwt-Assertion': token}


@pytest.mark.asyncio
async def test_valid_header_and_cookie(access):
    client, jwks, _ = access
    token = jwks.signers[0].token()
    assert (await client.get('/api/sessions', headers=headers(token))).status == 200
    assert (await client.get('/api/sessions', cookies={'CF_Authorization': token})).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [
    {'exp': int(time.time()) - 3600},
    {'aud': ['other']},
    {'iss': 'https://evil.cloudflareaccess.com'},
    {'aud': None},
    {'exp': None},
    {'nbf': int(time.time()) + 3600},
])
async def test_invalid_claims_rejected(access, overrides):
    client, jwks, _ = access
    response = await client.get('/api/sessions', headers=headers(jwks.signers[0].token(**overrides)))
    assert response.status == 401


@pytest.mark.asyncio
async def test_small_clock_skew_is_tolerated(access):
    client, jwks, _ = access
    token = jwks.signers[0].token(exp=int(time.time()) - 5, nbf=int(time.time()) + 5)
    assert (await client.get('/api/sessions', headers=headers(token))).status == 200


@pytest.mark.asyncio
async def test_bad_signature_rejected(access):
    client, jwks, _ = access
    forger = Signer('k1')  # right kid, wrong private key
    assert (await client.get('/api/sessions', headers=headers(forger.token()))).status == 401


@pytest.mark.asyncio
async def test_other_algorithms_rejected(access):
    client, _, _ = access
    claims = {'aud': [AUD], 'iss': f'https://{TEAM}', 'exp': int(time.time()) + 300}
    hs256 = jwt.encode(claims, 'x' * 32, algorithm='HS256', headers={'kid': 'k1'})
    unsigned = jwt.encode(claims, None, algorithm='none', headers={'kid': 'k1'})
    for token in (hs256, unsigned, 'not-a-jwt'):
        assert (await client.get('/api/sessions', headers=headers(token))).status == 401


@pytest.mark.asyncio
async def test_unknown_kid_refetches_keys_but_is_rate_limited(access):
    client, jwks, app = access
    assert (await client.get('/api/sessions', headers=headers(jwks.signers[0].token()))).status == 200
    assert jwks.hits == 1
    rotated, other = Signer('k2'), Signer('k3')
    jwks.signers.append(rotated)
    # A new kid right after a fetch must not hit Cloudflare again.
    assert (await client.get('/api/sessions', headers=headers(rotated.token()))).status == 401
    assert jwks.hits == 1
    app['access'].jwks.clock.now += 31
    assert (await client.get('/api/sessions', headers=headers(rotated.token()))).status == 200
    assert jwks.hits == 2
    assert (await client.get('/api/sessions', headers=headers(other.token()))).status == 401
    assert jwks.hits == 2


@pytest.mark.asyncio
async def test_unreachable_jwks_fails_closed_then_recovers(access):
    client, jwks, app = access
    jwks.fail = True
    token = jwks.signers[0].token()
    assert (await client.get('/api/sessions', headers=headers(token))).status == 401
    jwks.fail = False
    app['access'].jwks.clock.now += 31
    assert (await client.get('/api/sessions', headers=headers(token))).status == 200


@pytest.mark.asyncio
async def test_rotated_out_keys_are_dropped_and_failed_refetch_keeps_cache(access):
    client, jwks, app = access
    token = jwks.signers[0].token()
    assert (await client.get('/api/sessions', headers=headers(token))).status == 200
    jwks.fail = True
    app['access'].jwks.clock.now += 31
    assert (await client.get('/api/sessions', headers=headers(Signer('k9').token()))).status == 401
    assert (await client.get('/api/sessions', headers=headers(token))).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/sessions', '/s/1/', '/browser/vnc.html'])
async def test_missing_credentials_get_401_json(access, path):
    client, _, _ = access
    response = await client.get(path)
    assert response.status == 401
    assert await response.json() == {'error': 'Authentication required'}


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/', '/view/one', '/login', '/static/session-cards.js'])
async def test_pages_get_access_sign_in_page(access, path):
    client, _, _ = access
    response = await client.get(path, allow_redirects=False)
    assert response.status == 401
    assert 'Sign in through Cloudflare Access' in await response.text()


@pytest.mark.asyncio
async def test_login_post_does_not_bypass_access(access):
    client, _, _ = access
    response = await client.post('/login', json={'token': ''})
    assert response.status == 401


@pytest.mark.asyncio
async def test_healthz_stays_open(access):
    client, _, _ = access
    assert (await client.get('/healthz')).status == 200


@pytest.mark.asyncio
async def test_origin_check_still_applies(access):
    client, jwks, _ = access
    response = await client.get('/api/sessions', headers={**headers(jwks.signers[0].token()),
                                                          'Origin': 'https://evil.example'})
    assert response.status == 403


@pytest.mark.asyncio
async def test_email_allowlist(access, monkeypatch):
    client, jwks, app = access
    app['access'].config = type(app['access'].config)(TEAM, [AUD], frozenset({'me@example.com'}))
    signer = jwks.signers[0]
    assert (await client.get('/api/sessions', headers=headers(signer.token(email='ME@example.com')))).status == 200
    assert (await client.get('/api/sessions', headers=headers(signer.token(email='you@example.com')))).status == 401
    assert (await client.get('/api/sessions', headers=headers(signer.token(email=None)))).status == 401


@pytest.mark.asyncio
async def test_multiple_audiences(access, monkeypatch):
    client, jwks, app = access
    app['access'].config = type(app['access'].config)(TEAM, [AUD, 'aud-two'], frozenset())
    assert (await client.get('/api/sessions', headers=headers(jwks.signers[0].token(aud=['aud-two'])))).status == 200


@pytest.mark.parametrize('env', [
    {'DEVBOX_ACCESS_AUD': ''},
    {'DEVBOX_ACCESS_TEAM_DOMAIN': ''},
])
def test_startup_requires_team_domain_and_aud(tmp_path, monkeypatch, env):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_AUTH_MODE', 'cloudflare-access')
    monkeypatch.setenv('DEVBOX_ACCESS_TEAM_DOMAIN', TEAM)
    monkeypatch.setenv('DEVBOX_ACCESS_AUD', AUD)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match='DEVBOX_ACCESS_TEAM_DOMAIN and DEVBOX_ACCESS_AUD'):
        server.create_app(runtime.Runtime())


def test_team_domain_accepts_url_form_and_csv_audiences(tmp_path, monkeypatch):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_AUTH_MODE', 'cloudflare-access')
    monkeypatch.setenv('DEVBOX_ACCESS_TEAM_DOMAIN', 'https://' + TEAM + '/')
    monkeypatch.setenv('DEVBOX_ACCESS_AUD', 'a, b ,')
    monkeypatch.setenv('DEVBOX_ACCESS_EMAILS', 'X@Example.com, y@example.com')
    config = server.create_app(runtime.Runtime())['access'].config
    assert config.issuer == f'https://{TEAM}'
    assert config.certs_url == f'https://{TEAM}/cdn-cgi/access/certs'
    assert config.audiences == ['a', 'b']
    assert config.emails == {'x@example.com', 'y@example.com'}


@pytest.mark.asyncio
async def test_websocket_upgrade_requires_jwt(access):
    client, jwks, app = access
    with pytest.raises(aiohttp.WSServerHandshakeError) as denied:
        await client.ws_connect('/s/1/ws')
    assert denied.value.status == 401
    # With a valid JWT the upgrade passes the boundary; no session is connected here.
    with pytest.raises(aiohttp.WSServerHandshakeError) as allowed:
        await client.ws_connect('/s/1/ws', headers=headers(jwks.signers[0].token()))
    assert allowed.value.status == 503


@pytest.mark.asyncio
async def test_websocket_relays_with_jwt(access):
    client, jwks, app = access
    upstream = web.Application()

    async def echo(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            await ws.send_str('echo ' + message.data)
        return ws

    upstream.router.add_get('/s/9/ws', echo)
    runner = web.AppRunner(upstream)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', 7699).start()
    except OSError:
        await runner.cleanup()
        pytest.skip('port 7699 busy')
    try:
        app['runtime'].sessions['two'] = {'name': 'two', 'port': 7699, 'connection': 'connected'}
        async with client.ws_connect('/s/9/ws', headers=headers(jwks.signers[0].token())) as ws:
            await ws.send_str('hi')
            assert (await ws.receive_str()) == 'echo hi'
    finally:
        await runner.cleanup()
