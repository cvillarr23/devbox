import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devbox_store as store  # noqa: E402

ME = os.getpid()


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class EventMappingTest(unittest.TestCase):
    def test_mapping(self):
        cases = [
            ({"hook_event_name": "SessionStart", "source": "startup"}, "idle"),
            ({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}, "working"),
            ({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, "working"),
            ({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, "working"),
            ({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, "waiting"),
            ({"hook_event_name": "Notification", "notification_type": "permission_prompt"}, "waiting"),
            ({"hook_event_name": "Notification", "notification_type": "elicitation_dialog"}, "waiting"),
            ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, None),
            ({"hook_event_name": "Stop", "stop_hook_active": False}, "done"),
            ({"hook_event_name": "Interrupt", "turn_id": "t"}, "idle"),
            ({"hook_event_name": "SessionEnd", "reason": "other"}, "end"),
            ({"hook_event_name": "PreCompact"}, None),
            ({}, None),
        ]
        for payload, want in cases:
            self.assertEqual(store.event_state(payload), want, payload)


class AgentStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DEVBOX_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["DEVBOX_STATE_DIR"]

    def ev(self, name, pane="0", agent="claude", pid=ME, **extra):
        store.record_agent_event("s1", pane, agent, dict(hook_event_name=name, **extra), pid)

    def test_no_agents_is_idle(self):
        self.assertEqual(store.session_status("s1"), {"state": "idle", "agents": []})

    def test_record_and_since_preserved(self):
        self.ev("UserPromptSubmit")
        first = store.session_status("s1")["agents"][0]
        self.assertEqual((first["agent"], first["state"], first["pane"]), ("claude", "working", "0"))
        self.ev("PreToolUse")
        self.assertEqual(store.session_status("s1")["agents"][0]["since"], first["since"])
        self.ev("PermissionRequest")
        a = store.session_status("s1")["agents"][0]
        self.assertEqual(a["state"], "waiting")
        self.assertGreaterEqual(a["since"], first["since"])

    def test_ignored_event_changes_nothing(self):
        self.ev("Stop")
        self.ev("Notification", notification_type="idle_prompt")
        self.assertEqual(store.session_status("s1")["agents"][0]["state"], "done")

    def test_session_end_removes(self):
        self.ev("UserPromptSubmit")
        self.ev("SessionEnd", reason="other")
        self.assertEqual(store.session_status("s1")["agents"], [])

    def test_aggregate_most_urgent_wins(self):
        self.ev("Stop", pane="1")
        self.assertEqual(store.session_status("s1")["state"], "done")
        self.ev("UserPromptSubmit", pane="2", agent="codex")
        self.assertEqual(store.session_status("s1")["state"], "working")
        self.ev("PermissionRequest", pane="3")
        self.assertEqual(store.session_status("s1")["state"], "waiting")
        self.assertEqual([a["pane"] for a in store.session_status("s1")["agents"]], ["1", "2", "3"])

    def test_ack_clears_done_only(self):
        self.ev("Stop", pane="1")
        self.ev("UserPromptSubmit", pane="2")
        store.ack_done("s1")
        st = store.session_status("s1")
        self.assertEqual(st["state"], "working")
        by_pane = {a["pane"]: a for a in st["agents"]}
        self.assertTrue(by_pane["1"]["acked"])
        self.assertFalse(by_pane["2"]["acked"])
        self.ev("SessionEnd", pane="2")
        self.assertEqual(store.session_status("s1")["state"], "idle")

    def test_new_done_is_unacked_again(self):
        self.ev("Stop")
        store.ack_done("s1")
        self.ev("UserPromptSubmit")
        self.ev("Stop")
        self.assertEqual(store.session_status("s1")["state"], "done")

    def test_dead_pid_pruned(self):
        self.ev("UserPromptSubmit", pane="1", pid=dead_pid())
        self.ev("Stop", pane="2")
        st = store.session_status("s1")
        self.assertEqual([a["pane"] for a in st["agents"]], ["2"])
        self.assertFalse(os.path.exists(os.path.join(store.session_dir("s1"), "agents", "1.json")))

    def test_bad_pane_rejected(self):
        with self.assertRaises(ValueError):
            store.record_agent_event("s1", "../x", "claude", {"hook_event_name": "Stop"}, ME)
        with self.assertRaises(ValueError):
            store.record_agent_event("s1", "0", "gemini", {"hook_event_name": "Stop"}, ME)


if __name__ == "__main__":
    unittest.main()
