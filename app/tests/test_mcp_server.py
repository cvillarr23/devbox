import json
import os
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, APP)

import mcp_server  # noqa: E402

PHASE_1 = {'notes_read', 'notes_append', 'open_url', 'msg_send', 'msg_inbox', 'session_spawn'}


class FakeControl(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def address_string(self):
                return 'unix'

            def log_message(self, *a):
                pass

            def respond(self, body):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, data))
                if self.path.endswith('/notes-command'):
                    self.respond({'content': 'my notes\n'})
                elif self.path.endswith('/msg-send'):
                    self.respond({'delivered': [{'to': data['to'], 'id': 7, 'delivery': 'queued'}]})
                elif self.path.endswith('/spawn'):
                    self.respond({'name': 'child', 'url': 'http://x/view/child'})
                else:
                    self.respond({'ok': True})

        super().__init__(path, Handler)


@pytest.fixture
def control(tmp_path):
    server = FakeControl(str(tmp_path / 'c.sock'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def run_server(messages, tmp_path, session=True, socket=None):
    env = {'PATH': os.environ['PATH'], 'DEVBOX_STATE_DIR': str(tmp_path),
           'DEVBOX_SOCKET': socket or str(tmp_path / 'missing.sock')}
    if session:
        env.update({'ZELLIJ_SESSION_NAME': 's1', 'ZELLIJ_PANE_ID': '3'})
    stdin = ''.join(m if isinstance(m, str) else json.dumps(m) + '\n' for m in messages)
    out = subprocess.run([sys.executable, os.path.join(APP, 'mcp_server.py')], input=stdin, env=env,
                         capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    return [json.loads(line) for line in out.stdout.splitlines()]


def rpc(ident, method, **params):
    return {'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}


def call(ident, tool, **arguments):
    return rpc(ident, 'tools/call', name=tool, arguments=arguments)


def result_text(response):
    return response['result']['content'][0]['text']


def test_tool_schemas():
    tools = mcp_server.tool_table()
    assert set(tools) == PHASE_1
    for tool in tools.values():
        assert tool['description']
        schema = tool['inputSchema']
        assert schema['type'] == 'object' and schema['additionalProperties'] is False
        assert set(schema['required']) <= set(schema['properties'])
    assert tools['notes_append']['inputSchema']['required'] == ['text']
    assert tools['msg_send']['inputSchema']['required'] == ['to', 'text']
    assert 'blocker' in tools['msg_send']['inputSchema']['properties']['kind']['enum']
    assert tools['open_url']['inputSchema']['required'] == ['url']


def test_initialize_and_list(tmp_path):
    init, listed = run_server([rpc(1, 'initialize', protocolVersion='2025-03-26'),
                               {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                               rpc(2, 'tools/list')], tmp_path)
    assert init['result']['protocolVersion'] == '2025-03-26'
    assert init['result']['capabilities'] == {'tools': {}}
    assert {t['name'] for t in listed['result']['tools']} == PHASE_1
    assert all('handler' not in t for t in listed['result']['tools'])


def test_round_trip_against_control_socket(tmp_path, control):
    responses = run_server([
        call(1, 'notes_read'),
        call(2, 'notes_append', text='hello'),
        call(3, 'open_url', url='https://example.com', target='local'),
        call(4, 'msg_send', to='parent', text='done', kind='done'),
        call(5, 'session_spawn', name='child', worktree='feat/x', prompt='go'),
    ], tmp_path, socket=control.server_address)
    assert [r['result']['isError'] for r in responses] == [False] * 5
    assert result_text(responses[0]) == 'my notes\n'
    assert result_text(responses[1]) == 'appended'
    assert json.loads(result_text(responses[3]))['delivered'][0]['id'] == 7
    paths = [p for p, _ in control.requests]
    assert paths == ['/api/sessions/s1/notes-command', '/api/sessions/s1/notes-command',
                     '/api/sessions/s1/open', '/api/sessions/s1/msg-send', '/api/sessions/s1/spawn']
    sent = [d for _, d in control.requests]
    assert sent[1] == {'action': 'append', 'content': 'hello'}
    assert sent[2] == {'url': 'https://example.com', 'target': 'local'}
    assert sent[3] == {'from_pane': '3', 'to': 'parent', 'kind': 'done', 'body': 'done'}
    assert sent[4]['pane'] == '3' and sent[4]['worktree'] == 'feat/x' and sent[4]['prompt'] == 'go'
    assert 'agent' not in sent[4]


def test_msg_inbox_reads_state(tmp_path):
    import messages
    old = os.environ.get('DEVBOX_STATE_DIR')
    os.environ['DEVBOX_STATE_DIR'] = str(tmp_path)
    try:
        messages.deliver('s1', {'session': 's2', 'pane': '1'}, None, 'question', 'which port?')
    finally:
        if old is None:
            os.environ.pop('DEVBOX_STATE_DIR')
        else:
            os.environ['DEVBOX_STATE_DIR'] = old
    (response,) = run_server([call(1, 'msg_inbox', unread=True)], tmp_path)
    assert response['result']['isError'] is False
    (message,) = json.loads(result_text(response))
    assert message['body'] == 'which port?' and message['kind'] == 'question' and not message['read_at']


def test_outside_a_session_is_a_tool_error(tmp_path):
    responses = run_server([call(1, 'notes_read'), call(2, 'msg_inbox'), rpc(3, 'ping')], tmp_path, session=False)
    for response in responses[:2]:
        assert response['result']['isError'] is True
        assert 'cannot detect current devbox session' in result_text(response)
    assert responses[2]['result'] == {}


def test_bad_arguments_are_tool_errors(tmp_path):
    responses = run_server([call(1, 'notes_append'), call(2, 'msg_send', to='parent', text='x', kind='nope'),
                            call(3, 'session_spawn', cwd='/a', worktree='b')], tmp_path)
    assert all(r['result']['isError'] for r in responses)
    assert 'text must be' in result_text(responses[0])
    assert 'kind must be one of' in result_text(responses[1])


def test_protocol_errors(tmp_path):
    responses = run_server(['not json\n', rpc(1, 'nope'), call(2, 'missing'),
                            rpc(3, 'tools/call', name='notes_read', arguments=[])], tmp_path)
    assert responses[0]['error']['code'] == -32700
    assert responses[1]['error']['code'] == -32601
    assert responses[2]['error']['code'] == -32602
    assert responses[3]['error']['code'] == -32602


def test_session_spawn_schema_has_effort_enum(tmp_path):
    _, listed = run_server([rpc(1, 'initialize', protocolVersion='2025-03-26'), rpc(2, 'tools/list')], tmp_path)
    tool = next(t for t in listed['result']['tools'] if t['name'] == 'session_spawn')
    assert tool['inputSchema']['properties']['effort']['enum'] == ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']
