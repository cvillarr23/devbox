#!/usr/bin/env python3
"""Stdio MCP server exposing devbox session tools to agents.

A thin typed layer over cli.py: every tool calls the same function the CLI
does, so there is one code path for the business logic. The session comes from
current_session() on each call; outside a devbox session a tool returns an
error result instead of failing the protocol.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

PROTOCOL_VERSION = '2025-06-18'
SUPPORTED_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
TOOL_ERRORS = (ValueError, RuntimeError, OSError)


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, indent=2)


def _require_text(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{key} must be a non-empty string')
    return value


def notes_read(args):
    import cli
    return cli.notes_command(cli.current_session()['session'], 'cat')['content']


def notes_append(args):
    import cli
    cli.notes_command(cli.current_session()['session'], 'append', _require_text(args, 'text'))
    return 'appended'


def open_url(args):
    import cli
    session = cli.current_session()['session']
    return cli.open_url(session, _require_text(args, 'url'), args.get('target') or 'remote')


def msg_send(args):
    import cli
    import messages
    kind = args.get('kind') or 'update'
    if kind not in messages.KINDS:
        raise ValueError('kind must be one of: ' + ', '.join(messages.KINDS))
    return cli.msg_send(_require_text(args, 'to'), kind, _require_text(args, 'text'), args.get('urgency') or None)


def msg_inbox(args):
    import cli
    import messages
    if os.environ.get('DEVBOX_REMOTE') == '1':
        os.environ.setdefault('DEVBOX_STATE_DIR', str(Path.home() / '.local/state/devbox'))
    session, pane = cli._msg_identity()
    return messages.inbox(session, pane, unread=bool(args.get('unread')))


SPAWN_FIELDS = ('name', 'environment', 'cwd', 'worktree', 'agent', 'model', 'effort', 'permission_mode', 'prompt')


def session_spawn(args):
    import cli
    data = {k: args[k] for k in SPAWN_FIELDS if args.get(k)}
    return cli.spawn_session({**data, 'origin_cwd': os.getcwd()})


def session_kill(args):
    import cli
    names = args.get('names')
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError('names must be a non-empty list of session names')
    caller = cli.current_session()['session']
    result = cli.invoke(caller, 'kill', {'names': names, 'self': caller in names, 'children_only': True})
    return result


def _string(description, **extra):
    return {'type': 'string', 'description': description, **extra}


def _schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required),
            'additionalProperties': False}


def _tools():
    import messages
    return [
        ('notes_read', 'Read this devbox session\'s notepad.', _schema(), notes_read),
        ('notes_append', 'Append a line to this devbox session\'s notepad.',
         _schema({'text': _string('Text to append as a new line')}, ['text']), notes_append),
        ('open_url', 'Open a URL in the devbox viewer\'s browser or in devbox\'s remote Chrome.',
         _schema({'url': _string('http(s) URL to open'),
                  'target': _string('remote (devbox Chrome, default) or local (the viewer\'s browser)',
                                    enum=['remote', 'local'])}, ['url']), open_url),
        ('msg_send', 'Send a message to another devbox session: parent, children, siblings, '
                     'SESSION or SESSION:PANE.',
         _schema({'to': _string('SESSION, SESSION:PANE, parent, children or siblings'),
                  'text': _string('Message body'),
                  'kind': _string('Message kind; blocker, question, contract and review notify urgently',
                                  enum=list(messages.KINDS)),
                  'urgency': _string('urgent nudges an idle receiver, quiet only waits in its inbox; '
                                     'default depends on the kind and the receiver', enum=list(messages.URGENCIES))},
                 ['to', 'text']), msg_send),
        ('msg_inbox', 'List messages sent to this session or pane. Does not acknowledge them.',
         _schema({'unread': {'type': 'boolean', 'description': 'Only unread messages'}}), msg_inbox),
        ('session_spawn', 'Start a child devbox session running an agent, optionally in a new git worktree.',
         _schema({'name': _string('Session name'), 'environment': _string('Environment name'),
                  'cwd': _string('Working directory; not with worktree'),
                  'worktree': _string('Branch for a new git worktree from the current repo; not with cwd'),
                  'agent': _string('Agent to run', enum=['claude', 'codex']),
                  'model': _string('Model name'),
                  'effort': _string('Reasoning effort; claude: low, medium, high, xhigh, max; codex: minimal, low, medium, high, xhigh',
                                    enum=['minimal', 'low', 'medium', 'high', 'xhigh', 'max']),
                  'permission_mode': _string('Claude permission mode', enum=['plan', 'auto']),
                  'prompt': _string('Initial prompt for the agent')}), session_spawn),
        ('session_kill', 'End devbox sessions: your own children, or this session itself (listing its own name). '
                         'Other sessions are refused.',
         _schema({'names': {'type': 'array', 'items': {'type': 'string'},
                            'description': 'Session names to end: your children and/or this session'}},
                 ['names']), session_kill),
    ]


def tool_table():
    return {name: {'name': name, 'description': description, 'inputSchema': schema, 'handler': handler}
            for name, description, schema, handler in _tools()}


def call_tool(tools, name, arguments):
    tool = tools.get(name)
    if tool is None:
        raise KeyError(name)
    try:
        return {'content': [{'type': 'text', 'text': _text(tool['handler'](arguments))}], 'isError': False}
    except TOOL_ERRORS as exc:
        return {'content': [{'type': 'text', 'text': f'devbox: {exc}'}], 'isError': True}


def handle(tools, message):
    """Return the response for one JSON-RPC message, or None for a notification."""
    method, params, ident = message.get('method'), message.get('params') or {}, message.get('id')
    if ident is None:
        return None

    def ok(result):
        return {'jsonrpc': '2.0', 'id': ident, 'result': result}

    def error(code, text):
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': code, 'message': text}}

    if method == 'initialize':
        asked = params.get('protocolVersion')
        return ok({'protocolVersion': asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                   'capabilities': {'tools': {}}, 'serverInfo': {'name': 'devbox', 'version': '0.1.0'}})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': [{k: v for k, v in t.items() if k != 'handler'} for t in tools.values()]})
    if method == 'tools/call':
        arguments = params.get('arguments')
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            return error(-32602, 'arguments must be an object')
        try:
            return ok(call_tool(tools, params.get('name'), arguments))
        except KeyError:
            return error(-32602, f"Unknown tool: {params.get('name')}")
    return error(-32601, f'Method not found: {method}')


def serve(stdin=sys.stdin, stdout=sys.stdout):
    tools = tool_table()
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('not an object')
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            response = handle(tools, message)
        if response is not None:
            stdout.write(json.dumps(response) + '\n')
            stdout.flush()


if __name__ == '__main__':
    serve()
