"""DEVBOX_TOKEN_PORT: a second listener with token auth, chosen by the local port, never by a header."""
import os
import socket
import stat
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock

import runtime
import server
from test_access_auth import AUD, TEAM, Clock, Jwks, Signer, headers


def _clean_env(tmp_path, monkeypatch, mode):
    for key in [k for k in os.environ if k.startswith('DEVBOX_')]:
        monkeypatch.delenv(key)
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    monkeypatch.setenv('DEVBOX_AUTH_MODE', mode)
    if mode == 'cloudflare-access':
        monkeypatch.setenv('DEVBOX_ACCESS_TEAM_DOMAIN', TEAM)
        monkeypatch.setenv('DEVBOX_ACCESS_AUD', AUD)


async def _serve(tmp_path, monkeypatch, mode):
    """Yield (main client, token-port session, app, jwks): one runner, two TCP listeners, as in main()."""
    _clean_env(tmp_path, monkeypatch, mode)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    monkeypatch.setenv('DEVBOX_TOKEN_PORT', str(port))
    jwks = Jwks()
    jwks.signers.append(Signer('k1'))
    jwks_app = web.Application()
    jwks_app.router.add_get('/certs', jwks.handler)
    async with TestServer(jwks_app) as jwks_server:
        rt = runtime.Runtime()
        rt.ensure = AsyncMock(side_effect=AssertionError('Listing must not start a terminal'))
        app = server.create_app(rt)
        if mode == 'cloudflare-access':
            app['access'].jwks.url = str(jwks_server.make_url('/certs'))
            app['access'].jwks.clock = Clock()
        async with TestClient(TestServer(app)) as main:
            await web.SockSite(main.server.runner, sock).start()
            async with aiohttp.ClientSession(base_url=f'http://127.0.0.1:{port}') as tok:
                yield main, tok, app, jwks


@pytest_asyncio.fixture
async def access(tmp_path, monkeypatch):
    async for served in _serve(tmp_path, monkeypatch, 'cloudflare-access'):
        yield served


@pytest_asyncio.fixture
async def proxy_mode(tmp_path, monkeypatch):
    async for served in _serve(tmp_path, monkeypatch, 'proxy'):
        yield served


def cookie(app):
    return {'Cookie': f"{server.COOKIE}={app['token']}"}


def bearer(app):
    return {'Authorization': 'Bearer ' + app['token']}


@pytest.mark.asyncio
async def test_main_port_rejects_token_in_access_mode(access):
    main, _, app, _ = access
    for creds in ({}, cookie(app), bearer(app)):
        response = await main.get('/api/sessions', headers=creds)
        assert response.status == 401
        assert await response.json() == {'error': 'Authentication required'}
        response = await main.get('/', headers=creds, allow_redirects=False)
        assert response.status == 401
        assert 'Sign in through Cloudflare Access' in await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/sessions', '/s/1/', '/browser/vnc.html'])
async def test_main_port_token_never_reaches_proxied_paths(access, path):
    main, _, app, _ = access
    assert (await main.get(path, headers={**cookie(app), **bearer(app)})).status == 401


@pytest.mark.asyncio
async def test_token_port_requires_token(access):
    _, tok, app, _ = access
    response = await tok.get('/', allow_redirects=False)
    assert response.status == 302
    assert response.headers['Location'] == '/login'
    response = await tok.get('/api/sessions')
    assert response.status == 401
    assert await response.json() == {'error': 'Authentication required'}
    assert (await tok.get('/api/sessions', headers={'Cookie': f'{server.COOKIE}=wrong'})).status == 401
    assert (await tok.get('/api/sessions', headers={'Authorization': 'Bearer wrong'})).status == 401


@pytest.mark.asyncio
async def test_token_port_accepts_token_without_jwt(access):
    _, tok, app, _ = access
    assert (await tok.get('/api/sessions', headers=cookie(app))).status == 200
    assert (await tok.get('/api/sessions', headers=bearer(app))).status == 200
    assert (await tok.get('/', headers=cookie(app))).status == 200


