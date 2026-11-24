import base64
import os
import pty
import stat
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import clipboard  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCRIPT = os.path.join(ROOT, "app", "clipboard.py")
SHIMS = ("clip", "xclip", "xsel", "wl-copy", "wl-paste")


def run(flavor, args=(), stdin=b"", tty=None, new_session=False):
    env = {"PATH": os.environ["PATH"]}
    if tty:
        env["DEVBOX_CLIP_TTY"] = tty
    return subprocess.run([sys.executable, SCRIPT, flavor, *args], input=stdin, env=env,
                          capture_output=True, start_new_session=new_session)


def copy_through_pty(flavor, args=(), stdin=b""):
    master, slave = pty.openpty()
    try:
        r = run(flavor, args, stdin, tty=os.ttyname(slave), new_session=True)
        os.set_blocking(master, False)
        try:
            out = os.read(master, 1 << 20)
        except BlockingIOError:
            out = b""
        return r, out
    finally:
        os.close(master)
        os.close(slave)


class Osc52Test(unittest.TestCase):
    def test_framing_and_base64(self):
        self.assertEqual(clipboard.osc52(b"hi"), b"\x1b]52;c;" + base64.b64encode(b"hi") + b"\x07")
        self.assertEqual(clipboard.osc52(b"hi"), b"\x1b]52;c;aGk=\x07")

    def test_empty_and_non_ascii(self):
        self.assertEqual(clipboard.osc52(b""), b"\x1b]52;c;\x07")
        data = "héllo ✓\n".encode()
        seq = clipboard.osc52(data)
        self.assertEqual(base64.b64decode(seq[len(b"\x1b]52;c;"):-1]), data)


class ParseTest(unittest.TestCase):
    def test_xclip(self):
        self.assertEqual(clipboard.parse_xclip(["-selection", "clipboard"])[0], "copy")
        self.assertEqual(clipboard.parse_xclip(["-selection", "clipboard", "-i"])[0], "copy")
        self.assertEqual(clipboard.parse_xclip(["-sel", "c", "-in", "-quiet"])[0], "copy")
        self.assertEqual(clipboard.parse_xclip(["-selection", "clipboard", "-o"])[0], "read")
        self.assertEqual(clipboard.parse_xclip(["-out"])[0], "read")
        # a selection value that looks like a flag is consumed, not parsed
        self.assertEqual(clipboard.parse_xclip(["-display", ":0", "-i"])[0], "copy")

    def test_xsel(self):
        for args in (["--clipboard", "--input"], ["-b", "-i"], ["-bi"], ["-b"], []):
            self.assertEqual(clipboard.parse_xsel(args)[0], "copy", args)
        for args in (["--clipboard", "--output"], ["-bo"], ["-o"], ["-b", "-o"]):
            self.assertEqual(clipboard.parse_xsel(args)[0], "read", args)
        for args in (["-c"], ["-bc"], ["--clear"]):
            self.assertEqual(clipboard.parse_xsel(args)[0], "clear", args)

    def test_wl_copy(self):
        self.assertEqual(clipboard.parse_wl_copy([]), ("copy", []))
        self.assertEqual(clipboard.parse_wl_copy(["hello", "world"]), ("copy", ["hello", "world"]))
        self.assertEqual(clipboard.parse_wl_copy(["-n", "-t", "text/plain", "x"]), ("copy", ["x"]))
        self.assertEqual(clipboard.parse_wl_copy(["--primary", "x"]), ("copy", ["x"]))
        self.assertEqual(clipboard.parse_wl_copy(["--", "-x"]), ("copy", ["-x"]))
        self.assertEqual(clipboard.parse_wl_copy(["--clear"])[0], "clear")


class ShimTest(unittest.TestCase):
    def test_shims_are_executable_and_dispatch(self):
        for name in SHIMS:
            path = os.path.join(ROOT, "bin", name)
            self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, name)
            with open(path) as f:
                body = f.read()
            self.assertIn(f"clipboard.py {name} ", body)

    def test_copy_writes_osc52_to_tty(self):
        cases = [
            ("clip", [], b"hi\n"),
            ("xclip", ["-selection", "clipboard"], b"hi\n"),
            ("xsel", ["-bi"], b"hi\n"),
            ("xsel", ["--clipboard", "--input"], b"hi\n"),
            ("wl-copy", [], b"hi\n"),
        ]
        for flavor, args, stdin in cases:
            r, out = copy_through_pty(flavor, args, stdin)
            self.assertEqual(r.returncode, 0, flavor)
            self.assertEqual(r.stdout, b"", flavor)
            self.assertEqual(out, clipboard.osc52(b"hi\n"), flavor)

    def test_text_args(self):
        r, out = copy_through_pty("wl-copy", ["hello", "there"])
        self.assertEqual(out, clipboard.osc52(b"hello there"))
        r, out = copy_through_pty("clip", ["a", "b"])
        self.assertEqual(out, clipboard.osc52(b"a b"))

    def test_clear_sends_empty_payload(self):
        r, out = copy_through_pty("xsel", ["-bc"])
        self.assertEqual(out, clipboard.osc52(b""))

    def test_read_modes_fail_cleanly(self):
        for flavor, args in (("xclip", ["-o"]), ("xclip", ["-selection", "clipboard", "-o"]),
                             ("xsel", ["-bo"]), ("wl-paste", [])):
            r, out = copy_through_pty(flavor, args)
            self.assertEqual(r.returncode, 1, flavor)
            self.assertEqual(r.stdout, b"")
            self.assertIn(b"copy only", r.stderr)
            self.assertEqual(out, b"")

    def test_no_terminal_exits_zero_silently(self):
        r = run("clip", stdin=b"hi\n", new_session=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")
        self.assertEqual(r.stderr, b"")

    def test_oversized_payload_errors(self):
        r, out = copy_through_pty("clip", stdin=b"x" * (clipboard.MAX_BYTES + 1))
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"limit", r.stderr)
        self.assertEqual(out, b"")


if __name__ == "__main__":
    unittest.main()
