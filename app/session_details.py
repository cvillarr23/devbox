"""Read-only session card metadata. Never starts a service or scans shell history."""
import json
import os
from pathlib import Path
import subprocess
import time

import devbox_store as store

_cache = (0, (), {})


def terminal_starts(names):
    global _cache
    names = tuple(sorted(n for n in names if store.valid_name(n)))
    if _cache[1] == names and time.monotonic() - _cache[0] < 15:
        return _cache[2]
    result = {}
    if names:
        try:
            out = subprocess.run(['systemctl', 'show', *('ttyd-session-' + n + '.service' for n in names),
                                  '--property=Id,ActiveEnterTimestampMonotonic,WorkingDirectory'],
                                 capture_output=True, text=True, timeout=3)
            boot = time.time() - time.monotonic()
            for block in out.stdout.strip().split('\n\n'):
                props = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
                unit = props.get('Id', '')
                name = unit.removeprefix('ttyd-session-').removesuffix('.service')
                stamp = int(props.get('ActiveEnterTimestampMonotonic', '0'))
                if name in names:
                    result[name] = {'started_at': boot + stamp / 1e6 if stamp else None,
                                    'cwd': props.get('WorkingDirectory', '')}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    _cache = (time.monotonic(), names, result)
    return result


def repository(folder):
    """Find the repo without invoking Git; linked worktrees share a group."""
    try:
        folder = Path(folder).resolve()
        for parent in (folder, *folder.parents):
            dotgit = parent / '.git'
            if dotgit.is_dir():
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
                    root = parent
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
