import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import devbox_store as store
import session_details as details


class DetailsTest(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', *map(str, args)],
                              check=True, capture_output=True, text=True)

    def test_worktrees_share_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Reproduce an unrelated empty ancestor marker without relying on
            # the test runner's /tmp being dirty.
            Path(tmp, '.git').mkdir()
            repo = Path(tmp, 'repo')
            self.git('init', repo)
            self.git('-C', repo, 'commit', '--allow-empty', '-m', 'Initial')
            worktree = Path(tmp, 'branch')
            self.git('-C', repo, 'worktree', 'add', '-b', 'branch', worktree)
            self.assertEqual(details.repository(str(worktree)), details.repository(str(repo)))
            self.assertEqual(details.repository(str(repo)), {'name': 'repo', 'path': str(repo)})
            self.assertIsNone(details.repository(tmp))

    def test_unborn_repository_and_nested_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, 'repo')
            self.git('init', repo)
            folder = repo / 'nested'
            (folder / '.git').mkdir(parents=True)
            self.assertEqual(details.repository(folder), {'name': 'repo', 'path': str(repo)})

    def test_relative_gitdir_and_broken_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git('init', '--separate-git-dir', root / 'metadata', root / 'repo')
            marker = root / 'repo/.git'
            marker.write_text('gitdir: ../metadata\n')
            self.assertEqual(details.repository(root / 'repo'), {'name': 'repo', 'path': str(root / 'repo')})
            for value in ('gitdir: ../missing', 'not a gitdir', 'gitdir: ../empty'):
                (root / 'empty').mkdir(exist_ok=True)
                marker.write_text(value)
                self.assertIsNone(details.repository(root / 'repo'))

    def test_activity_survives_end_and_ack_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, DEVBOX_STATE_DIR=tmp):
            with patch.object(store.time, 'time', return_value=100):
                store.record_agent_event('s1','0','claude',{'hook_event_name':'Stop'},os.getpid())
            store.ack_done('s1')
            data = details.details('s1',{},store.session_status('s1'),{})
            self.assertEqual(data['last_activity_at'],100)
            with patch.object(store.time,'time',return_value=200):
                store.record_agent_event('s1','0','claude',{'hook_event_name':'SessionEnd'},os.getpid())
            self.assertEqual(details.details('s1',{},store.session_status('s1'),{})['last_activity_at'],200)

    def test_unknown_times_are_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, DEVBOX_STATE_DIR=tmp):
            data = details.details('s1',{'cwd':tmp},{'agents':[]},{})
            self.assertIsNone(data['started_at'])
            self.assertIsNone(data['last_activity_at'])
            self.assertEqual(data['folders'],[tmp])

    def test_repeated_working_events_advance_activity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, DEVBOX_STATE_DIR=tmp):
            for stamp in [100,200]:
                with patch.object(store.time,'time',return_value=stamp):
                    store.record_agent_event('s1','0','codex',{'hook_event_name':'PreToolUse'},os.getpid())
            agent=store.session_status('s1')['agents'][0]
            self.assertEqual(agent['since'],100)
            self.assertEqual(agent['updated_at'],200)
