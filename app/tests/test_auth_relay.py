import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth_relay

PUBLIC = 'https://devbox-next.roo-net.cc'


def auth_url(state='st1', redirect='https://devbox-next.roo-net.cc:443/oidc/callback'):
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(
        {'client_id': 'x', 'redirect_uri': redirect, 'state': state})


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv('DEVBOX_PUBLIC_URL', PUBLIC)
    monkeypatch.delenv('DEVBOX_PORT', raising=False)
    clock = Clock()
    return auth_relay.AuthRelay(clock=clock), clock


def test_register_then_claim_returns_registered_session_and_port(relay):
    r, _ = relay
    r.register('one', 8250, auth_url())
    assert r.claim('st1') == ('one', 8250)


def test_claim_is_single_use(relay):
    r, _ = relay
    r.register('one', 8250, auth_url())
    r.claim('st1')
    with pytest.raises(auth_relay.Unknown):
        r.claim('st1')


def test_unknown_state_rejected(relay):
    r, _ = relay
    with pytest.raises(auth_relay.Unknown):
        r.claim('nope')
    with pytest.raises(auth_relay.Unknown):
        r.claim('')


def test_expired_state_rejected_and_removed(relay):
    r, clock = relay
    r.register('one', 8250, auth_url())
    clock.now += auth_relay.TTL_SECONDS + 1
    with pytest.raises(auth_relay.Expired):
        r.claim('st1')
    with pytest.raises(auth_relay.Unknown):
        r.claim('st1')


def test_register_purges_expired_entries(relay):
    r, clock = relay
    r.register('one', 8250, auth_url('old'))
    clock.now += auth_relay.TTL_SECONDS + 1
    r.register('one', 8250, auth_url('new'))
    with pytest.raises(auth_relay.Unknown):
        r.claim('old')


def test_reregistering_same_session_and_port_replaces_previous(relay):
    r, _ = relay
    r.register('one', 8250, auth_url('a'))
    r.register('one', 8250, auth_url('b'))
    with pytest.raises(auth_relay.Unknown):
        r.claim('a')
    assert r.claim('b') == ('one', 8250)


def test_requires_public_url(relay, monkeypatch):
    r, _ = relay
    monkeypatch.delenv('DEVBOX_PUBLIC_URL')
    with pytest.raises(ValueError, match='DEVBOX_PUBLIC_URL'):
        r.register('one', 8250, auth_url())


@pytest.mark.parametrize('redirect', [
    'http://localhost:8250/oidc/callback',
    'https://evil.example/oidc/callback',
    'https://devbox-next.roo-net.cc:8443/oidc/callback',
    'https://devbox-next.roo-net.cc/other',
    '',
])
def test_redirect_uri_must_be_devbox_callback(relay, redirect):
    r, _ = relay
    with pytest.raises(ValueError, match='redirect_uri'):
        r.register('one', 8250, auth_url(redirect=redirect))


def test_redirect_uri_without_explicit_port_accepted(relay):
    r, _ = relay
    r.register('one', 8250, auth_url(redirect='https://devbox-next.roo-net.cc/oidc/callback'))


def test_url_without_state_rejected(relay):
    r, _ = relay
    with pytest.raises(ValueError, match='state'):
        r.register('one', 8250, 'https://accounts.google.com/auth?redirect_uri=' + PUBLIC)


@pytest.mark.parametrize('port', [0, 80, 1023, 65536, 7680, 7690, 7789, 6080, 'x', None, True])
def test_reserved_or_invalid_ports_rejected(relay, port):
    r, _ = relay
    with pytest.raises(ValueError, match='port'):
        r.register('one', port, auth_url())


def test_custom_devbox_port_reserved(relay, monkeypatch):
    r, _ = relay
    monkeypatch.setenv('DEVBOX_PORT', '9000')
    with pytest.raises(ValueError, match='port'):
        r.register('one', 9000, auth_url())


# --- HTTP callback -------------------------------------------------------

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest_asyncio

from test_dashboard import dashboard  # noqa: F401  (fixture)
import server


@pytest_asyncio.fixture
async def upstream():
    """Stands in for bao's loopback listener and records what reaches it."""
    hits = []

    async def handler(request):
        hits.append((request.method, request.path_qs))
        return web.Response(text='bao ok', status=hits_status[0])

    hits_status = [200]
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handler)
    async with TestServer(app) as srv:
        yield srv.port, hits, hits_status


@pytest_asyncio.fixture
async def relayed(dashboard, upstream, monkeypatch):  # noqa: F811
    client, rt = dashboard
    monkeypatch.setenv('DEVBOX_PUBLIC_URL', PUBLIC)
    rt.sessions['one']['connection'] = 'connected'
    port, hits, status = upstream
    return client, rt, port, hits, status


async def register(client, port, state='st1'):
    return await client.post('/api/sessions/one/auth-register', json={'port': port, 'url': auth_url(state)})


@pytest.mark.asyncio
async def test_callback_replays_path_and_query_to_registered_port(relayed):
    client, _, port, hits, _ = relayed
    assert (await register(client, port)).status == 200
    response = await client.get('/oidc/callback?state=st1&code=abc')
    assert response.status == 200
    assert 'close this tab' in await response.text()
    assert hits == [('GET', '/oidc/callback?state=st1&code=abc')]