@pytest.mark.asyncio
async def test_token_port_does_not_accept_jwt_instead_of_token(access):
    _, tok, _, jwks = access
    assert (await tok.get('/api/sessions', headers=headers(jwks.signers[0].token()))).status == 401


@pytest.mark.asyncio
async def test_login_on_main_port_in_access_mode_is_401(access):
    main, _, app, _ = access
    assert (await main.get('/login')).status == 401
    response = await main.post('/login', json={'token': app['token']})
    assert response.status == 401
    assert 'Set-Cookie' not in response.headers


@pytest.mark.asyncio
async def test_login_on_token_port(access):
    _, tok, app, _ = access
    response = await tok.get('/login')
    assert response.status == 200
    assert 'Access token' in await response.text()
    assert (await tok.post('/login', json={'token': 'wrong'})).status == 401
    assert (await tok.post('/login', data='{"token": ""}')).status == 415
    response = await tok.post('/login', json={'token': app['token']})
    assert response.status == 200
    assert f"{server.COOKIE}={app['token']}" in response.headers['Set-Cookie']


@pytest.mark.asyncio
async def test_token_port_cross_origin_is_403(access):
    _, tok, app, _ = access
    evil = await tok.get('/api/sessions', headers={**cookie(app), 'Origin': 'https://evil.example'})
    assert evil.status == 403
    same = await tok.get('/api/sessions', headers={**cookie(app), 'Origin': f'http://{tok._base_url.authority}'})
    assert same.status == 200


@pytest.mark.asyncio
async def test_token_port_requires_json_bodies(access):
    _, tok, app, _ = access
    response = await tok.post('/api/sessions', data='name=x', headers={**cookie(app), 'Content-Type': 'text/plain'})
    assert response.status == 415


@pytest.mark.asyncio
async def test_headers_cannot_select_token_policy(access):
    main, tok, app, _ = access
    spoof = {'Host': f'127.0.0.1:{tok._base_url.port}', 'X-Forwarded-Port': str(tok._base_url.port),
             'X-Forwarded-Host': f'127.0.0.1:{tok._base_url.port}', 'Tailscale-User-Login': 'me@example.com'}
    assert (await main.get('/api/sessions', headers={**spoof, **cookie(app)})).status == 401
    assert (await main.post('/login', json={'token': app['token']}, headers=spoof)).status == 401
    assert (await tok.get('/api/sessions', headers=spoof)).status == 401


