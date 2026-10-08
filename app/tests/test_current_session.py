import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_process
import current_session


class CurrentSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.tmp.name}
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def entry(self, name, thread="thread-1", pid=None, start=None):
        pid = os.getpid() if pid is None else pid
        path = Path(self.tmp.name) / "sessions" / name / "agents" / "3.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agent": "codex", "session_id": thread, "state": "idle",
                                   "pid": pid, "pid_start": agent_process.identity(pid) if start is None else start}))

    def test_explicit_session_overrides_everything(self):
        os.environ["ZELLIJ_SESSION_NAME"] = "env-session"
        self.assertEqual(current_session.detect("chosen")["session"], "chosen")

    def test_zellij_environment(self):
        os.environ.update(ZELLIJ_SESSION_NAME="pane-session", ZELLIJ_PANE_ID="2")
        self.assertEqual(current_session.detect(), {"session": "pane-session", "pane": "2", "source": "environment"})

    def test_relay_session_precedes_zellij(self):
        os.environ.update(DEVBOX_SESSION="relayed", ZELLIJ_SESSION_NAME="remote-zellij")
        self.assertEqual(current_session.detect(), {"session": "relayed", "pane": None, "source": "devbox-environment"})

    def test_agent_cli_reports_detected_session(self):
        os.environ["ZELLIJ_SESSION_NAME"] = "pane-session"
        result = subprocess.run([sys.executable, str(ROOT / "cli.py"), "devbox-session", "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["source"], "environment")
        os.environ.pop("ZELLIJ_SESSION_NAME")
        result = subprocess.run([sys.executable, str(ROOT / "cli.py"), "devbox-session"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--session NAME", result.stderr)

    def test_attached_claude_session_precedes_launch_environment(self):
        os.environ.update(CLAUDE_CODE_SESSION_ID="claude-1", ZELLIJ_SESSION_NAME="launch")
        with patch.object(current_session.store, "attached_pane", return_value=("attached", "7")):
            self.assertEqual(current_session.detect()["session"], "attached")

    def test_exact_codex_thread_maps_to_registered_pane(self):
        self.entry("core-131")
        os.environ["CODEX_THREAD_ID"] = "thread-1"
        self.assertEqual(current_session.detect(), {"session": "core-131", "pane": "3", "source": "agent-registry"})

    def test_ambiguous_thread_fails(self):
        self.entry("one")
        self.entry("two")
        os.environ["CODEX_THREAD_ID"] = "thread-1"
        with self.assertRaisesRegex(current_session.SessionDetectionError, "ambiguous"):
            current_session.detect()

    def test_stale_process_and_reused_pid_do_not_match(self):
        for pid, start in ((99999999, 1), (os.getpid(), -1)):
            self.entry("stale", pid=pid, start=start)
            os.environ["CODEX_THREAD_ID"] = "thread-1"
            with self.assertRaises(current_session.SessionDetectionError):
                current_session.detect()

    def test_no_cwd_or_only_session_guess(self):
        self.entry("only")
        with self.assertRaises(current_session.SessionDetectionError):
            current_session.detect()

    def test_invalid_name_is_rejected(self):
        with self.assertRaises(current_session.SessionDetectionError):
            current_session.detect("../bad")

    def test_cli_and_notes_share_detection(self):
        self.entry("core-131")
        os.environ["CODEX_SESSION_ID"] = "thread-1"
        result = subprocess.run([sys.executable, str(ROOT / "devbox-session"), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["session"], "core-131")
        append = subprocess.run([sys.executable, str(ROOT / "devbox-notes"), "append", "detected"], capture_output=True, text=True)
        self.assertEqual(append.returncode, 0, append.stderr)
        self.assertEqual((Path(self.tmp.name) / "sessions/core-131/notes.md").read_text(), "detected\n")

    def test_status_show_shares_detection(self):
        self.entry("core-131")
        os.environ["CODEX_THREAD_ID"] = "thread-1"
        result = subprocess.run([sys.executable, str(ROOT / "devbox-status"), "show"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_detection_does_not_create_notes(self):
        result = subprocess.run([sys.executable, str(ROOT / "devbox-notes"), "append", "wrong place"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.tmp.name) / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
