import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_process
import devbox_store as store


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, DEVBOX_PROC_ROOT=str(self.root / 'proc'), DEVBOX_STATE_DIR=str(self.root / 'state'))
        self.env.start()
        (self.root / 'proc').mkdir()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def process(self, pid, agent='codex', session='work', pane='0', argv=None, start=100):
        root = self.root / 'proc' / str(pid)
        root.mkdir(exist_ok=True)
        (root / 'cmdline').write_bytes(('\0'.join(argv or [agent]) + '\0').encode())
        (root / 'environ').write_bytes(f'ZELLIJ_SESSION_NAME={session}\0ZELLIJ_PANE_ID={pane}\0'.encode())
        (root / 'cwd').symlink_to('/workspace')
        fields = ['S', '1'] + ['0'] * 17 + [str(start)]
        (root / 'stat').write_text(f'{pid} ({agent}) ' + ' '.join(fields))

    def test_codex_presence_without_hook(self):
        self.process(123)
        status = store.session_status('work')
        self.assertEqual(status['state'], 'unavailable')
        self.assertEqual(status['agents'][0]['agent'], 'codex')
        self.assertEqual(status['agents'][0]['pane'], '0')

    def test_shared_daemon_does_not_appear_as_terminal_agent(self):
        self.process(123, argv=['codex', 'app-server', '--managed-daemon'])
        self.assertEqual(agent_process.processes(), [])

    def test_daemon_hook_resolves_resumed_session(self):
        self.process(123, argv=['codex', 'resume', 'thread-1'])
        self.process(124, session='other', argv=['codex', 'resume', 'thread-2'])
        result = agent_process.locate('codex', {'session_id': 'thread-2', 'cwd': '/workspace'})
        self.assertEqual(result['session'], 'other')

    def test_ambiguous_cwd_is_not_assigned(self):
        self.process(123)
        self.process(124, session='other')
        self.assertIsNone(agent_process.locate('codex', {'session_id': 'new', 'cwd': '/workspace'}))

    def test_node_wrapper_deduplicates_native_child(self):
        self.process(123, argv=['node', '/usr/lib/node_modules/@openai/codex/bin/codex.js'])
        self.process(124, start=200)
        self.assertEqual([p['pid'] for p in agent_process.processes()], [124])

    def test_reused_pid_does_not_keep_stale_working_status(self):
        self.process(os.getpid(), start=100)
        store.record_agent_event('work', '0', 'codex', {'hook_event_name': 'UserPromptSubmit'}, os.getpid())
        path = self.root / 'proc' / str(os.getpid()) / 'stat'
        path.write_text(path.read_text().replace('100', '200'))
        status = store.session_status('work')
        self.assertEqual(status['state'], 'unavailable')
        self.assertEqual(status['agents'][0]['pid_start'], 200)

    def test_invalid_pid_cannot_mark_an_agent_alive(self):
        self.assertFalse(store._pid_alive(0))
        self.assertFalse(store._pid_alive(-1))

class DaemonStatusTest(ProcessTest):
    def test_daemon_working_waiting_completion_ack(self):
        import codex_daemon
        self.process(os.getpid(), argv=['codex', 'resume', 'thread-1'])
        thread = {'id': 'thread-1', 'cwd': '/workspace', 'status': {'type': 'active', 'activeFlags': []}, 'updatedAt': 1}
        with patch.object(codex_daemon, 'threads', return_value=[thread]):
            self.assertEqual(store.session_status('work')['state'], 'working')
            thread['status']['activeFlags'] = ['waitingOnApproval']
            self.assertEqual(store.session_status('work')['state'], 'waiting')
            thread['status'] = {'type': 'idle'}
            self.assertEqual(store.session_status('work')['state'], 'done')
            store.ack_done('work')
            self.assertEqual(store.session_status('work')['state'], 'idle')
            thread['status'] = {'type': 'active', 'activeFlags': []}
            self.assertEqual(store.session_status('work')['state'], 'working')

    def test_ambiguous_daemon_threads_keep_status_unavailable(self):
        import codex_daemon
        self.process(os.getpid())
        threads = [{'id': name, 'cwd': '/workspace', 'status': {'type': 'active'}} for name in ('a', 'b')]
        with patch.object(codex_daemon, 'threads', return_value=threads):
            self.assertEqual(store.session_status('work')['state'], 'unavailable')

class FreshThreadTest(unittest.TestCase):
    def test_fresh_terminal_excludes_stale_loaded_threads(self):
        import codex_daemon
        process = {'agent': 'codex', 'cwd': '/workspace', 'fresh': True, 'started_at': 1000}
        threads = [{'id': 'old', 'cwd': '/workspace', 'createdAt': 500},
                   {'id': 'new', 'cwd': '/workspace', 'createdAt': 1002}]
        self.assertEqual(codex_daemon.match(process, [process], threads)['id'], 'new')
        threads.append({'id': 'another', 'cwd': '/workspace', 'createdAt': 1005})
        self.assertIsNone(codex_daemon.match(process, [process], threads))
