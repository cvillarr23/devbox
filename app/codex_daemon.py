"""Read only session metadata from Codex's local Unix WebSocket endpoint."""
import base64
import hashlib
import json
import os
import socket
import stat
import struct
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_cache = (0, [])


def _exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = sock.recv(size - len(chunks))
        if not data:
            raise OSError('Codex socket closed')
        chunks.extend(data)
    return bytes(chunks)


def _send(sock, obj, opcode=1):
    payload = json.dumps(obj).encode() if opcode == 1 else obj
    mask = os.urandom(4)
    size = len(payload)
    length = bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack('!H', size)
    sock.sendall(bytes([0x80 | opcode]) + length + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _receive(sock):
    fragments = bytearray()
    while True:
        head = _exact(sock, 2)
        size = head[1] & 127
        if size == 126:
            size = struct.unpack('!H', _exact(sock, 2))[0]
        elif size == 127:
            size = struct.unpack('!Q', _exact(sock, 8))[0]
        if size > 8_000_000 or len(fragments) + size > 8_000_000:
            raise ValueError('Oversized Codex response')
        mask = _exact(sock, 4) if head[1] & 128 else None
        payload = _exact(sock, size)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        opcode = head[0] & 15
        if opcode == 8:
            raise OSError('Codex socket closed')
        if opcode == 9:
            _send(sock, payload, opcode=10)
            continue
        if opcode in (0, 1):
            fragments.extend(payload)
            if head[0] & 128:
                return json.loads(fragments)


def _request(sock, ident, method, params):
    _send(sock, {'id': ident, 'method': method, 'params': params})
    for _ in range(200):
        message = _receive(sock)
        if message.get('id') == ident:
            if 'error' in message:
                raise ValueError('Codex metadata request failed')
            return message['result']
    raise ValueError('Too many Codex notifications')


def fetch(path):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                      'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n').encode())
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            header.extend(_exact(sock, 1))
            if len(header) > 8192:
                raise ValueError('Invalid WebSocket handshake')
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if b' 101 ' not in header.split(b'\r\n')[0] or expected not in header:
            raise ValueError('Codex WebSocket upgrade failed')
        _request(sock, 1, 'initialize', {'clientInfo': {'name': 'devbox-status', 'version': '0.1.0'},
                                       'capabilities': {'experimentalApi': True}})
        _send(sock, {'method': 'initialized'})
        records, cursor, ident = [], None, 2
        for _ in range(10):
            result = _request(sock, ident, 'thread/list', {'limit': 100, 'cursor': cursor,
                              'archived': False, 'useStateDbOnly': True})
            # Do not retain titles, previews, turns, transcript paths, or messages.
            records.extend({k: t.get(k) for k in ('id', 'cwd', 'status', 'createdAt', 'updatedAt')}
                           for t in result.get('data', []) if t.get('status', {}).get('type') != 'notLoaded')
            cursor = result.get('nextCursor')
            if not cursor:
                break
            ident += 1
        return records


def threads():
    global _cache
    if os.environ.get('DEVBOX_PROC_ROOT', '/proc') != '/proc':
        return []
    with _lock:
        if time.monotonic() - _cache[0] < 2:
            return _cache[1]
        try:
            state = Path(os.environ.get('DEVBOX_STATE_DIR', '/var/lib/dev-workspace')) / 'sessions'
            uid = state.stat().st_uid if state.exists() else os.getuid()
            configured = os.environ.get('DEVBOX_CODEX_SOCKET')
            paths = [Path(configured)] if configured else list(Path(f'/tmp/codex-daemon-{uid}').glob('*'))
            sockets = [p for p in paths if stat.S_ISSOCK(p.stat().st_mode)]
            records = fetch(sockets[0]) if len(sockets) == 1 else []
        except (OSError, ValueError, KeyError, TypeError):
            records = []
        _cache = time.monotonic(), records
        return records


def match(process, all_processes, records):
    exact = [t for t in records if process.get('resumed') and t['id'] == process['resumed']]
    if exact:
        return exact[0] if len(exact) == 1 else None
    same_cwd = [p for p in all_processes if p['agent'] == 'codex' and p['cwd'] == process['cwd']]
    candidates = [t for t in records if t['cwd'] == process['cwd']]
    if len(same_cwd) != 1:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Daemons retain idle loaded threads after their terminal client exits.
    # A fresh client can only create its initial thread after it starts.
    if process.get('fresh') and process.get('started_at'):
        created = [t for t in candidates if (t.get('createdAt') or 0) >= process['started_at'] - 2]
        return created[0] if len(created) == 1 else None
    return None


def state(thread):
    value = thread.get('status', {})
    if value.get('type') == 'active':
        return 'waiting' if set(value.get('activeFlags', [])) & {'waitingOnApproval', 'waitingOnUserInput'} else 'working'
    return 'idle' if value.get('type') == 'idle' else 'unavailable'
