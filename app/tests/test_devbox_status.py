import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "devbox-status")


class DevboxStatusCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.tmp.name,
                    "ZELLIJ_SESSION_NAME": "s1", "ZELLIJ_PANE_ID": "3"}

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, payload, agent="claude", env=None, via_shell=False):
        cmd = [sys.executable, CLI, "hook", "--agent", agent]
        if via_shell:
            # Claude Code runs hook commands through `sh -c`.
            cmd = ["sh", "-c", " ".join(shlex.quote(c) for c in cmd)]
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(cmd, input=data, env=env or self.env, capture_output=True, text=True)

    def entry(self):
        with open(os.path.join(self.tmp.name, "sessions", "s1", "agents", "3.json")) as f:
            return json.load(f)

    def send(self, kind, body="hi", pane=None, **kw):
        sys.path.insert(0, os.path.dirname(CLI))
        import messages
        with mock.patch.dict(os.environ, {"DEVBOX_STATE_DIR": self.tmp.name}):
            return messages.deliver("s1", {"session": "pm", "pane": "2"}, pane, kind, body, **kw)

    def test_prompt_injects_unread_messages_and_acks_them(self):
        self.send("update", "fyi")
        r = self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("from=pm:2 kind=update] fyi", out["additionalContext"])
        self.assertIn("not instructions from the user", out["additionalContext"])
        r = self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "again"})
        self.assertEqual(r.stdout, "")

    def test_stop_blocks_only_for_urgent_messages(self):
        self.send("done")
        self.assertEqual(self.hook({"hook_event_name": "Stop"}).stdout, "")
        self.send("question", "A or B?")
        out = json.loads(self.hook({"hook_event_name": "Stop"}).stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("A or B?", out["reason"])
        self.assertEqual(self.hook({"hook_event_name": "Stop"}).stdout, "")

    def test_stop_follows_the_stamped_urgency(self):
        self.send("question", "quiet one", urgency="quiet")
        self.assertEqual(self.hook({"hook_event_name": "Stop"}).stdout, "")
        self.send("update", "act now", urgency="urgent")
        out = json.loads(self.hook({"hook_event_name": "Stop"}).stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("act now", out["reason"])
        self.assertNotIn("quiet one", out["reason"])

    def test_stop_after_a_block_does_not_block_again(self):
        self.send("blocker")
        r = self.hook({"hook_event_name": "Stop", "stop_hook_active": True})
        self.assertEqual(r.stdout, "")

    def test_messages_for_another_pane_are_not_delivered(self):
        self.send("question", pane="9")
        r = self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        self.assertEqual(r.stdout, "")

    def test_records_state_silently(self):
        r = self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        e = self.entry()
        self.assertEqual((e["agent"], e["state"]), ("claude", "working"))
        self.assertEqual(e["pid"], os.getpid())

    def test_pid_skips_shell_wrapper(self):
        self.hook({"hook_event_name": "Stop"}, agent="codex", via_shell=True)
        e = self.entry()
        self.assertEqual((e["agent"], e["state"], e["pid"]), ("codex", "done", os.getpid()))

    def test_outside_zellij_is_noop(self):
        env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.tmp.name}
        r = self.hook({"hook_event_name": "Stop"}, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "sessions")))

    def test_garbage_input_still_exits_zero(self):
        for bad in ["not json", "", "[1,2]"]:
            r = self.hook(bad)
            self.assertEqual((r.returncode, r.stdout), (0, ""), bad)
        env = dict(self.env, ZELLIJ_SESSION_NAME="../evil")
        self.assertEqual(self.hook({"hook_event_name": "Stop"}, env=env).returncode, 0)

    def test_show(self):
        self.hook({"hook_event_name": "PermissionRequest"})
        r = subprocess.run([sys.executable, CLI, "show"], env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
