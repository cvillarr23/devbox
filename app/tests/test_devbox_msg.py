import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, APP)


class DevboxMsgCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.tmp.name,
                    "ZELLIJ_SESSION_NAME": "s1", "ZELLIJ_PANE_ID": "3",
                    "DEVBOX_SOCKET": os.path.join(self.tmp.name, "missing.sock")}
        patcher = mock.patch.dict(os.environ, {"DEVBOX_STATE_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        import messages
        self.messages = messages

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, command="devbox-msg", stdin=None):
        return subprocess.run([sys.executable, os.path.join(APP, "cli.py"), command, *args],
                              input=stdin, env=self.env, capture_output=True, text=True)

    def deliver(self, kind="question", pane=None, body="A or B?"):
        return self.messages.deliver("s1", {"session": "pm", "pane": "2"}, pane, kind, body)

    def test_inbox_marks_unread_and_hides_other_panes(self):
        self.deliver()
        self.deliver(pane="9", body="not mine")
        out = self.run_cli("inbox").stdout
        self.assertIn("* [#1 ", out)
        self.assertIn("from=pm:2 kind=question] A or B?", out)
        self.assertNotIn("not mine", out)

    def test_ack_then_unread_is_empty(self):
        self.deliver()
        self.deliver()
        self.assertEqual(self.run_cli("ack", "1").stdout.strip(), "acked #1")
        self.assertEqual([json.loads(l)["id"] for l in self.run_cli("inbox", "--unread", "--json").stdout.splitlines()], [2])
        self.assertEqual(self.run_cli("ack", "--all").stdout.strip(), "acked #2")
        self.assertEqual(self.run_cli("inbox", "--unread").stdout, "")

    def test_ack_needs_ids_or_all(self):
        r = self.run_cli("ack")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--all", r.stderr)

    def test_send_has_no_sender_override(self):
        for flag in ("--from", "--session"):
            r = self.run_cli("send", "pm", flag, "other", "hi")
            self.assertEqual(r.returncode, 2)
            self.assertIn("unrecognized arguments: " + flag, r.stderr)
        self.assertIn("unrecognized", self.run_cli("inbox", "--from", "x").stderr)

    def test_urgent_and_quiet_are_mutually_exclusive(self):
        r = self.run_cli("send", "pm", "--urgent", "--quiet", "hi")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not allowed with", r.stderr)

    def test_config_rejects_unknown_options_and_reports_an_offline_server(self):
        self.assertEqual(self.run_cli("config").returncode, 2)
        r = self.run_cli("config", "pm", "--nudge", "done,update")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("devbox: "))

    def test_send_reports_an_offline_server(self):
        r = self.run_cli("send", "--to", "parent", "--kind", "question", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("devbox: "))

    def test_devbox_msg_alias_and_help(self):
        self.deliver()
        self.assertIn("A or B?", self.run_cli("msg", "inbox", command="devbox").stdout)
        self.assertEqual(self.run_cli("msg", "--help", command="devbox").returncode, 0)