@pytest.mark.asyncio
async def test_websocket_on_token_port_upgrades_with_cookie(access):
    main, tok, app, _ = access
    upstream = web.Application()

    async def echo(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            await ws.send_str('echo ' + message.data)
        return ws

    upstream.router.add_route('GET', '/s/{slot}/ws', echo)
    runner = web.AppRunner(upstream)
    await runner.setup()
    # The proxy only dials 127.0.0.1:7690+slot, so use whichever slot is free here.
    for slot in range(99, -1, -1):
        try:
            await web.TCPSite(runner, '127.0.0.1', 7690 + slot).start()
            break
        except OSError:
            continue
    else:
        await runner.cleanup()
        pytest.skip('no free session port')
    try:
        app['runtime'].sessions['two'] = {'name': 'two', 'port': 7690 + slot, 'connection': 'connected'}
        with pytest.raises(aiohttp.WSServerHandshakeError) as denied:
            await main.ws_connect(f'/s/{slot}/ws', headers=cookie(app))
        assert denied.value.status == 401
        with pytest.raises(aiohttp.WSServerHandshakeError) as unauthenticated:
            await tok.ws_connect(f'/s/{slot}/ws')
        # The upgrade is refused with the /login redirect, which the client follows to the form.
        assert unauthenticated.value.request_info.url.path == '/login'
        assert unauthenticated.value.history[0].status == 302
        async with tok.ws_connect(f'/s/{slot}/ws', headers=cookie(app)) as ws:
            await ws.send_str('hi')
            assert (await ws.receive_str()) == 'echo hi'
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_proxy_mode_main_port_unchanged_and_token_port_needs_token(proxy_mode):
    main, tok, app, _ = proxy_mode
    assert (await main.get('/api/sessions')).status == 200
    # The real token is checked only on the token port, so the main port is no oracle for it.
    assert (await main.post('/login', json={'token': app['token']})).status == 401
    assert (await tok.get('/api/sessions')).status == 401
    assert (await tok.get('/api/sessions', headers=bearer(app))).status == 200


def test_token_file_created_when_token_port_set(tmp_path, monkeypatch):
    _clean_env(tmp_path, monkeypatch, 'cloudflare-access')
    monkeypatch.setenv('DEVBOX_TOKEN_PORT', '7681')
    app = server.create_app(runtime.Runtime())
    token_file = Path(tmp_path / 'state' / 'access-token')
    assert app['token_port'] == 7681
    assert app['token'] == token_file.read_text().strip() and len(app['token']) >= 24
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_custom_token_file_used_when_token_port_set(tmp_path, monkeypatch):
    _clean_env(tmp_path, monkeypatch, 'proxy')
    monkeypatch.setenv('DEVBOX_TOKEN_PORT', '7681')
    monkeypatch.setenv('DEVBOX_TOKEN_FILE', str(tmp_path / 'tok'))
    (tmp_path / 'tok').write_text('t' * 30 + '\n')
    assert server.create_app(runtime.Runtime())['token'] == 't' * 30


@pytest.mark.parametrize('mode', ['token', 'proxy', 'cloudflare-access'])
@pytest.mark.parametrize('value', [None, ''])
def test_unset_token_port_is_the_default(tmp_path, monkeypatch, mode, value):
    _clean_env(tmp_path, monkeypatch, mode)
    if value is not None:
        monkeypatch.setenv('DEVBOX_TOKEN_PORT', value)
    app = server.create_app(runtime.Runtime())
    token_file = tmp_path / 'state' / 'access-token'
    assert app['token_port'] is None
    assert token_file.exists() == (mode == 'token')
    assert (app['token'] == '') == (mode != 'token')
    assert ('access' in app) == (mode == 'cloudflare-access')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['token', 'proxy', 'cloudflare-access'])
async def test_unset_token_port_keeps_responses(tmp_path, monkeypatch, mode):
    """Without DEVBOX_TOKEN_PORT every listener behaves as its mode always did."""
    _clean_env(tmp_path, monkeypatch, mode)
    if mode == 'cloudflare-access':
        monkeypatch.setenv('DEVBOX_ACCESS_TEAM_DOMAIN', TEAM)
    app = server.create_app(runtime.Runtime())
    if mode == 'cloudflare-access':
        app['access'].jwks.url = 'http://127.0.0.1:9/certs'
    async with TestClient(TestServer(app)) as client:
        api = await client.get('/api/sessions')
        page = await client.get('/', allow_redirects=False)
        login = await client.get('/login')
        empty_login = await client.post('/login', json={'token': ''})
    expected = {'token': (401, 302, 200, 401), 'proxy': (200, 200, 200, 200),
                'cloudflare-access': (401, 401, 401, 401)}[mode]
    assert (api.status, page.status, login.status, empty_login.status) == expected


@pytest.mark.parametrize('value, error', [
    ('abc', 'must be a TCP port'), ('0', 'must be a TCP port'), ('70000', 'must be a TCP port'),
    ('7680', 'must differ from DEVBOX_PORT'),
])
def test_invalid_token_port_fails_startup(tmp_path, monkeypatch, value, error):
    _clean_env(tmp_path, monkeypatch, 'cloudflare-access')
    monkeypatch.setenv('DEVBOX_TOKEN_PORT', value)
    with pytest.raises(ValueError, match=error):
        server.create_app(runtime.Runtime())
