"""Dashboard OpenBao login (#121): start a flow, finish it in /oidc/callback against a fake OpenBao."""
import os
import stat
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bao_login
import runtime
import server

PUBLIC = 'https://devbox-next.roo-net.cc'
TOKEN = 's.SECRET-bao-token-value'
CODE = 'SECRET-oauth-code'


class FakeBao:
    """Records OpenBao API calls and answers like OpenBao 2.6's OIDC and token endpoints."""

    def __init__(self):
        self.calls = []
        self.states = {}
        self.redirect_ok = True
        self.callback_error = None
        self.seq = 0

    async def auth_url(self, request):
        body = await request.json()
        self.calls.append(('auth_url', body))
        if not self.redirect_ok:
            return web.json_response({'data': {'auth_url': ''}})
        self.seq += 1
        state = f'st_state{self.seq}xyz'
        self.states[state] = body['client_nonce']
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(
            {'client_id': 'x', 'redirect_uri': body['redirect_uri'], 'state': state, 'nonce': 'n'})
        return web.json_response({'data': {'auth_url': url}})

    async def callback(self, request):
        q = dict(request.query)
        self.calls.append(('callback', q))
        if self.callback_error:
            return web.json_response({'errors': [self.callback_error]}, status=400)
        if self.states.pop(q.get('state'), None) != q.get('client_nonce') or q.get('code') != CODE:
            return web.json_response({'errors': ['invalid client_nonce']}, status=400)
        return web.json_response({'auth': {'client_token': TOKEN, 'lease_duration': 3600}})

    async def lookup_self(self, request):
        self.calls.append(('lookup-self', request.headers.get('X-Vault-Token')))
        if request.headers.get('X-Vault-Token') != TOKEN:
            return web.json_response({'errors': ['permission denied']}, status=403)
        return web.json_response({'data': {'id': TOKEN, 'ttl': 1500, 'display_name': 'oidc-me@example.com'}})


@pytest_asyncio.fixture
async def fake_bao():
    bao = FakeBao()
    app = web.Application()
    app.router.add_post('/v1/auth/oidc/oidc/auth_url', bao.auth_url)
    app.router.add_get('/v1/auth/oidc/oidc/callback', bao.callback)
    app.router.add_get('/v1/auth/token/lookup-self', bao.lookup_self)
    async with TestServer(app) as srv:
        yield bao, str(srv.make_url('')).rstrip('/')


def _env(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith('DEVBOX_')]:
        monkeypatch.delenv(key)
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    monkeypatch.setenv('DEVBOX_SOCKET', str(tmp_path / 'ctl.sock'))
    monkeypatch.setenv('DEVBOX_PUBLIC_URL', PUBLIC)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest_asyncio.fixture
async def devbox(tmp_path, monkeypatch, fake_bao):
    """(client, app, fake, home) with DEVBOX_BAO_ADDR pointing at the fake OpenBao."""
    home = _env(tmp_path, monkeypatch)
    bao, addr = fake_bao
    monkeypatch.setenv('DEVBOX_BAO_ADDR', addr)
    rt = runtime.Runtime()
    rt.ensure = AsyncMock(side_effect=AssertionError('must not start a terminal'))
    app = server.create_app(rt)
    async with TestClient(TestServer(app), headers={'Authorization': 'Bearer ' + app['token']}) as client:
        client.origin = f'http://{client.host}:{client.port}'
        yield client, app, bao, home


async def start(client):
    return await client.post('/api/bao/login', json={}, headers={'Origin': client.origin})


async def start_state(client):
    response = await start(client)
    assert response.status == 200, await response.text()
    url = (await response.json())['auth_url']
    return parse_qs(urlsplit(url).query)['state'][0]


def callback(client, state, code=CODE):
    return client.get('/oidc/callback?' + urlencode({'state': state, 'code': code}))


@pytest.mark.asyncio
async def test_start_asks_openbao_with_fresh_nonce_and_public_redirect(devbox):
    client, app, bao, _ = devbox
    await start_state(client)
    await start_state(client)
    (_, first), (_, second) = bao.calls
    assert first['role'] == 'admin'
    assert first['redirect_uri'] == PUBLIC + '/oidc/callback'
    assert len(first['client_nonce']) >= 32 and first['client_nonce'] != second['client_nonce']


