#!/usr/bin/env python3
"""Same-origin HTTP/WebSocket gateway for a single-user devbox."""
import asyncio
import contextlib
import html
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
import access_auth
import auth_relay
import bao_login
import bootstrap
import devbox_store as store
import environments
import diagnostics
import session_details
from runtime import Runtime

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
COOKIE = 'devbox_session'


@web.middleware
async def boundary(request, handler):
    # The Unix socket is accessible only to the devbox Unix user. HTTP always
    # requires a token unless explicitly placed behind a trusted auth proxy; the
    # policy comes from the listener the connection arrived on (see _policy).
    policy = _policy(request)
    if policy != 'unix' and request.path not in _unauthenticated_paths(policy):
        denied = await _denied(request, policy)
        if denied:
            return denied
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text='Cross-origin requests are not allowed')
        if (request.method not in ('GET', 'HEAD') and
                (request.method != 'DELETE' or request.can_read_body) and
                request.content_type != 'application/json'):
            raise web.HTTPUnsupportedMediaType(text='Use application/json')
    try:
        response = await handler(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response
    except store.Conflict as exc:
        return web.json_response({'error': 'notes changed', 'content': exc.content, 'version': exc.version}, status=409)
    except (ValueError, store.InvalidMeta, KeyError, TypeError) as exc:
        return web.json_response({'error': str(exc)}, status=400)
    except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
        return web.json_response({'error': str(exc) or 'Operation timed out'}, status=503)


def _policy(request):
    """The auth policy for this connection: 'unix', 'token', 'proxy' or 'cloudflare-access'.

    Chosen only by the local socket the connection was accepted on, never by a
    header: DEVBOX_TOKEN_PORT always uses token auth, the main port its mode."""
    sockname = request.transport.get_extra_info('sockname')
    if sockname == request.app['socket']:
        return 'unix'
    if request.app['token_port'] is not None and isinstance(sockname, tuple) and sockname[1] == request.app['token_port']:
        return 'token'
    return request.app['auth']


def _unauthenticated_paths(policy):
    """Access mode has no token form, so `/login` must not be reachable around the JWT check."""
    return ('/healthz',) if policy == 'cloudflare-access' else ('/healthz', '/login')


async def _denied(request, policy):
    """The response that rejects an unauthenticated request, or None if it may proceed."""
    if policy == 'cloudflare-access':
        if await request.app['access'].verify(request, request.app['client']):
            return None
        return _access_denied(request)
    token = request.app['token'] if policy == 'token' else ''
    cookie = request.cookies.get(COOKIE, '')
    bearer = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if token and not (secrets.compare_digest(cookie, token) or secrets.compare_digest(bearer, token)):
        if request.path.startswith('/api/'):
            return web.json_response({'error': 'Authentication required'}, status=401)
        raise web.HTTPFound('/login')
    return None


def _access_denied(request):
    if request.path.startswith(('/api/', '/s/', '/browser/')):
        return web.json_response({'error': 'Authentication required'}, status=401)
    return web.Response(status=401, content_type='text/html', text=(
        '<!doctype html><meta name="viewport" content="width=device-width"><title>Devbox</title>'
        '<h1>Devbox</h1><p>Sign in through Cloudflare Access.</p>'))


async def login(request):
    # Proxy auth has no token to type, so a tab or form left over from token mode goes to the dashboard.
    if _policy(request) == 'proxy':
        raise web.HTTPSeeOther('/')
    if request.method == 'GET':
        return web.Response(text='''<!doctype html><meta name="viewport" content="width=device-width"><title>Devbox login</title>
<h1>Devbox</h1><form><label>Access token <input type="password" required autocomplete="current-password"></label><button>Sign in</button></form><p role="alert"></p>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.querySelector('input').value})});if(r.ok)location.href='/';else document.querySelector('p').textContent='Invalid token';};</script>''', content_type='text/html')
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    data = await request.json()
    # The token is checked only where token auth applies; elsewhere it stays '' as before the token port.
    token = request.app['token'] if 'token' in (_policy(request), request.app['auth']) else ''
    if not isinstance(data.get('token'), str) or not secrets.compare_digest(data['token'], token):
        raise web.HTTPUnauthorized()
    response = web.json_response({'ok': True})
    response.set_cookie(COOKIE, token, httponly=True, samesite='Strict',
                        secure=_cookie_secure(request))
    return response


def _cookie_secure(request):
    """Trusting X-Forwarded-Proto is opt-in: any client can send that header."""
    if request.scheme == 'https' or os.environ.get('DEVBOX_SECURE_COOKIE') == '1':
        return True
    if os.environ.get('DEVBOX_TRUST_PROXY') != '1':
        return False
    return request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip().lower() == 'https'


def page(name):
    config = {'portStart': 7690, 'publicHost': store.public_base_url(), 'tailnetHost': '',
              'bao': bool(os.environ.get('DEVBOX_BAO_ADDR', '').strip())}
    return web.Response(text=(WEB / name).read_text().replace('__DEVBOX_CONFIG__', json.dumps(config)),
                        content_type='text/html', headers={'Cache-Control': 'no-store'})


async def view(request):
    if request.match_info['name'] not in request.app['runtime'].sessions:
        raise web.HTTPNotFound()
    if request.path.startswith('/mobile/'):
        entry = request.app['runtime'].sessions[request.match_info['name']]
        try:
            text = (WEB / 'vendor/driftty/index.html').read_text()
        except FileNotFoundError:
            return web.Response(status=503, text='Mobile client is not installed. Select Desktop in the view toolbar.',
                                headers={'Cache-Control': 'no-store'})
        # Use only the registered backend, never a client-supplied URL.
        return web.Response(text=text.replace('<head>', f'<head><meta name="devbox-terminal-base" content="/s/{entry["port"]-7690}">', 1),
                            content_type='text/html', headers={'Cache-Control': 'no-store'})
    return page('view.html')


async def static(request):
    path = (WEB / request.match_info['path']).resolve()
    if not path.is_relative_to(WEB) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def proxy(request):
    rt = request.app['runtime']
    if request.path.startswith('/browser/'):
        if not rt.browser.status()['running']:
            raise web.HTTPServiceUnavailable(text='Start the browser from the Browser panel')
        port, path = 6080, '/' + request.match_info['path']
    else:
        slot = int(request.match_info['slot'])
        if not 0 <= slot < 100:
            raise web.HTTPNotFound()
        port = 7690 + slot
        entry = next((v for v in rt.sessions.values() if v['port'] == port), None)
        if not entry or entry['connection'] != 'connected':
            raise web.HTTPServiceUnavailable(text='Session is disconnected. Reconnect it from its page.')
        path = request.path
    url = f'http://127.0.0.1:{port}{path}'
    if request.query_string:
        url += '?' + request.query_string
    client = request.app['client']
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        protocols = [p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
        async with client.ws_connect(url, protocols=protocols, max_msg_size=16*1024*1024) as upstream:
            downstream = web.WebSocketResponse(protocols=protocols, max_msg_size=16*1024*1024)
            await downstream.prepare(request)
            async def relay(source, dest):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await dest.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await dest.send_bytes(message.data)
                    elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
            tasks = [asyncio.create_task(relay(upstream, downstream)), asyncio.create_task(relay(downstream, upstream))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await downstream.close()
            return downstream
    async with client.request(request.method, url, data=await request.read(), allow_redirects=False) as upstream:
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in ('content-type', 'cache-control')}
        return web.Response(body=await upstream.read(), status=upstream.status, headers=headers)


async def sessions(request):
    rt = request.app['runtime']
    if request.method == 'POST':
        data = await request.json()
        name = data.get('name', '')
        if not store.valid_name(name):
            raise ValueError('Use 1–64 letters, numbers, underscores or hyphens for the name')
        if name in rt.sessions:
            raise ValueError('Session already exists; use Reconnect')
        entry = await rt.ensure(name, data.get('environment', 'local'), data.get('cwd', ''))
        metadata = {k: data[k] for k in ('title', 'description', 'color') if k in data}
        if metadata:
            store.update_meta(name, metadata)
        return web.json_response(public_entry(rt, name))
    return web.json_response([public_entry(rt, name) for name in rt.sessions])


def public_entry(rt, name):
    entry = rt.sessions[name]
    meta = store.read_meta(name)
    meta['cwd'] = entry['cwd']
    status = rt.status(name)
    if entry['target']['type'] == 'local':
        details = session_details.details(name, meta, status, entry)
    else:
        details = {'started_at': entry['started_at'], 'last_activity_at': max(
            (a.get('updated_at', 0) for a in status['agents']), default=None),
            'agent_names': sorted({a['agent'] for a in status['agents']}),
            'folders': [entry['cwd']], 'repositories': []}
    view_url = store.public_view_url(name)
    return {**{k: entry[k] for k in ('name', 'port', 'environment', 'connection')},
            **meta, **details, 'status': status['state'], 'unread': status.get('unread', 0),
            **({'view_url': view_url} if view_url else {}),
            **({'cwd_missing': True} if entry['target']['type'] == 'local' and not os.path.isdir(entry['cwd']) else {}),
            **({'resume': status['resume']} if status.get('resume') else {})}


async def session(request):
    rt, name = request.app['runtime'], request.match_info['name']
    if name not in rt.sessions:
        raise web.HTTPNotFound()
    action = request.match_info.get('action', '')
    data = await request.json() if request.can_read_body else {}
    method = request.method
    if not action:
        if method == 'GET':
            return web.json_response(public_entry(rt, name))
        if method == 'DELETE':
            await rt.kill(name)
            return web.Response(status=204)
    if action == 'reconnect' and method == 'POST':
        entry = rt.sessions[name]
        if entry['connection'] == 'connected':
            raise ValueError('Session is already connected')
        await rt.disconnect(name)
        await rt.ensure(name, entry['environment'], entry['cwd'])
        return web.json_response(public_entry(rt, name))
    if action == 'meta':
        if method == 'GET':
            return web.json_response({**store.read_meta(name), 'cwd': rt.sessions[name]['cwd']})
        if method == 'PATCH':
            if 'cwd' in data:
                cwd = data.pop('cwd')
                if rt.sessions[name]['target']['type'] == 'local':
                    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
                        raise ValueError('Working directory does not exist')
                else:
                    if name not in rt.remotes:
                        raise ValueError('Reconnect before changing the remote working directory')
                    cwd = (await rt.remotes[name].call({'operation': 'check', 'cwd': cwd}))['cwd']
                rt.sessions[name]['cwd'] = cwd
                rt.save()
            return web.json_response({**store.update_meta(name, data), 'cwd': rt.sessions[name]['cwd']})
    if action == 'notes':
        if method == 'GET':
            content, version = store.read_notes(name)
            return web.json_response({'content': content, 'version': version})
        if method == 'PUT':
            if not isinstance(data.get('content'), str):
                raise ValueError('content must be a string')
            version = store.write_notes(name, data['content'], base_version=data.get('base_version'))
            return web.json_response({'version': version})
    if action == 'status' and method == 'GET':
        return web.json_response(rt.status(name))
    if action == 'status/ack' and method == 'POST':
        return web.json_response(await rt.ack(name))
    if action == 'links' and method == 'GET':
        return web.json_response(rt.browser.pending(name, request.query.get('viewer'), request.query.get('active') == '1'))
    if action in ('links/claim', 'links/ack') and method == 'POST':
        if action.endswith('claim'):
            result = rt.browser.claim(name, data['id'], data['viewer'])
        else:
            result = rt.browser.ack(name, data['id'], data['viewer'], data['status'])
        return web.json_response(result)
    if action in ('open', 'forward', 'notes-command', 'doctor', 'spawn', 'kill', 'msg-send', 'msg-config', 'msg-who', 'auth-register') and method == 'POST':
        return web.json_response(await rt.operation(name, 'notes' if action == 'notes-command' else action, data,
                                                    restricted=_policy(request) == 'unix'))
    raise web.HTTPNotFound()


def _relay_page(status, message):
    """Static text only: callback query values are never reflected."""
    return web.Response(status=status, content_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}, text=(
        '<!doctype html><meta name="viewport" content="width=device-width"><title>Devbox</title>'
        f'<h1>Devbox</h1><p>{message}</p>'))


def _log_callback(state, flow, outcome):
    """One line per callback: a state prefix and the outcome, never the code, token or full state."""
    print(f'oidc-callback state={state[:6] or "-"} flow={flow} outcome={outcome}', flush=True)


async def auth_callback(request):
    """Finish a dashboard OpenBao login, or replay a relay login to its session's loopback port."""
    state = request.query.get('state', '')
    bao = request.app['bao']
    if bao is not None and bao.owns(state):
        return await _bao_callback(request, bao, state)
    rt = request.app['runtime']
    try:
        name, port = rt.auth_relay.claim(state)
    except auth_relay.Unknown:
        _log_callback(state, 'relay', 'unknown')
        return _relay_page(404, 'No pending login matches this request. Start the login again from the session.')
    except auth_relay.Expired:
        _log_callback(state, 'relay', 'expired')
        return _relay_page(410, 'This login expired. Start it again from the session.')
    if name not in rt.sessions or rt.sessions[name]['connection'] != 'connected':
        _log_callback(state, 'relay', 'disconnected')
        return _relay_page(503, 'The session is not connected. Reconnect it and start the login again.')
    url = f'http://127.0.0.1:{await rt.loopback_port(name, port)}{auth_relay.CALLBACK_PATH}'
    if request.query_string:
        url += '?' + request.query_string
    try:
        async with request.app['client'].get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as upstream:
            ok = 200 <= upstream.status < 300
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        ok = False
    _log_callback(state, 'relay', 'replayed' if ok else 'failed')
    if ok:
        return _relay_page(200, 'Signed in. You can close this tab.')
    return _relay_page(502, 'The session did not accept the login. Start it again from the session.')


async def _bao_callback(request, bao, state):
    try:
        nonce = bao.claim(state)
    except auth_relay.Expired:
        _log_callback(state, 'bao', 'expired')
        return _relay_page(410, 'This OpenBao login expired (logins last 5 minutes). Start it again from the dashboard.')
    except auth_relay.Unknown:
        # Raced with another tab using the same link.
        _log_callback(state, 'bao', 'unknown')
        return _relay_page(404, 'This OpenBao login was already used. Start it again from the dashboard.')
    try:
        ttl = await bao.finish(request.app['client'], state, nonce, request.query)
    except bao_login.BaoError as exc:
        _log_callback(state, 'bao', 'failed')
        return _relay_page(502, 'OpenBao login failed: ' + html.escape(str(exc)))
    except OSError as exc:
        _log_callback(state, 'bao', 'write-failed')
        return _relay_page(500, 'Signed in, but the token could not be saved: ' + html.escape(exc.strerror or type(exc).__name__))
    _log_callback(state, 'bao', 'claimed')
    expiry = f' It expires in {ttl // 60} min.' if isinstance(ttl, int) and ttl > 0 else ''
    return _relay_page(200, 'Logged in to OpenBao. The token is in ~/.vault-token for every session.'
                       + expiry + ' You can close this tab.')


def _dashboard_only(request):
    """Agents reach the server over the Unix socket; these routes are for a signed-in browser."""
    if _policy(request) == 'unix':
        raise web.HTTPForbidden(text='Only the dashboard can do this')
    if request.method != 'GET' and not request.headers.get('Origin'):
        raise web.HTTPForbidden(text='Origin header required')


async def bao_status(request):
    _dashboard_only(request)
    bao = request.app['bao']
    if bao is None:
        return web.json_response({'enabled': False})
    try:
        return web.json_response(await bao.status(request.app['client']))
    except bao_login.BaoError as exc:
        return web.json_response({'enabled': True, 'error': str(exc)})


async def bao_start(request):
    _dashboard_only(request)
    bao = request.app['bao']
    if bao is None:
        raise web.HTTPNotFound()
    base = store.public_base_url()
    if not base:
        raise ValueError('DEVBOX_PUBLIC_URL is not set; devbox has no public callback')
    try:
        url = await bao.start(request.app['client'], bao_login.redirect_uri(base))
    except bao_login.BaoError as exc:
        return web.json_response({'error': str(exc)}, status=502)
    return web.json_response({'auth_url': url}, headers={'Cache-Control': 'no-store'})


async def spawn_top_level(request):
    return web.json_response(await request.app['runtime'].spawn(None, await request.json()))


async def browser(request):
    browser = request.app['runtime'].browser
    if request.method == 'POST':
        await browser.start()
    elif request.method == 'DELETE':
        await browser.stop()
    return web.json_response(browser.status())


async def targets(request):
    rt = request.app['runtime']
    if request.method == 'GET':
        return web.json_response([{'name': n, 'type': c['type']} for n, c in rt.targets.items()])
    name = request.match_info['name']
    config = await environments.resolve(rt.targets[name])
    if config['type'] == 'local':
        raise ValueError('Local environment is installed with the image')
    return web.json_response({'message': await bootstrap.bootstrap(config)})


async def doctor(request):
    rt = request.app['runtime']
    report = await asyncio.to_thread(diagnostics.report)
    return web.json_response({**report, 'environments': list(rt.targets), 'sessions': [
        {'name': n, 'connection': rt.status(n)['connection'], 'agents': [
            {k: a.get(k) for k in ('agent', 'pane', 'state', 'source', 'updated_at', 'pid', 'pid_start')}
            for a in rt.status(n)['agents']]} for n in rt.sessions],
        'hooks': {agent: Path(path).exists() for agent, path in (
            ('codex', '/etc/codex/requirements.toml'), ('claude', '/etc/claude-code/managed-settings.json'))}})


async def lifecycle(app):
    app['client'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
    task = asyncio.create_task(app['runtime'].poll())
    restore = asyncio.create_task(app['runtime'].restore())
    yield
    for pending in (restore, task):
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
    await app['runtime'].close()
    await app['client'].close()


def create_app(runtime=None):
    app = web.Application(middlewares=[boundary], client_max_size=2*1024*1024)
    app['runtime'] = runtime or Runtime()
    app['socket'] = os.environ.get('DEVBOX_SOCKET', '/tmp/devbox-control.sock')
    token_file = Path(os.environ.get('DEVBOX_TOKEN_FILE', str(Path(store.state_dir()) / 'access-token')))
    auth = os.environ.get('DEVBOX_AUTH_MODE', 'token')
    if auth not in ('token', 'proxy', 'cloudflare-access'):
        raise ValueError('DEVBOX_AUTH_MODE must be token, proxy or cloudflare-access')
    app['auth'] = auth
    app['token_port'] = _token_port()
    app['bao'] = bao_login.from_env()
    if auth == 'cloudflare-access':
        app['access'] = access_auth.CloudflareAccess.from_env()
    if auth == 'token' or app['token_port'] is not None:
        if not token_file.exists():
            token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32))
        app['token'] = token_file.read_text().strip()
        if len(app['token']) < 24:
            raise ValueError('Access token must have at least 24 characters')
    else:
        app['token'] = ''
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/healthz', lambda _: web.json_response({'ok': True}))
    app.router.add_route('*', '/login', login)
    app.router.add_get('/', lambda _: page('index.html'))
    app.router.add_get('/view/{name}', view)
    app.router.add_get('/mobile/{name}', view)
    app.router.add_get('/static/{path:.*}', static)
    app.router.add_get(auth_relay.CALLBACK_PATH, auth_callback)
    app.router.add_route('*', '/s/{slot:\\d+}/{path:.*}', proxy)
    app.router.add_route('*', '/browser/{path:.*}', proxy)
    app.router.add_get('/api/widgets', lambda _: web.json_response(sorted(p.name for p in (WEB / 'widgets').glob('*.js'))))
    app.router.add_post('/api/spawn', spawn_top_level)
    app.router.add_route('*', '/api/sessions', sessions)
    app.router.add_route('*', '/api/sessions/{name}', session)
    app.router.add_route('*', '/api/sessions/{name}/{action:.*}', session)
    app.router.add_route('*', '/api/browser', browser)
    app.router.add_get('/api/environments', targets)
    app.router.add_post('/api/environments/{name}/bootstrap', targets)
    app.router.add_get('/api/doctor', doctor)
    app.router.add_get('/api/bao', bao_status)
    app.router.add_post('/api/bao/login', bao_start)
    return app


