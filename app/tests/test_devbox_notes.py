import os
import subprocess
import sys
import tempfile
import unittest

CLI = os.path.join(os.path.dirname(__file__), "..", "devbox-notes")


class DevboxNotesCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"PATH": os.environ["PATH"], "DEVBOX_STATE_DIR": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None, session="s1"):
        env = dict(self.env)
        if session:
            env["ZELLIJ_SESSION_NAME"] = session
        return subprocess.run([sys.executable, CLI, *args], input=stdin, env=env,
                              capture_output=True, text=True)

    def test_no_session_errors(self):
        r = self.run_cli("cat", session=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ZELLIJ_SESSION_NAME", r.stderr)

    def test_append_and_cat(self):
        self.assertEqual(self.run_cli("append", "first").returncode, 0)
        self.assertEqual(self.run_cli("append", "-", stdin="second\n").returncode, 0)
        self.assertEqual(self.run_cli("cat").stdout, "first\nsecond\n")

    def test_write_replaces_from_stdin(self):
        self.run_cli("append", "old")
        self.assertEqual(self.run_cli("write", stdin="new\n").returncode, 0)
        self.assertEqual(self.run_cli("cat").stdout, "new\n")

    def test_session_flag_overrides_env(self):
        self.run_cli("--session", "other", "append", "x")
        self.assertEqual(self.run_cli("cat").stdout, "")
        self.assertEqual(self.run_cli("--session", "other", "cat").stdout, "x\n")

    def test_path(self):
        r = self.run_cli("path")
        self.assertEqual(r.stdout.strip(), os.path.join(self.tmp.name, "sessions", "s1", "notes.md"))

    def test_bad_session_name(self):
        r = self.run_cli("cat", session="../evil")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
