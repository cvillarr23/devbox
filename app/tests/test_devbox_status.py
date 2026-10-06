import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

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