def _token_port():
    """DEVBOX_TOKEN_PORT as an int, or None when unset or empty."""
    value = os.environ.get('DEVBOX_TOKEN_PORT', '')
    if not value:
        return None
    if not value.isdigit() or not 0 < int(value) < 65536:
        raise ValueError('DEVBOX_TOKEN_PORT must be a TCP port number')
    if int(value) == int(os.environ.get('DEVBOX_PORT', '7680')):
        raise ValueError('DEVBOX_TOKEN_PORT must differ from DEVBOX_PORT')
    return int(value)


async def main():
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    socket_path = Path(app['socket'])
    socket_path.unlink(missing_ok=True)
    unix = web.UnixSite(runner, str(socket_path))
    await unix.start()
    socket_path.chmod(0o600)
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get('DEVBOX_PORT', '7680'))).start()
    if app['token_port'] is not None:
        await web.TCPSite(runner, '0.0.0.0', app['token_port']).start()
    print('Devbox ready. Access token file: ' + os.environ.get('DEVBOX_TOKEN_FILE', str(Path(store.state_dir()) / 'access-token')), flush=True)
    if app['token_port'] is not None:
        print(f"Token-auth listener on port {app['token_port']}; keep it off public ingress.", flush=True)
    import signal
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        socket_path.unlink(missing_ok=True)


if __name__ == '__main__':
    asyncio.run(main())
