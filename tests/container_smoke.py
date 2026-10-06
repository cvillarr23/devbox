#!/usr/bin/env python3
"""Exercise a disposable Docker/Kubernetes devbox without printing its token."""
import argparse
import asyncio
import json
import subprocess
import uuid

import aiohttp


async def check(args):
    if args.namespace:
        command = ['kubectl', '-n', args.namespace, 'exec', 'deployment/' + args.deployment,
                   '--', 'cat', '/data/state/access-token']
    else:
        command = ['docker', 'exec', args.container, 'cat', '/data/access-token']
    base = args.url.rstrip('/')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as startup:
        for _ in range(60):
            try:
                async with startup.get(base + '/healthz') as response:
                    if response.status == 200:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.25)
        else:
            raise RuntimeError('Devbox did not become ready')
    token = subprocess.check_output(command, text=True).strip()
    exec_command = command[:-2]
    subprocess.run([*exec_command, 'codex', '--no-daemon', '--version'], check=True, capture_output=True)
    print('Codex launcher accepts explicit daemon setting: passed')
    sandbox = subprocess.run([*exec_command, 'codex', 'sandbox', '--', 'sh', '-c', 'printf CODEX_SANDBOX_OK'], check=True, capture_output=True, text=True)
    assert sandbox.stdout == 'CODEX_SANDBOX_OK'
    print('Codex sandboxed tool execution: passed')
    name = 'validation-' + uuid.uuid4().hex[:8]
    created = False
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}) as client:
        async def api(method, path, data=None):
            async with client.request(method, base + path, json=data) as response:
                if response.status == 204:
                    return None
                result = await response.json()
                if response.status >= 400:
                    raise RuntimeError(f'{method} {path}: {result.get("error", response.status)}')
                return result
        try:
            entry = await api('POST', '/api/sessions', {'name': name})
            created = True
            async with client.ws_connect(base + f'/s/{entry["port"] - 7690}/ws', protocols=['tty']) as ws:
                await ws.send_str(json.dumps({'AuthToken': '', 'columns': 100, 'rows': 30}))
                await asyncio.sleep(1)
                await ws.send_bytes(b'0devbox-notes append "terminal command executed"\r')
                for _ in range(30):
                    notes = await api('GET', f'/api/sessions/{name}/notes')
                    if 'terminal command executed' in notes['content']:
                        break
                    await asyncio.sleep(.25)
                assert 'terminal command executed' in notes['content'], notes
                print('Terminal WebSocket and notes CLI: passed')
                link = await api('POST', f'/api/sessions/{name}/open', {'target': 'local', 'url': 'https://example.com'})
                assert link['status'] == 'pending'
                claim = await api('POST', f'/api/sessions/{name}/links/claim', {'id': link['id'], 'viewer': 'smoke'})
                assert claim['claimed']
                duplicate = await api('POST', f'/api/sessions/{name}/links/claim', {'id': link['id'], 'viewer': 'another'})
                assert not duplicate['claimed']
                await api('POST', f'/api/sessions/{name}/links/ack', {'id': link['id'], 'viewer': 'smoke', 'status': 'opened'})
                print('Local link acknowledgment: passed')
                await api('POST', '/api/browser', {})
                async with client.get(base + '/browser/vnc.html') as response:
                    assert response.status == 200
                async with client.ws_connect(base + '/browser/websockify', protocols=['binary']) as vnc:
                    message = await asyncio.wait_for(vnc.receive(), 5)
                    assert message.data.startswith(b'RFB '), message
                await api('POST', f'/api/sessions/{name}/open', {'target': 'remote', 'url': 'https://example.com'})
                print('Chrome launch, remote link, and VNC handshake: passed')
            await api('DELETE', '/api/browser')
        finally:
            if created:
                await api('DELETE', f'/api/sessions/{name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:17680')
    parser.add_argument('--container', default='devbox-ci')
    parser.add_argument('--namespace')
    parser.add_argument('--deployment', default='devbox')
    asyncio.run(check(parser.parse_args()))
