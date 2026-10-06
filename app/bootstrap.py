"""Explicit helper installation; user hooks retain the agent's trust review."""
import base64
import io
import json
import os
import shlex
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PermissionRequest', 'Stop', 'SessionEnd', 'Interrupt']


def install_hooks(root, home, managed=False):
    home = Path(home)
    for agent in ('claude', 'codex'):
        command = shlex.join(['python3', str(root / 'devbox-status'), 'hook', '--agent', agent])
        if managed:
            if agent == 'codex':
                path = Path('/etc/codex/requirements.toml')
                text = '[features]\nhooks = true\n\n[hooks]\nmanaged_dir = ' + json.dumps(str(root)) + '\n'
                for event in EVENTS:
                    text += f'\n[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = "command"\ncommand = {json.dumps(command)}\ntimeout = 5\n'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                continue
            path = Path('/etc/claude-code/managed-settings.json')
        else:
            path = home / f'.{agent}' / ('hooks.json' if agent == 'codex' else 'settings.json')
        content = json.loads(path.read_text()) if path.exists() else {}
        hooks = content.setdefault('hooks', {})
        events = EVENTS if agent == 'codex' else EVENTS[:-1] + ['Notification']
        for event in events:
            groups = [g for g in hooks.get(event, []) if not any('devbox-status' in h.get('command', '') for h in g.get('hooks', []))]
            group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
            if event == 'Notification':
                group['matcher'] = 'permission_prompt|elicitation_dialog'
            hooks[event] = groups + [group]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.with_suffix(path.suffix + '.pre-devbox').exists():
            path.with_suffix(path.suffix + '.pre-devbox').write_bytes(path.read_bytes())
        path.write_text(json.dumps(content, indent=2) + '\n')
    for agent in ('claude', 'codex'):
        for skill in ('devbox-notes', 'devbox-browser'):
            target = home / f'.{agent}/skills/{skill}'
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() and not target.is_symlink():
                target.symlink_to(root / 'skills' / skill)


def archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in ('remote.py', 'diagnostics.py', 'bootstrap.py', 'devbox_store.py', 'agent_process.py', 'codex_daemon.py', 'devbox-status', 'cli.py'):
            tar.add(ROOT / name, arcname=name)
        tar.add(ROOT.parent / 'config/zellij.kdl', arcname='zellij.kdl')
        tar.add(ROOT.parent / 'config/layout.kdl', arcname='layout.kdl')
        tar.add(ROOT / 'skills', arcname='skills')
    return base64.b64encode(buf.getvalue()).decode()


async def bootstrap(config):
    from environments import run, transport
    payload = archive()
    # Fixed program; data is transferred on stdin, never interpolated in shell commands.
    program = 'import base64,io,os,pathlib,shutil,sys,tarfile\nroot=pathlib.Path.home()/".local/share/devbox"\nmissing=[x for x in ("python3","zellij") if not shutil.which(x)]\nif missing: raise SystemExit("Install required dependencies first: "+", ".join(missing))\ncodex=shutil.which("codex")\nroot.mkdir(parents=True,exist_ok=True)\ndata=base64.b64decode(sys.stdin.read())\nwith tarfile.open(fileobj=io.BytesIO(data),mode="r:gz") as t:\n for m in t.getmembers():\n  if m.name.startswith("/") or ".." in pathlib.Path(m.name).parts or m.issym() or m.islnk(): raise SystemExit("Invalid archive")\n t.extractall(root)\n(root/"bin").mkdir(exist_ok=True)\nimport shlex\nfor name in ("devbox","devbox-open","devbox-notes"):\n p=root/"bin"/name\n p.write_text("#!/bin/sh\\nexport DEVBOX_REMOTE=1\\nexec python3 "+shlex.quote(str(root/"cli.py"))+" "+name+" \\"$@\\"\\n")\n p.chmod(0o755)\nif codex and str(root/"bin") not in codex:\n p=root/"bin/codex"\n p.write_text("#!/bin/sh\\nexec "+shlex.quote(codex)+" --no-daemon \\"$@\\"\\n")\n p.chmod(0o755)\np=root/"bin/xdg-open"\np.write_text("#!/bin/sh\\nexec "+shlex.quote(str(root/"bin/devbox-open"))+" \\"$@\\"\\n")\np.chmod(0o755)\nsys.path.insert(0,str(root))\nfrom bootstrap import install_hooks\ninstall_hooks(root,pathlib.Path.home())\nprint("Installed helper and user hooks. Review Codex hooks with /hooks before relying on status.")\n'
    return (await run(transport(config, ['python3', '-c', program]), payload.encode(), timeout=60)).decode()
