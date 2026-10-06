import asyncio
import json
from pathlib import Path
import pytest
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock

from browser import Browser, validate_url
import environments
import runtime
import server


def test_environment_validation(state):
    path = state / 'environments.yaml'
    path.write_text('environments:\n  host:\n    type: ssh\n    host: "-oProxyCommand=bad"\n')
    with pytest.raises(ValueError):
        environments.load()
    path.write_text('environments:\n  host:\n    type: ssh\n    host: my-server\n')
    assert environments.load()['host']['host'] == 'my-server'


def test_ssh_command_arguments_are_quoted():
    command = environments.transport({'type': 'ssh', 'host': 'server'}, ['printf', '$(touch /tmp/unsafe)'])
    assert command[-1] == "printf '$(touch /tmp/unsafe)'"


@pytest.mark.asyncio
async def test_ambiguous_pods_rejected(monkeypatch):
    pod = {'metadata': {'name': 'a', 'uid': '1'}, 'status': {'phase': 'Running', 'containerStatuses': [{'name': 'main', 'ready': True}]}}
    monkeypatch.setattr(environments, 'run', AsyncMock(return_value=json.dumps({'items': [pod, pod]}).encode()))
    with pytest.raises(ValueError, match='found 2'):
        await environments.resolve({'type': 'kubernetes', 'context': 'c', 'namespace': 'n', 'selector': 'app=x', 'container': 'main'})


@pytest.mark.asyncio
async def test_local_link_claim_and_ack():
    browser = Browser()
    result = await browser.open('one', 'https://example.com/path', 'local')
    assert result['status'] == 'pending'
    ident = result['id']
    assert browser.claim('one', ident, 'viewer-1')['claimed']
    assert not browser.claim('one', ident, 'viewer-2')['claimed']
    assert not browser.claim('one', ident, 'viewer-1')['claimed']
    browser.ack('one', ident, 'viewer-1', 'blocked')
    assert browser.pending('one', 'viewer-1', True)['links'][0]['status'] == 'blocked'
    browser.ack('one', ident, 'viewer-1', 'opened')
    assert not browser.pending('one', 'viewer-1', True)['links']
    with pytest.raises(ValueError):
        browser.claim('other', ident, 'viewer-1')


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:pass@example.com', 'https://example.com\n--flag'])
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.asyncio
async def test_restart_does_not_resume_processes(state):
    rt = runtime.Runtime()
    rt.sessions['one'] = {'name': 'one', 'port': 7690, 'connection': 'connected', 'environment': 'local', 'target': {'type': 'local'}, 'cwd': str(state), 'started_at': 1}
    rt.save()
    restored = runtime.Runtime()
    assert restored.sessions['one']['connection'] == 'stopped'
    assert restored.children == {}


@pytest.mark.asyncio
async def test_gateway_auth_csrf_and_session_notes(state):
    rt = runtime.Runtime()
    rt.sessions['one'] = {'name': 'one', 'port': 7690, 'connection': 'stopped', 'environment': 'local', 'target': {'type': 'local'}, 'cwd': str(state), 'started_at': 1}
    rt.save()
    app = server.create_app(rt)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get('/healthz')).status == 200
        assert (await client.get('/api/sessions')).status == 401
        auth = {'Authorization': 'Bearer ' + app['token']}
        r = await client.get('/api/sessions', headers=auth)
        assert r.status == 200
        assert (await r.json())[0]['connection'] == 'stopped'
        r = await client.post('/api/sessions/one/open', headers={**auth, 'Origin': 'https://evil.example'}, json={'url': 'https://example.com', 'target': 'local'})
        assert r.status == 403
        r = await client.get('/api/sessions/one/notes', headers=auth)
        version = (await r.json())['version']
        assert (await client.put('/api/sessions/one/notes', headers=auth, json={'content': 'first', 'base_version': version})).status == 200
        assert (await client.put('/api/sessions/one/notes', headers=auth, json={'content': 'second', 'base_version': version})).status == 409
        assert (await client.get('/static/%2e%2e/server.py', headers=auth)).status == 404
        assert (await client.get('/s/0/', headers=auth)).status == 503
        r = await client.post('/api/sessions/one/open', headers=auth, json={'url': 'https://example.com', 'target': 'local'})
        assert (await r.json())['status'] == 'pending'
        assert (await client.post('/api/sessions/one/open', headers=auth, json={'url': 'javascript:alert(1)', 'target': 'remote'})).status == 400


@pytest.mark.asyncio
async def test_remote_bootstrap_program_compiles(state, monkeypatch):
    import bootstrap
    captured = {}
    async def fake_run(argv, data=None, timeout=None):
        captured['program'] = argv[-1]
        captured['data'] = data
        return b'ok'
    monkeypatch.setattr(environments, 'run', fake_run)
    await bootstrap.bootstrap({'type': 'local'})
    compile(captured['program'], '<bootstrap>', 'exec')
    import base64,io,tarfile
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(captured['data']))) as tar:
        assert 'agent_process.py' in tar.getnames()
        assert 'skills/devbox-browser/SKILL.md' in tar.getnames()
