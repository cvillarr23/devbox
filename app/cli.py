#!/usr/bin/env python3
"""Agent-facing CLI. Uses a private Unix socket locally and a relay remotely."""
import argparse
import http.client
import json
import os
import re
import socket
import subprocess
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


def open_url(session, url, target):
    return invoke(session, 'open', {'url': url, 'target': target})


def notes_command(session, action, content=''):
    return invoke(session, 'notes', {'action': action, 'content': content})


def current_session(explicit=None):
    from current_session import detect, SessionDetectionError
    try:
        return detect(explicit)
    except SessionDetectionError as exc:
        raise ValueError(str(exc)) from None


def doctor_session(explicit):
    """Detection is optional here: doctor is what you run when detection may be broken."""
    try:
        return current_session(explicit)['session']
    except ValueError:
        if explicit:
            raise
        return None


def run_doctor(args):
    """Remote targets go through the relay when a session is known, else report the target itself.

    The relay only serves requests from a session the server knows, so with no
    session the target's own diagnostics are the most it can give.
    """
    if os.environ.get('DEVBOX_REMOTE') != '1':
        return request('/api/doctor')
    session = doctor_session(args.session)
    if session:
        return invoke(session, 'doctor', {})
    import diagnostics
    return {**diagnostics.report(), 'session': None}


def with_view_url(result):
    import devbox_store
    view_url = devbox_store.public_view_url(result['session'])
    return {**result, 'view_url': view_url} if view_url else result


def spawn_request(args):
    prompt = sys.stdin.read() if args.prompt == '-' else args.prompt
    fields = {'name': args.name, 'environment': args.environment, 'cwd': args.cwd, 'worktree': args.worktree,
              'agent': args.agent, 'model': args.model, 'permission_mode': args.permission_mode, 'prompt': prompt,
              'origin_cwd': os.getcwd()}
    return {k: v for k, v in fields.items() if v}


def spawn_session(data):
    """Spawn a child of the detected session; with no session, a top-level one."""
    try:
        parent = current_session()
    except ValueError:
        parent = None
    if parent:
        return invoke(parent['session'], 'spawn', {**data, 'pane': parent['pane']})
    if os.environ.get('DEVBOX_REMOTE') == '1':
        raise ValueError('No devbox session; run inside one')
    return request('/api/spawn', data)


def run_spawn(args):
    result = spawn_session(spawn_request(args))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result['name'])
        print(result['url'])


AUTH_URL_RE = re.compile(r'https?://\S+')


def auth_relay_parser():
    parser = argparse.ArgumentParser(prog='devbox auth-relay', usage='devbox auth-relay [--port N] [--session S] -- COMMAND...')
    parser.add_argument('--port', type=int, default=8250, help="loopback port the command's callback listener uses")
    parser.add_argument('--session', help='override automatic detection')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    return parser


def _auth_url(line):
    """The authorization URL on a line of the command's output, or None."""
    match = AUTH_URL_RE.search(line)
    return match.group(0) if match and 'state=' in match.group(0) else None


def _relay_login(session, port, url):
    """Register the pending login, then open its URL in the viewer's browser."""
    try:
        invoke(session, 'auth-register', {'port': port, 'url': url})
    except (ValueError, RuntimeError) as exc:
        print(f'devbox: cannot relay this login: {exc}', file=sys.stderr, flush=True)
        return
    invoke(session, 'open', {'url': url, 'target': 'local'})


def run_auth_relay(args):
    """Run a loopback-callback login with its callback relayed through devbox's public URL."""
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise SystemExit('devbox auth-relay: give the login command after --')
    session = current_session(args.session)['session']
    relayed = False
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            print(line, end='', flush=True)
            url = None if relayed else _auth_url(line)
            if url:
                relayed = True
                _relay_login(session, args.port, url)
    return proc.returncode


