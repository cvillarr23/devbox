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


def current_session(explicit=None):
    from current_session import detect, SessionDetectionError
    try:
        return detect(explicit)
    except SessionDetectionError as exc:
        raise ValueError(str(exc)) from None


def spawn_request(args):
    prompt = sys.stdin.read() if args.prompt == '-' else args.prompt
    fields = {'name': args.name, 'environment': args.environment, 'cwd': args.cwd, 'worktree': args.worktree,
              'agent': args.agent, 'model': args.model, 'permission_mode': args.permission_mode, 'prompt': prompt,
              'origin_cwd': os.getcwd()}
    return {k: v for k, v in fields.items() if v}


def run_spawn(args):
    """Spawn a child of the detected session; with no session, a top-level one."""
    data = spawn_request(args)
    try:
        parent = current_session()
    except ValueError:
        parent = None
    if parent:
        result = invoke(parent['session'], 'spawn', {**data, 'pane': parent['pane']})
    elif os.environ.get('DEVBOX_REMOTE') == '1':
        raise ValueError('No devbox session; run inside one')
    else:
        result = request('/api/spawn', data)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result['name'])
        print(result['url'])


MSG_USAGE = """devbox-msg send [TO] [--to TO] [--kind KIND] TEXT|-
    TO is SESSION, SESSION:PANE, parent, children or siblings.
devbox-msg inbox [--unread] [--follow] [--json]
devbox-msg ack ID... | --all
devbox-msg who [--json]"""


def _msg_parser(prog):
    import messages
    parser = argparse.ArgumentParser(prog=prog, usage=MSG_USAGE)
    sub = parser.add_subparsers(dest='action', required=True)
    send = sub.add_parser('send')
    send.add_argument('words', nargs='+', metavar='[TO] TEXT')
    send.add_argument('--to')
    send.add_argument('--kind', choices=messages.KINDS, default='update')
    inbox = sub.add_parser('inbox')
    inbox.add_argument('--unread', action='store_true')
    inbox.add_argument('--follow', action='store_true')
    inbox.add_argument('--json', action='store_true')
    ack = sub.add_parser('ack')
    ack.add_argument('ids', nargs='*', type=int)
    ack.add_argument('--all', action='store_true')
    sub.add_parser('who').add_argument('--json', action='store_true')
    return parser


def _msg_identity():
    """Sender identity comes only from detection; send has no --from or --session."""
    me = current_session()
    return me['session'], me['pane'] or os.environ.get('ZELLIJ_PANE_ID')


def _msg_text(args):
    """`send parent hi` and `send --to parent hi` both work; `-` reads stdin."""
    to, words = (args.to, args.words) if args.to else (args.words[0], args.words[1:])
    if not words:
        raise ValueError('give TO and TEXT, or --to TO TEXT')
    return to, sys.stdin.read() if words == ['-'] else ' '.join(words)


def _msg_send(args):
    session, pane = _msg_identity()
    to, text = _msg_text(args)
    result = invoke(session, 'msg-send', {'from_pane': pane, 'to': to, 'kind': args.kind, 'body': text})
    for item in result['delivered']:
        print(f"{item['to']}: " + (f"#{item['id']} " if item['id'] else '') + item['delivery'])


def _msg_inbox(args):
    import messages
    import time
    session, pane = _msg_identity()
    shown = set()
    while True:
        for message in messages.inbox(session, pane, unread=args.unread):
            if message['id'] not in shown:
                shown.add(message['id'])
                print(json.dumps(message) if args.json else ('  ' if message['read_at'] else '* ') + messages.render(message), flush=True)
        if not args.follow:
            return
        time.sleep(1)


def _msg_ack(args):
    import messages
    if not args.all and not args.ids:
        raise ValueError('give message ids or --all')
    session, pane = _msg_identity()
    acked = messages.ack(session, pane, None if args.all else args.ids)
    print('acked ' + (', '.join(f'#{i}' for i in acked) if acked else 'nothing'))


