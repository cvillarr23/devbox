import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import devbox_store as store  # noqa: E402

SID = "0ed0a90c-e81c-424d-ae00-3e89adf4fbeb"


def fake_proc(root, pid, argv, env, start=100):
    d = os.path.join(root, str(pid))
    os.makedirs(d)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
    with open(os.path.join(d, "environ"), "wb") as f:
        f.write(b"\0".join(("%s=%s" % kv).encode() for kv in env.items()) + b"\0")
    # comm with a space to make sure stat parsing splits after the last ")".
    fields = ["S", "1"] + ["0"] * 17 + [str(start)] + ["0"] * 5
    with open(os.path.join(d, "stat"), "w") as f:
        f.write("%d (claude x) %s\n" % (pid, " ".join(fields)))


class AttachedPaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.proc)
        os.environ["DEVBOX_PROC_ROOT"] = self.proc
        os.environ["DEVBOX_STATE_DIR"] = self.state

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["DEVBOX_PROC_ROOT"]
        del os.environ["DEVBOX_STATE_DIR"]

    def attach(self, pid, ident, session, pane, start=100, argv0="/home/developer/.local/bin/claude"):
        fake_proc(self.proc, pid, [argv0, "attach", ident],
                  {"ZELLIJ_SESSION_NAME": session, "ZELLIJ_PANE_ID": pane}, start)

    def test_matches_attach_client_by_id_prefix(self):
        self.attach(10, "0ed0a90c", "devbox", "0")
        self.assertEqual(store.attached_pane(SID), ("devbox", "0"))

    def test_newest_client_wins(self):
        self.attach(10, "0ed0a90c", "devbox", "0", start=100)
        self.attach(11, SID, "fantasy-1", "4", start=200)
        self.assertEqual(store.attached_pane(SID), ("fantasy-1", "4"))

    def test_no_match(self):
        self.attach(10, "deadbeef", "devbox", "0")
        fake_proc(self.proc, 12, ["/usr/bin/vim", "attach", "0ed0a90c"],
                  {"ZELLIJ_SESSION_NAME": "x", "ZELLIJ_PANE_ID": "1"})
        self.attach(13, "0ed0", "short", "1")  # too short to trust as a prefix
        fake_proc(self.proc, 14, ["/home/developer/.local/bin/claude", "attach", "0ed0a90c"], {})
        self.assertIsNone(store.attached_pane(SID))
        self.assertIsNone(store.attached_pane(""))
        self.assertIsNone(store.attached_pane(None))

    def test_unreadable_entries_skipped(self):
        os.makedirs(os.path.join(self.proc, "99"))  # vanished / no files
        os.makedirs(os.path.join(self.proc, "self"))
        self.attach(10, "0ed0a90c", "devbox", "2")
        self.assertEqual(store.attached_pane(SID), ("devbox", "2"))

    def test_moving_panes_drops_old_entry(self):
        me = os.getpid()
        store.record_agent_event("fantasy-pm", "0", "claude",
                                 {"hook_event_name": "UserPromptSubmit", "session_id": SID}, me)
        store.record_agent_event("devbox", "0", "claude",
                                 {"hook_event_name": "PreToolUse", "session_id": SID}, me)
        self.assertEqual(store.session_status("fantasy-pm")["agents"], [])
        self.assertEqual(store.session_status("devbox")["agents"][0]["state"], "working")
        # A different agent session in another pane is left alone.
        store.record_agent_event("fantasy-pm", "1", "codex",
                                 {"hook_event_name": "Stop", "session_id": "other"}, me)
        store.record_agent_event("devbox", "0", "claude",
                                 {"hook_event_name": "Stop", "session_id": SID}, me)
        self.assertEqual(len(store.session_status("fantasy-pm")["agents"]), 1)


class CliRoutingTest(unittest.TestCase):
    """Both CLIs report to the pane a background job is attached from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.proc)
        fake_proc(self.proc, 10, ["/home/developer/.local/bin/claude", "attach", "0ed0a90c"],
                  {"ZELLIJ_SESSION_NAME": "devbox", "ZELLIJ_PANE_ID": "0"})
        # The job itself was launched from fantasy-pm.
        self.env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.state,
                    "DEVBOX_PROC_ROOT": self.proc,
                    "ZELLIJ_SESSION_NAME": "fantasy-pm", "ZELLIJ_PANE_ID": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, name, *args, stdin="", env=None):
        return subprocess.run([sys.executable, os.path.join(HERE, "..", name), *args],
                              input=stdin, env=env or self.env, capture_output=True, text=True)

    def agents_file(self, session, pane="0"):
        return os.path.join(self.state, "sessions", session, "agents", pane + ".json")

    def test_status_follows_attach_client(self):
        r = self.run_cli("devbox-status", "hook", "--agent", "claude",
                         stdin=json.dumps({"hook_event_name": "Stop", "session_id": SID}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertTrue(os.path.exists(self.agents_file("devbox")))
        self.assertFalse(os.path.exists(self.agents_file("fantasy-pm")))

    def test_status_falls_back_to_own_pane(self):
        self.run_cli("devbox-status", "hook", "--agent", "codex",
                     stdin=json.dumps({"hook_event_name": "Stop", "session_id": "unrelated-codex-id"}))
        self.assertTrue(os.path.exists(self.agents_file("fantasy-pm")))

    def test_notes_follow_attach_client(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SID)
        self.run_cli("devbox-notes", "append", "hi", env=env)
        self.assertEqual(self.run_cli("devbox-notes", "--session", "devbox", "cat").stdout, "hi\n")
        self.assertEqual(self.run_cli("devbox-notes", "cat", env=env).stdout, "hi\n")

    def test_notes_session_flag_still_wins(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SID)
        self.run_cli("devbox-notes", "--session", "fantasy-1", "append", "x", env=env)
        self.assertEqual(self.run_cli("devbox-notes", "--session", "fantasy-1", "cat").stdout, "x\n")

    def test_notes_without_attach_use_zellij_env(self):
        self.run_cli("devbox-notes", "append", "y")
        self.assertEqual(self.run_cli("devbox-notes", "--session", "fantasy-pm", "cat").stdout, "y\n")


if __name__ == "__main__":
    unittest.main()
