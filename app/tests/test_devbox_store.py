import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devbox_store as store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DEVBOX_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["DEVBOX_STATE_DIR"]

    def test_valid_name(self):
        self.assertTrue(store.valid_name("fantasy-1"))
        self.assertTrue(store.valid_name("dev_workspace"))
        for bad in ["", "..", "a/b", "a b", "x" * 200]:
            self.assertFalse(store.valid_name(bad), bad)

    def test_session_dir_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            store.session_dir("../etc")

    def test_meta_defaults(self):
        self.assertEqual(store.read_meta("s1"), {"title": "", "description": "", "cwd": "", "color": ""})

    def test_update_meta_merges_and_persists(self):
        store.update_meta("s1", {"title": "Fantasy PM"})
        m = store.update_meta("s1", {"color": "#3b82f6"})
        self.assertEqual(m["title"], "Fantasy PM")
        self.assertEqual(store.read_meta("s1")["color"], "#3b82f6")

    def test_update_meta_validation(self):
        bad = [
            {"color": "red"},
            {"color": "#12345"},
            {"cwd": "relative/path"},
            {"cwd": "/definitely/not/a/dir"},
            {"title": "x" * 81},
            {"description": "x" * 501},
            {"nope": "x"},
            {"title": 5},
        ]
        for patch in bad:
            with self.assertRaises(store.InvalidMeta, msg=patch):
                store.update_meta("s1", patch)
        store.update_meta("s1", {"cwd": self.tmp.name, "color": ""})
        self.assertEqual(store.read_meta("s1")["cwd"], self.tmp.name)

    def test_notes_empty_then_write(self):
        content, version = store.read_notes("s1")
        self.assertEqual(content, "")
        new_version = store.write_notes("s1", "hello\n", base_version=version)
        self.assertEqual(store.read_notes("s1"), ("hello\n", new_version))

    def test_notes_conflict(self):
        _, v0 = store.read_notes("s1")
        store.append_notes("s1", "agent line")
        with self.assertRaises(store.Conflict) as cm:
            store.write_notes("s1", "browser edit", base_version=v0)
        self.assertEqual(cm.exception.content, "agent line\n")
        self.assertEqual(cm.exception.version, store.read_notes("s1")[1])

    def test_write_without_base_version_forces(self):
        store.append_notes("s1", "a")
        store.write_notes("s1", "b\n")
        self.assertEqual(store.read_notes("s1")[0], "b\n")

    def test_append_adds_separating_newline(self):
        store.write_notes("s1", "no trailing newline")
        store.append_notes("s1", "next")
        self.assertEqual(store.read_notes("s1")[0], "no trailing newline\nnext\n")

    def test_no_temp_files_left(self):
        store.write_notes("s1", "x")
        store.update_meta("s1", {"title": "t"})
        names = sorted(os.listdir(store.session_dir("s1")))
        self.assertEqual(names, [".lock", "meta.json", "notes.md"])


if __name__ == "__main__":
    unittest.main()
