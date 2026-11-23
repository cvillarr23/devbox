import json

import pytest

import cli
import devbox_store as store
from test_dashboard import dashboard  # noqa: F401  (fixture)


async def login(client, **headers):
    response = await client.post('/login', json={'token': client.server.app['token']}, headers=headers)
    assert response.status == 200
    return response.headers['Set-Cookie']


@pytest.mark.asyncio
@pytest.mark.parametrize('env, headers, secure', [
    ({}, {}, False),
    ({}, {'X-Forwarded-Proto': 'https'}, False),
    ({'DEVBOX_TRUST_PROXY': '1'}, {'X-Forwarded-Proto': 'https'}, True),
    ({'DEVBOX_TRUST_PROXY': '1'}, {'X-Forwarded-Proto': 'https, http'}, True),
    ({'DEVBOX_TRUST_PROXY': '1'}, {'X-Forwarded-Proto': 'http'}, False),
    ({'DEVBOX_TRUST_PROXY': '1'}, {}, False),
    ({'DEVBOX_SECURE_COOKIE': '1'}, {}, True),
])
async def test_cookie_secure_flag(dashboard, monkeypatch, env, headers, secure):  # noqa: F811
    client, _ = dashboard
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cookie = await login(client, **headers)
    assert ('Secure' in cookie) is secure


@pytest.mark.asyncio
async def test_public_url_in_config_and_session_list(dashboard, monkeypatch):  # noqa: F811
    client, _ = dashboard
    monkeypatch.delenv('DEVBOX_PUBLIC_URL', raising=False)
    assert '"publicHost": ""' in await (await client.get('/')).text()
    assert 'view_url' not in (await (await client.get('/api/sessions')).json())[0]
    monkeypatch.setenv('DEVBOX_PUBLIC_URL', 'https://devbox-next.roo-net.cc/')
    assert '"publicHost": "https://devbox-next.roo-net.cc"' in await (await client.get('/')).text()
    assert (await (await client.get('/api/sessions')).json())[0]['view_url'] == 'https://devbox-next.roo-net.cc/view/one'


def test_devbox_session_json_view_url(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'current_session', lambda explicit=None: {'session': 'one', 'pane': '%1'})
    monkeypatch.setattr('sys.argv', ['cli.py', 'devbox-session', '--json'])
    monkeypatch.delenv('DEVBOX_PUBLIC_URL', raising=False)
    cli.main()
    assert 'view_url' not in json.loads(capsys.readouterr().out)
    monkeypatch.setenv('DEVBOX_PUBLIC_URL', 'https://devbox-next.roo-net.cc')
    cli.main()
    assert json.loads(capsys.readouterr().out)['view_url'] == 'https://devbox-next.roo-net.cc/view/one'


def test_state_dir_default(monkeypatch):
    monkeypatch.delenv('DEVBOX_STATE_DIR', raising=False)
    assert store.state_dir() == '/data'
