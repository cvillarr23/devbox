#!/usr/bin/env python3
"""Agent-facing CLI. Uses a private Unix socket locally and a relay remotely."""
import argparse
import http.client
import json
import os
import socket
import sys
from pathlib import Path


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get('DEVBOX_SOCKET', '/tmp/devbox-control.sock'))


def request(path, data=None):
    connection = UnixHTTP('localhost', timeout=75)
    try:
        connection.request('POST' if data is not None else 'GET', path,
                           body=json.dumps(data) if data is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise RuntimeError(result.get('error', 'Request failed'))
        return result
    finally:
        connection.close()


def invoke(session, operation, args):
    if not session:
        raise ValueError('No devbox session; use --session NAME')
    if os.environ.get('DEVBOX_REMOTE') == '1':
        import remote
        os.environ['DEVBOX_SESSION'] = session
        return remote.queue_request(operation, args)
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session):
        raise ValueError('Invalid session name')
    return request(f'/api/sessions/{session}/' + ('notes-command' if operation == 'notes' else operation), args)


def current_session():
    import devbox_store
    attached = devbox_store.attached_pane(os.environ.get('CLAUDE_CODE_SESSION_ID'))
    return attached[0] if attached else os.environ.get('DEVBOX_SESSION', os.environ.get('ZELLIJ_SESSION_NAME'))


def main():
    command = sys.argv[1]
    parser = argparse.ArgumentParser(prog=command)
    if command == 'devbox-open':
        parser.add_argument('--target', choices=('local', 'remote'), default=os.environ.get('DEVBOX_OPEN_TARGET', 'remote'))
        parser.add_argument('--session', default=current_session())
        parser.add_argument('url')
        args = parser.parse_args(sys.argv[2:])
        result = invoke(args.session, 'open', {'url': args.url, 'target': args.target})
    elif command == 'devbox-notes':
        parser.add_argument('--session', default=current_session())
        parser.add_argument('action', choices=('cat', 'append', 'write', 'path'))
        parser.add_argument('text', nargs='?')
        args = parser.parse_args(sys.argv[2:])
        if args.action == 'path':
            if os.environ.get('DEVBOX_REMOTE') == '1':
                raise ValueError('Notes live in devbox; use devbox-notes cat')
            import devbox_store
            print(devbox_store.notes_path(args.session))
            return
        content = args.text if args.text not in (None, '-') else sys.stdin.read() if args.action in ('append', 'write') else ''
        result = invoke(args.session, 'notes', {'action': args.action, 'content': content})
        if args.action == 'cat':
            print(result['content'], end='')
            return
    else:
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('doctor')
        env = sub.add_parser('env').add_subparsers(dest='action', required=True)
        env.add_parser('list')
        env.add_parser('bootstrap').add_argument('name')
        forward = sub.add_parser('forward')
        forward.add_argument('port', type=int)
        forward.add_argument('--session', default=current_session())
        args = parser.parse_args(sys.argv[2:])
        if args.command == 'doctor':
            result = invoke(current_session(), 'doctor', {}) if os.environ.get('DEVBOX_REMOTE') == '1' else request('/api/doctor')
        elif args.command == 'forward':
            result = invoke(args.session, 'forward', {'port': args.port})
        elif args.action == 'list':
            result = request('/api/environments')
        else:
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.name):
                raise ValueError('Invalid environment name')
            result = request(f'/api/environments/{args.name}/bootstrap', {})
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'devbox: {exc}', file=sys.stderr)
        sys.exit(1)
