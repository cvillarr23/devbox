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
    assert restored.to_restore == ['one']


@pytest.mark.asyncio
async def test_restore_recreates_local_sessions_only(state, monkeypatch):
    rt = runtime.Runtime()
    for name, kind in (('one', 'local'), ('two', 'remote')):
        rt.sessions[name] = {'name': name, 'port': 7690, 'connection': 'connected', 'environment': kind,
                             'target': {'type': kind}, 'cwd': str(state), 'started_at': 1}
    rt.save()
    restored = runtime.Runtime()
    calls = []

    async def ensure(name, target, cwd):
        calls.append(('ensure', name))
    monkeypatch.setattr(restored, 'ensure', ensure)
    monkeypatch.setattr(runtime.child_session, 'restore_session', lambda name, cwd: calls.append(('zellij', name)))
    await restored.restore()
    assert calls == [('ensure', 'one'), ('zellij', 'one')]
    assert restored.to_restore == []


@pytest.mark.asyncio
async def test_graceful_shutdown_marks_connected_sessions_for_restore(state):
    rt = runtime.Runtime()
    rt.sessions['one'] = {'name': 'one', 'port': 7690, 'connection': 'connected', 'environment': 'local', 'target': {'type': 'local'}, 'cwd': str(state), 'started_at': 1}
    rt.sessions['two'] = {**rt.sessions['one'], 'name': 'two', 'connection': 'disconnected'}
    await rt.close()
    rt.save()
    assert runtime.Runtime().to_restore == ['one']


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
        assert (await client.delete('/api/browser', headers=auth)).status == 200
        rt.kill = AsyncMock()
        assert (await client.delete('/api/sessions/one', headers=auth)).status == 204
        rt.kill.assert_awaited_once_with('one')


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
        assert 'plugins/devbox/skills/devbox-browser/SKILL.md' in tar.getnames()
        assert {'messages.py', 'pane_input.py', 'plugins/devbox/skills/devbox-msg/SKILL.md', '.claude-plugin/marketplace.json'} <= set(tar.getnames())


def test_browser_vnc_fits_whole_desktop():
    b = Browser()
    assert b.xvfb_command()[:5] == ['Xvfb', ':99', '-screen', '0', '1440x900x24']
    assert '-xrandr' not in b.x11vnc_command()
    js = (Path(__file__).parent.parent / 'app/web/browser-panel.js').read_text()
    assert '/browser/vnc.html?autoconnect=true&resize=scale&path=browser/websockify' in js
    assert 'resize=remote' not in js


def _kill_runtime(state, states, links):
    import devbox_store as store
    rt = runtime.Runtime()
    for name in ('pm', 'a', 'b', 'c', 'other'):
        rt.sessions[name] = {'name': name, 'port': 1, 'connection': 'stopped', 'environment': 'local',
                             'target': {'type': 'local'}, 'cwd': str(state), 'started_at': 1}
        store.update_meta(name, {'parent': links[name]} if name in links else {})
    rt.save()
    rt.status = lambda n: {'state': states.get(n, 'working')}
    rt.kill = AsyncMock()
    return rt


@pytest.mark.asyncio
async def test_kill_guards_unknown_and_self(state):
    rt = _kill_runtime(state, {}, {'other': 'pm'})
    with pytest.raises(ValueError, match='--self'):
        await rt.operation('pm', 'kill', {'names': ['pm']})
    with pytest.raises(ValueError, match='Unknown session'):
        await rt.operation('pm', 'kill', {'names': ['nope']})
    rt.kill.assert_not_awaited()
    assert (await rt.operation('pm', 'kill', {'names': ['other']})) == {'ended': ['other']}
    rt.kill.assert_awaited_once_with('other')


@pytest.mark.asyncio
async def test_kill_self_replies_before_ending(state):
    rt = _kill_runtime(state, {}, {})
    result = await rt.operation('pm', 'kill', {'names': ['pm'], 'self': True, 'delay': 0})
    assert result == {'ended': ['pm']}
    rt.kill.assert_not_awaited()
    await asyncio.gather(*rt._deferred) if rt._deferred else await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    rt.kill.assert_awaited_once_with('pm')


@pytest.mark.asyncio
async def test_kill_children_filters_by_parent_and_state(state, monkeypatch):
    import messages
    rt = _kill_runtime(state, {'a': 'done', 'b': 'working', 'c': 'idle', 'other': 'done'},
                       {'a': 'pm', 'b': 'pm', 'c': 'pm:2'})
    monkeypatch.setattr(messages, 'pending', lambda n, pane, urgent_only=False: [1] if n == 'c' else [])
    assert await rt.operation('pm', 'kill', {'children': True, 'dry_run': True}) == {'would_end': ['a']}
    rt.kill.assert_not_awaited()
    assert await rt.operation('pm', 'kill', {'children': True}) == {'ended': ['a']}
    rt.kill.assert_awaited_once_with('a')


@pytest.mark.asyncio
async def test_kill_restriction(state):
    rt = _kill_runtime(state, {}, {'a': 'pm', 'b': 'a'})
    with pytest.raises(ValueError, match='not a child'):
        await rt.operation('pm', 'kill', {'names': ['other']})
    assert await rt.operation('pm', 'kill', {'names': ['a', 'b']}) == {'ended': ['a', 'b']}
    assert await rt.operation('pm', 'kill', {'names': ['other']}, restricted=False) == {'ended': ['other']}
    assert (await rt.operation('pm', 'kill', {'names': ['pm'], 'self': True, 'delay': 0}))['ended'] == ['pm']
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_socket_kill_of_non_child_refused_without_client_flag(state, tmp_path):
    rt = _kill_runtime(state, {}, {'a': 'pm'})
    app = server.create_app(rt)
    app['socket'] = str(tmp_path / 's.sock')
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, app['socket']).start()
    import aiohttp
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=app['socket'])) as http:
        r = await http.post('http://x/api/sessions/pm/kill', json={'names': ['other']})
        assert r.status == 400 and 'not a child' in (await r.text())
        r = await http.post('http://x/api/sessions/pm/kill', json={'names': ['a']})
        assert r.status == 200
    rt.kill.assert_awaited_once_with('a')
    await runner.cleanup()