MSG_USAGE = """devbox-msg send [TO] [--to TO] [--kind KIND] [--urgent | --quiet] TEXT|-
    TO is SESSION, SESSION:PANE, parent, children or siblings.
    --urgent nudges an idle receiver; --quiet only waits in its inbox.
devbox-msg config SESSION [--nudge KIND,...] [--quiet KIND,...] [--reset]
    Which kinds nudge SESSION's idle agents; no options shows the effective set.
devbox-msg inbox [--unread] [--follow] [--json]
devbox-msg ack ID... | --all
devbox-msg who [--json]"""


def _kinds(text):
    return [k.strip() for k in text.split(',') if k.strip()]


def _msg_parser(prog):
    import messages
    parser = argparse.ArgumentParser(prog=prog, usage=MSG_USAGE)
    sub = parser.add_subparsers(dest='action', required=True)
    send = sub.add_parser('send')
    send.add_argument('words', nargs='+', metavar='[TO] TEXT')
    send.add_argument('--to')
    send.add_argument('--kind', choices=messages.KINDS, default='update')
    urgency = send.add_mutually_exclusive_group()
    urgency.add_argument('--urgent', dest='urgency', action='store_const', const='urgent')
    urgency.add_argument('--quiet', dest='urgency', action='store_const', const='quiet')
    config = sub.add_parser('config')
    config.add_argument('session')
    config.add_argument('--nudge', type=_kinds)
    config.add_argument('--quiet', type=_kinds)
    config.add_argument('--reset', action='store_true')
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


def msg_send(to, kind, body, urgency=None):
    """Send as the detected session; the sender is never caller-supplied."""
    session, pane = _msg_identity()
    return invoke(session, 'msg-send', {'from_pane': pane, 'to': to, 'kind': kind, 'body': body,
                                        **({'urgency': urgency} if urgency else {})})


def _msg_send(args):
    to, text = _msg_text(args)
    result = msg_send(to, args.kind, text, args.urgency)
    for item in result['delivered']:
        print(f"{item['to']}: " + (f"#{item['id']} " if item['id'] else '') + item['delivery'])


def _msg_config(args):
    session, _ = _msg_identity()
    result = invoke(session, 'msg-config', {'session': args.session, 'nudge': args.nudge,
                                            'quiet': args.quiet, 'reset': args.reset})
    print(f"{result['session']}: nudges on {', '.join(result['nudges']) or 'nothing'}"
          + (f"  (session setting: {json.dumps(result['setting'])})" if result['setting'] else ''))


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
    {'send': _msg_send, 'config': _msg_config, 'inbox': _msg_inbox, 'ack': _msg_ack, 'who': _msg_who}[args.action](args)


def main():
    command = sys.argv[1]
    if command == 'devbox-msg' or (command == 'devbox' and sys.argv[2:3] == ['msg']):
        return msg_main(sys.argv[2:] if command == 'devbox-msg' else sys.argv[3:],
                        'devbox-msg' if command == 'devbox-msg' else 'devbox msg')
    if command == 'devbox' and sys.argv[2:3] == ['auth-relay']:
        sys.exit(run_auth_relay(auth_relay_parser().parse_args(sys.argv[3:])))
    parser = argparse.ArgumentParser(prog=command)
    if command == 'devbox-open':
        parser.add_argument('--target', choices=('local', 'remote'), default=os.environ.get('DEVBOX_OPEN_TARGET', 'remote'))
        parser.add_argument('--session')
        parser.add_argument('url')
        args = parser.parse_args(sys.argv[2:])
        args.session = current_session(args.session)['session']
        result = open_url(args.session, args.url, args.target)
    elif command == 'devbox-session':
        parser.add_argument('--session', help='override automatic detection')
        parser.add_argument('--json', action='store_true', help='include pane and detection source')
        args = parser.parse_args(sys.argv[2:])
        result = current_session(args.session)
        if args.json:
            result = with_view_url(result)
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
        result = notes_command(args.session, args.action, content)
        if args.action == 'cat':
            print(result['content'], end='')
            return
    else:
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('doctor').add_argument('--session', help='override automatic detection')
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
            result = run_doctor(args)
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