def _msg_who(args):
    session, pane = _msg_identity()
    result = invoke(session, 'msg-who', {'from_pane': pane})
    if args.json:
        print(json.dumps(result, indent=2))
        return
    rows = {r['name']: r for r in result['sessions']}
    lines = [('me', result['me']), ('parent', result['parent'])]
    lines += [('child', c) for c in result['children']] + [('sibling', s) for s in result['siblings']]
    for role, address in lines:
        row = rows.get((address or '').partition(':')[0], {})
        status = row.get('status', 'gone' if address else '')
        unread = f" {row['unread']} unread" if row.get('unread') else ''
        print(f"{role:8} {address or '-':28} {status}{unread}")


def msg_main(argv, prog='devbox-msg'):
    if os.environ.get('DEVBOX_REMOTE') == '1':
        # The inbox is read from this target's own helper state, like the hooks write it.
        os.environ.setdefault('DEVBOX_STATE_DIR', str(Path.home() / '.local/state/devbox'))
    parser = _msg_parser(prog)
    # `send parent --kind question TEXT` leaves TEXT after an option, which
    # argparse hands back as extras rather than as more `words`.
    args, extras = parser.parse_known_args(argv)
    if extras and (args.action != 'send' or any(e.startswith('-') and e != '-' for e in extras)):
        parser.error('unrecognized arguments: ' + ' '.join(extras))
    if extras:
        args.words += extras
    {'send': _msg_send, 'inbox': _msg_inbox, 'ack': _msg_ack, 'who': _msg_who}[args.action](args)


def main():
    command = sys.argv[1]
    if command == 'devbox-msg' or (command == 'devbox' and sys.argv[2:3] == ['msg']):
        return msg_main(sys.argv[2:] if command == 'devbox-msg' else sys.argv[3:],
                        'devbox-msg' if command == 'devbox-msg' else 'devbox msg')
    parser = argparse.ArgumentParser(prog=command)
    if command == 'devbox-open':
        parser.add_argument('--target', choices=('local', 'remote'), default=os.environ.get('DEVBOX_OPEN_TARGET', 'remote'))
        parser.add_argument('--session')
        parser.add_argument('url')
        args = parser.parse_args(sys.argv[2:])
        args.session = current_session(args.session)['session']
        result = invoke(args.session, 'open', {'url': args.url, 'target': args.target})
    elif command == 'devbox-session':
        parser.add_argument('--session', help='override automatic detection')
        parser.add_argument('--json', action='store_true', help='include pane and detection source')
        args = parser.parse_args(sys.argv[2:])
        result = current_session(args.session)
        print(json.dumps(result) if args.json else result['session'])
        return
    elif command == 'devbox-notes':
        parser.add_argument('--session')
        parser.add_argument('action', choices=('cat', 'append', 'write', 'path'))
        parser.add_argument('text', nargs='?')
        args = parser.parse_args(sys.argv[2:])
        args.session = current_session(args.session)['session']
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
        forward.add_argument('--session')
        spawn = sub.add_parser('spawn', help='start a child session running an agent')
        spawn.add_argument('--name')
        spawn.add_argument('--environment')
        where = spawn.add_mutually_exclusive_group()
        where.add_argument('--cwd')
        where.add_argument('--worktree', metavar='BRANCH', help='create a git worktree for BRANCH from the current repo')
        spawn.add_argument('--agent', choices=('claude', 'codex'))
        spawn.add_argument('--model')
        spawn.add_argument('--permission-mode', choices=('plan', 'auto'))
        spawn.add_argument('--prompt', metavar='TEXT|-', help='initial prompt; - reads stdin')
        spawn.add_argument('--json', action='store_true')
        args = parser.parse_args(sys.argv[2:])
        if args.command == 'spawn':
            return run_spawn(args)
        if args.command == 'doctor':
            result = invoke(current_session()['session'], 'doctor', {}) if os.environ.get('DEVBOX_REMOTE') == '1' else request('/api/doctor')
        elif args.command == 'forward':
            result = invoke(current_session(args.session)['session'], 'forward', {'port': args.port})
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
