#!/usr/bin/env python3
"""User-scoped remote helper. JSON-lines control and raw TCP forwarding."""
import argparse
import base64
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('DEVBOX_STATE_DIR', str(Path.home() / '.local/state/devbox'))
sys.path.insert(0, str(ROOT))
import devbox_store as store


def queue_request(operation, args):
    session = os.environ.get('DEVBOX_SESSION', os.environ.get('ZELLIJ_SESSION_NAME', ''))
    if not store.valid_name(session):
        raise ValueError('No devbox session; set DEVBOX_SESSION or use --session')
    queue = Path(store.state_dir()) / 'requests'
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = uuid.uuid4().hex
    path = queue / (ident + '.json')
    response = queue / (ident + '.response')
    store._atomic_write(str(path), json.dumps({'id': ident, 'session': session, 'operation': operation, 'args': args}))
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if response.exists():
                result = json.loads(response.read_text())
                if 'error' in result:
                    raise RuntimeError(result['error'])
                return result
            time.sleep(.1)
        raise RuntimeError('Devbox control connection is offline; request was not delivered')
    finally:
        path.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def snapshot(names, responses):
    queue = Path(store.state_dir()) / 'requests'
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    for ident, result in responses.items():
        if len(ident) == 32 and all(c in '0123456789abcdef' for c in ident):
            request = queue / (ident + '.json')
            if request.exists():
                store._atomic_write(str(queue / (ident + '.response')), json.dumps(result))
                request.unlink(missing_ok=True)
    requests = []
    for path in queue.glob('*.json'):
        if time.time() - path.stat().st_mtime > 30:
            path.unlink(missing_ok=True)
            continue
        requests.append(json.loads(path.read_text()))
    states = {name: store.session_status(name) for name in names if store.valid_name(name)}
    return {'states': states, 'requests': requests}


def attach(name, cwd):
    if not store.valid_name(name):
        raise ValueError('Invalid session name')
    if cwd:
        os.chdir(os.path.expanduser(cwd))
    else:
        os.chdir(Path.home())
    os.environ.update(DEVBOX_SESSION=name, BROWSER=str(ROOT / 'bin/devbox-open'),
                      PATH=str(ROOT / 'bin') + ':' + os.environ['PATH'])
    # Metadata and hooks use the Zellij session name on either side of the relay.
    existing = subprocess.run(['zellij', 'list-sessions', '-s', '-n'], capture_output=True, text=True).stdout.splitlines()
    args = ['zellij', '--config', str(ROOT / 'zellij.kdl')]
    args += ['attach', name] if name in existing else ['--new-session-with-layout', str(ROOT / 'layout.kdl'), '--session', name]
    os.execvp(args[0], args)


def forward(port):
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
        sock.settimeout(None)
        incoming = True
        while True:
            ready, _, _ = select.select([sock, sys.stdin.buffer] if incoming else [sock], [], [], 120)
            if not ready:
                return
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            if incoming and sys.stdin.buffer in ready:
                data = os.read(sys.stdin.fileno(), 65536)
                if data:
                    sock.sendall(data)
                else:
                    incoming = False
                    sock.shutdown(socket.SHUT_WR)


def rpc(request):
    op = request['operation']
    if op == 'hello':
        missing = [tool for tool in ('zellij', 'python3') if not shutil.which(tool)]
        if missing:
            raise ValueError('Missing dependencies: ' + ', '.join(missing))
        return {'version': 1}
    if op == 'snapshot':
        return snapshot(request.get('sessions', []), request.get('responses', {}))
    if op == 'check':
        cwd = os.path.expanduser(request.get('cwd') or str(Path.home()))
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError('Working directory does not exist on target')
        return {'cwd': cwd}
    if op == 'kill':
        name = request['session']
        if not store.valid_name(name):
            raise ValueError('Invalid session')
        subprocess.run(['zellij', 'delete-session', '-f', name], capture_output=True, timeout=10)
        return {'ok': True}
    if op == 'ack':
        store.ack_done(request['session'])
        return {'ok': True}
    raise ValueError('Unknown remote operation')


def main():
    command = sys.argv[1]
    if command == 'rpc':
        for line in sys.stdin:
            try:
                if len(line) > 2_000_000:
                    raise ValueError('Oversized request')
                result = rpc(json.loads(line))
            except Exception as exc:
                result = {'error': str(exc)}
            print(json.dumps(result), flush=True)
    elif command == 'attach':
        attach(sys.argv[2], sys.argv[3])
    elif command == 'forward':
        forward(sys.argv[2])
    else:
        raise ValueError('Unknown command')


if __name__ == '__main__':
    main()