@pytest.mark.asyncio
async def test_callback_second_use_is_rejected_without_reaching_port(relayed):
    client, _, port, hits, _ = relayed
    await register(client, port)
    await client.get('/oidc/callback?state=st1&code=abc')
    response = await client.get('/oidc/callback?state=st1&code=abc')
    assert response.status == 404
    assert len(hits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', '?code=abc', '?state=other&code=abc'])
async def test_callback_unknown_state_rejected(relayed, query):
    client, _, port, hits, _ = relayed
    await register(client, port)
    assert (await client.get('/oidc/callback' + query)).status == 404
    assert hits == []


@pytest.mark.asyncio
async def test_callback_expired_state_rejected(relayed):
    client, rt, port, hits, _ = relayed
    clock = Clock()
    rt.auth_relay.clock = clock
    await register(client, port)
    clock.now += auth_relay.TTL_SECONDS + 1
    assert (await client.get('/oidc/callback?state=st1&code=abc')).status == 410
    assert hits == []


@pytest.mark.asyncio
async def test_callback_for_disconnected_session_is_unavailable(relayed):
    client, rt, port, hits, _ = relayed
    await register(client, port)
    rt.sessions['one']['connection'] = 'disconnected'
    assert (await client.get('/oidc/callback?state=st1&code=abc')).status == 503
    assert hits == []


@pytest.mark.asyncio
async def test_callback_reports_upstream_failure_without_reflecting_query(relayed):
    client, _, port, _, status = relayed
    status[0] = 500
    await register(client, port, state='<b>x')
    response = await client.get('/oidc/callback?state=%3Cb%3Ex&code=%3Cscript%3E')
    assert response.status == 502
    body = await response.text()
    assert '<script>' not in body and '<b>x' not in body


@pytest.mark.asyncio
async def test_callback_unreachable_port_reports_502(relayed):
    client, _, _, _, _ = relayed
    await register(client, 59999)
    assert (await client.get('/oidc/callback?state=st1&code=abc')).status == 502


@pytest.mark.asyncio
async def test_callback_response_is_not_cached_or_referred(relayed):
    client, _, port, _, _ = relayed
    await register(client, port)
    response = await client.get('/oidc/callback?state=st1&code=abc')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['Referrer-Policy'] == 'no-referrer'


@pytest.mark.asyncio
async def test_register_rejects_foreign_redirect_and_reserved_port(relayed):
    client, _, port, hits, _ = relayed
    bad = await client.post('/api/sessions/one/auth-register',
                            json={'port': port, 'url': auth_url(redirect='https://evil.example/oidc/callback')})
    assert bad.status == 400
    assert (await register(client, 7680)).status == 400
    assert (await client.get('/oidc/callback?state=st1&code=abc')).status == 404


@pytest.mark.asyncio
async def test_callback_requires_authentication(dashboard, monkeypatch):  # noqa: F811
    client, rt = dashboard
    rt.sessions['one']['connection'] = 'connected'
    rt.auth_relay.pending['st1'] = {'session': 'one', 'port': 59999, 'expires': 1e18}
    anonymous = TestClient(TestServer(client.server.app))
    async with anonymous:
        response = await anonymous.get('/oidc/callback?state=st1&code=abc', allow_redirects=False)
    assert response.status in (302, 401)
    assert 'st1' in rt.auth_relay.pending


@pytest.mark.asyncio
async def test_remote_session_replays_through_forward(relayed):
    client, rt, port, hits, _ = relayed
    rt.sessions['one']['target'] = {'type': 'ssh'}
    seen = []

    async def forward(name, remote_port):
        seen.append((name, remote_port))
        return {'port': port}
    rt.forward = forward
    await register(client, 8250)
    assert (await client.get('/oidc/callback?state=st1&code=abc')).status == 200
    assert seen == [('one', 8250)]
    assert hits == [('GET', '/oidc/callback?state=st1&code=abc')]


# --- CLI -------------------------------------------------------------------

import sys as _sys

import cli

URL = auth_url('cli-state')


def fake_login(exit_code=0, extra=''):
    script = f"print('Complete the login:'); print(); print('  {URL}'); {extra}raise SystemExit({exit_code})"
    return [_sys.executable, '-c', script]


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, 'current_session', lambda explicit=None: {'session': explicit or 'one', 'pane': None})
    monkeypatch.setattr(cli, 'invoke', lambda session, op, args: seen.append((session, op, args)) or {})
    return seen


def run_relay(argv):
    return cli.run_auth_relay(cli.auth_relay_parser().parse_args(argv))


def test_cli_registers_then_opens_captured_url_and_propagates_exit(calls, capsys):
    code = run_relay(['--port', '8250', '--', *fake_login(3)])
    assert code == 3
    assert calls == [('one', 'auth-register', {'port': 8250, 'url': URL}),
                     ('one', 'open', {'url': URL, 'target': 'local'})]
    assert URL in capsys.readouterr().out


def test_cli_acts_on_first_url_only(calls):
    run_relay(['--', *fake_login(0, extra=f"print('{URL}'); ")])
    assert [op for _, op, _ in calls] == ['auth-register', 'open']


def test_cli_default_port_is_8250(calls):
    run_relay(['--', *fake_login()])
    assert calls[0][2]['port'] == 8250


def test_cli_ignores_urls_without_state(calls):
    code = run_relay(['--', _sys.executable, '-c', "print('docs at https://openbao.org/docs')"])
    assert code == 0 and calls == []


def test_cli_registration_failure_is_reported_and_command_still_finishes(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'current_session', lambda explicit=None: {'session': 'one', 'pane': None})
    seen = []

    def invoke(session, op, args):
        seen.append(op)
        raise RuntimeError('redirect_uri must be https://x/oidc/callback')
    monkeypatch.setattr(cli, 'invoke', invoke)
    code = run_relay(['--', *fake_login(0)])
    assert code == 0
    assert seen == ['auth-register']
    assert 'redirect_uri must be' in capsys.readouterr().err


def test_cli_requires_a_command(calls):
    with pytest.raises(SystemExit):
        run_relay(['--port', '8250'])
