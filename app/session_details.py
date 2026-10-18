"""Read-only session card metadata. Never starts a service or scans shell history."""
import json
import os
from pathlib import Path

import devbox_store as store

def valid_gitdir(gitdir, common_dir):
    # A leftover .git directory is not enough. Linked worktrees keep HEAD
    # locally and share the object database and refs with the main repository.
    return ((gitdir / 'HEAD').is_file() and
            (common_dir / 'objects').is_dir() and (common_dir / 'refs').is_dir())


def repository(folder):
    """Find the repo without invoking Git; linked worktrees share a group."""
    try:
        folder = Path(folder).resolve()
        for parent in (folder, *folder.parents):
            dotgit = parent / '.git'
            if dotgit.is_dir() and valid_gitdir(dotgit, dotgit):
                return {'name': parent.name, 'path': str(parent)}
            if dotgit.is_file():
                marker = dotgit.read_text().strip()
                if not marker.startswith('gitdir: '):
                    return None
                gitdir = (parent / marker[8:]).resolve()
                common = gitdir / 'commondir'
                if common.is_file():
                    common_dir = (gitdir / common.read_text().strip()).resolve()
                    root = common_dir.parent if common_dir.name == '.git' else parent
                else:
                    common_dir = gitdir
                    root = parent
                if valid_gitdir(gitdir, common_dir):
                    return {'name': root.name, 'path': str(root)}
    except (OSError, ValueError):
        pass
    return None


def details(name, meta, status, runtime):
    folders = set()
    times = []
    for agent in status['agents']:
        stamp = agent.get('updated_at', agent.get('since'))
        if isinstance(stamp, (int, float)):
            times.append(stamp)
        try:
            cwd = os.readlink(os.path.join(store._proc_root(), str(agent['pid']), 'cwd'))
        except (OSError, KeyError):
            cwd = agent.get('cwd', '')
        if cwd and os.path.isabs(cwd):
            folders.add(cwd)
    if not folders:
        cwd = meta.get('cwd') or runtime.get('cwd')
        if cwd:
            folders.add(cwd)
    try:
        activity = json.loads(Path(store.session_dir(name), 'activity.json').read_text()).get('at')
        if isinstance(activity, (int, float)):
            times.append(activity)
    except (OSError, ValueError):
        pass
    try:
        times.append(os.stat(store.notes_path(name)).st_mtime)
    except OSError:
        pass
    repos = {r['path']: r for f in sorted(folders) if (r := repository(f))}
    return {'started_at': runtime.get('started_at'),
            'last_activity_at': max(times) if times else None,
            'agent_names': sorted({a['agent'] for a in status['agents']}),
            'folders': sorted(folders), 'repositories': list(repos.values())}