@pytest.mark.asyncio
async def test_callback_writes_token_0600_and_reports_success(devbox, capsys):
    client, _, bao, home = devbox
    state = await start_state(client)
    response = await callback(client, state)
    text = await response.text()
    assert response.status == 200
    assert 'Logged in to OpenBao' in text and 'expires in 60 min' in text
    assert TOKEN not in text and CODE not in text
    assert response.headers['Cache-Control'] == 'no-store'
    token_file = home / '.vault-token'
    assert token_file.read_text() == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert [p.name for p in home.iterdir()] == ['.vault-token']
    assert bao.calls[-1] == ('callback', {'state': state, 'code': CODE, 'client_nonce': bao.calls[0][1]['client_nonce']})
    assert f'oidc-callback state={state[:6]} flow=bao outcome=claimed' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_existing_token_file_is_replaced_and_tightened(devbox):
    client, _, _, home = devbox
    (home / '.vault-token').write_text('old')
    (home / '.vault-token').chmod(0o644)
    await callback(client, await start_state(client))
    assert (home / '.vault-token').read_text() == TOKEN
    assert stat.S_IMODE((home / '.vault-token').stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_callback_is_single_use(devbox, capsys):
    client, _, bao, home = devbox
    state = await start_state(client)
    assert (await callback(client, state)).status == 200
    (home / '.vault-token').unlink()
    response = await callback(client, state)
    assert response.status == 404
    assert not (home / '.vault-token').exists()
    assert sum(1 for c in bao.calls if c[0] == 'callback') == 1
    assert f'state={state[:6]} flow=relay outcome=unknown' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_expired_state_is_rejected_without_calling_openbao(devbox, capsys):
    client, app, bao, home = devbox
    now = [1000.0]
    app['bao'].clock = lambda: now[0]
    state = await start_state(client)
    now[0] += bao_login.TTL_SECONDS + 1
    response = await callback(client, state)
    assert response.status == 410
    assert 'expired' in await response.text()
    assert not any(c[0] == 'callback' for c in bao.calls)
    assert not (home / '.vault-token').exists()
    assert f'state={state[:6]} flow=bao outcome=expired' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_expired_state_still_reports_expired_after_a_new_start(devbox):
    client, app, _, _ = devbox
    now = [1000.0]
    app['bao'].clock = lambda: now[0]
    state = await start_state(client)
    now[0] += bao_login.TTL_SECONDS + 1
    await start_state(client)
    assert (await callback(client, state)).status == 410


@pytest.mark.asyncio
async def test_unknown_state_is_rejected(devbox, capsys):
    client, _, bao, home = devbox
    await start_state(client)
    response = await callback(client, 'st_forged')
    assert response.status == 404
    assert not any(c[0] == 'callback' for c in bao.calls)
    assert not (home / '.vault-token').exists()
    assert 'state=st_for flow=relay outcome=unknown' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_openbao_error_is_shown_escaped_and_nothing_written(devbox, capsys):
    client, _, bao, home = devbox
    bao.callback_error = '<b>claim "email" not in bound_claims</b>'
    state = await start_state(client)
    response = await callback(client, state)
    text = await response.text()
    assert response.status == 502
    assert 'OpenBao login failed' in text and '&lt;b&gt;claim' in text and '<b>claim' not in text
    assert CODE not in text
    assert not (home / '.vault-token').exists()
    assert f'state={state[:6]} flow=bao outcome=failed' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unregistered_redirect_uri_fails_the_start(devbox):
    client, app, bao, _ = devbox
    bao.redirect_ok = False
    response = await start(client)
    assert response.status == 502
    assert 'allowed redirect URI' in (await response.json())['error']
    assert app['bao'].pending == {}


@pytest.mark.asyncio
async def test_unreachable_openbao_fails_cleanly(devbox):
    client, app, _, _ = devbox
    app['bao'].addr = 'http://127.0.0.1:1'
    response = await start(client)
    assert response.status == 502
    assert 'unreachable' in (await response.json())['error']


@pytest.mark.asyncio
async def test_no_secrets_in_logs(devbox, capsys):
    client, _, bao, _ = devbox
    state = await start_state(client)
    await callback(client, state)
    bao.callback_error = 'boom'
    await callback(client, await start_state(client))
    await callback(client, 'st_unknown')
    await client.get('/api/bao')
    out = capsys.readouterr()
    logs = out.out + out.err
    nonces = [c[1]['client_nonce'] for c in bao.calls if c[0] == 'auth_url']
    for secret in [TOKEN, CODE, state, *nonces]:
        assert secret not in logs
    assert len([line for line in logs.splitlines() if line.startswith('oidc-callback ')]) == 3


@pytest.mark.asyncio
async def test_start_needs_an_origin_header(devbox):
    client, app, _, _ = devbox
    response = await client.post('/api/bao/login', json={})
    assert response.status == 403
    assert app['bao'].pending == {}


@pytest.mark.asyncio
async def test_start_rejects_cross_origin_and_non_json(devbox):
    client, app, _, _ = devbox
    assert (await client.post('/api/bao/login', json={}, headers={'Origin': 'https://evil.example'})).status == 403
    assert (await client.post('/api/bao/login', data='{}', headers={
        'Origin': client.origin, 'Content-Type': 'text/plain'})).status == 415
    assert app['bao'].pending == {}


@pytest.mark.asyncio
async def test_start_requires_dashboard_auth(devbox):
    client, app, _, _ = devbox
    async with TestClient(client.server) as anonymous:
        response = await anonymous.post('/api/bao/login', json={}, headers={'Origin': client.origin})
    assert response.status == 401
    assert app['bao'].pending == {}


@pytest.mark.asyncio
async def test_routes_refused_over_unix_socket(devbox, tmp_path):
    client, app, _, home = devbox
    site = web.UnixSite(client.server.runner, app['socket'])
    await site.start()
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=app['socket'])) as unix:
            # Sanity check that the socket is the trusted policy: listings work without a token.
            async with unix.get('http://devbox/api/widgets') as ok:
                assert ok.status == 200
            async with unix.post('http://devbox/api/bao/login', json={}, headers={'Origin': 'http://devbox'}) as r:
                assert r.status == 403
            async with unix.get('http://devbox/api/bao') as r:
                assert r.status == 403
    finally:
        await site.stop()
    assert app['bao'].pending == {}


