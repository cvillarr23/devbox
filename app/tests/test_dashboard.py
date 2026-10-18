"""Host dashboard regressions adapted to the standalone HTTP gateway."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import server


@pytest_asyncio.fixture
async def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    rt = runtime.Runtime()
    rt.sessions['one'] = {'name': 'one', 'port': 7697, 'connection': 'stopped',
                          'environment': 'local', 'target': {'type': 'local'},
                          'cwd': str(tmp_path), 'started_at': 123}
    rt.ensure = AsyncMock(side_effect=AssertionError('Listing must not start a terminal'))
    rt.save()
    app = server.create_app(rt)
    async with TestClient(TestServer(app), headers={'Authorization': 'Bearer ' + app['token']}) as client:
        yield client, rt


@pytest.mark.asyncio
async def test_pages_and_shared_modules(dashboard):
    client, _ = dashboard
    for route, expected in [('/', 'session-cards.js'), ('/view/one', 'client-mode.js'),
                            ('/static/session-cards.js', 'devboxCards'),
                            ('/static/client-mode.js', 'devboxClientMode')]:
        response = await client.get(route)
        assert response.status == 200
        assert expected in await response.text()
    assert (await client.get('/view/unknown')).status == 404


@pytest.mark.asyncio
async def test_mobile_uses_registered_backend(dashboard):
    client, _ = dashboard
    response = await client.get('/mobile/one?backend=https://example.com')
    assert response.status == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.content_type == 'text/html'
    body = await response.text()
    assert '<meta name="devbox-terminal-base" content="/s/7">' in body
    assert 'https://example.com' not in body
    for name in ('unknown', '%3Cscript%3E'):
        assert (await client.get('/mobile/' + name)).status == 404


@pytest.mark.asyncio
async def test_missing_mobile_bundle_offers_desktop_recovery(dashboard, tmp_path, monkeypatch):
    client, _ = dashboard
    monkeypatch.setattr(server, 'WEB', tmp_path)
    response = await client.get('/mobile/one')
    assert response.status == 503
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Select Desktop' in await response.text()


@pytest.mark.asyncio
async def test_listing_is_read_only_and_includes_card_details(dashboard, tmp_path):
    client, rt = dashboard
    before = rt.path.read_bytes()
    for route in ('/api/sessions', '/api/sessions/one'):
        response = await client.get(route)
        assert response.status == 200
        data = await response.json()
        entry = data[0] if isinstance(data, list) else data
        assert entry['name'] == 'one'
        assert entry['port'] == 7697
        assert entry['started_at'] == 123
        assert entry['folders'] == [str(tmp_path)]
        assert entry['repositories'] == []
        assert entry['agent_names'] == []
        assert entry['last_activity_at'] is None
    rt.ensure.assert_not_called()
    assert rt.children == {}
    assert rt.path.read_bytes() == before