@pytest.mark.asyncio
async def test_status_reports_expiry_without_the_token(devbox):
    client, _, bao, home = devbox
    assert await (await client.get('/api/bao')).json() == {'enabled': True, 'logged_in': False}
    await callback(client, await start_state(client))
    response = await client.get('/api/bao')
    body = await response.json()
    assert body == {'enabled': True, 'logged_in': True, 'expires_in': 1500, 'display_name': 'oidc-me@example.com'}
    assert TOKEN not in await response.text()
    (home / '.vault-token').write_text('stale')
    assert (await (await client.get('/api/bao')).json())['logged_in'] is False


@pytest.mark.asyncio
async def test_dashboard_shows_control_when_configured(devbox):
    client, _, _, _ = devbox
    assert '"bao": true' in await (await client.get('/')).text()


@pytest.mark.asyncio
async def test_custom_role_and_mount(tmp_path, monkeypatch, fake_bao):
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv('DEVBOX_BAO_ADDR', 'http://openbao.openbao.svc:8200/')
    monkeypatch.setenv('DEVBOX_BAO_ROLE', 'reader')
    monkeypatch.setenv('DEVBOX_BAO_MOUNT', '/google/')
    login = bao_login.from_env()
    assert (login.addr, login.role, login.mount) == ('http://openbao.openbao.svc:8200', 'reader', 'google')


@pytest.mark.parametrize('key,value', [
    ('DEVBOX_BAO_ADDR', 'openbao:8200'), ('DEVBOX_BAO_ROLE', 'a b'),
    ('DEVBOX_BAO_MOUNT', '../sys'), ('DEVBOX_BAO_ROLE', 'x?y')])
def test_invalid_config_fails_startup(tmp_path, monkeypatch, key, value):
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv('DEVBOX_BAO_ADDR', 'http://openbao:8200')
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        server.create_app(runtime.Runtime())


# --- feature off -------------------------------------------------------------

@pytest_asyncio.fixture
async def unset(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    rt = runtime.Runtime()
    rt.ensure = AsyncMock(side_effect=AssertionError('must not start a terminal'))
    app = server.create_app(rt)
    async with TestClient(TestServer(app), headers={'Authorization': 'Bearer ' + app['token']}) as client:
        client.origin = f'http://{client.host}:{client.port}'
        yield client, app


@pytest.mark.asyncio
async def test_feature_hidden_when_unset(unset):
    client, app = unset
    assert app['bao'] is None
    assert '"bao": false' in await (await client.get('/')).text()
    assert await (await client.get('/api/bao')).json() == {'enabled': False}
    assert (await start(client)).status == 404


@pytest.mark.asyncio
async def test_relay_path_unchanged_when_unset_and_logged(unset, capsys):
    client, app = unset
    rt = app['runtime']
    rt.auth_relay.pending['relay-state-1'] = {'session': 'gone', 'port': 8250, 'expires': 1e18}
    response = await client.get('/oidc/callback?state=relay-state-1&code=' + CODE)
    assert response.status == 503
    out = capsys.readouterr().out
    assert 'oidc-callback state=relay- flow=relay outcome=disconnected' in out
    assert CODE not in out and 'relay-state-1' not in out


@pytest.mark.asyncio
async def test_relay_state_replays_while_bao_is_configured(devbox, capsys):
    """A state the relay registered still replays to the session's port, with BAO configured."""
    client, app, bao, home = devbox
    hits = []

    async def loopback(request):
        hits.append(request.path_qs)
        return web.Response(text='ok')
    loop_app = web.Application()
    loop_app.router.add_get('/oidc/callback', loopback)
    async with TestServer(loop_app) as loop_srv:
        rt = app['runtime']
        rt.sessions['one'] = {'name': 'one', 'connection': 'connected', 'target': {'type': 'local'}}
        rt.auth_relay.pending['relaystate'] = {'session': 'one', 'port': loop_srv.port, 'expires': 1e18}
        await start_state(client)
        response = await client.get('/oidc/callback?state=relaystate&code=abc')
    assert response.status == 200
    assert hits == ['/oidc/callback?state=relaystate&code=abc']
    assert not any(c[0] == 'callback' for c in bao.calls)
    assert not (home / '.vault-token').exists()
    assert 'oidc-callback state=relays flow=relay outcome=replayed' in capsys.readouterr().out
