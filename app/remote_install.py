"""Remote bootstrap entrypoint; receives the helper archive on stdin."""
import base64,io,os,pathlib,shutil,sys,tarfile
root=pathlib.Path.home()/".local/share/devbox"
missing=[x for x in ("python3","zellij") if not shutil.which(x)]
if missing: raise SystemExit("Install required dependencies first: "+", ".join(missing))
search=os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if pathlib.Path(p).expanduser().resolve() != (root/"bin").resolve())
codex=shutil.which("codex",path=search)
root.mkdir(parents=True,exist_ok=True)
data=base64.b64decode(sys.stdin.read())
with tarfile.open(fileobj=io.BytesIO(data),mode="r:gz") as t:
 for m in t.getmembers():
  if m.name.startswith("/") or ".." in pathlib.Path(m.name).parts or m.issym() or m.islnk(): raise SystemExit("Invalid archive")
 t.extractall(root)
(root/"bin").mkdir(exist_ok=True)
import shlex
for name in ("devbox","devbox-open","devbox-notes"):
 p=root/"bin"/name
 p.write_text("#!/bin/sh\nexport DEVBOX_REMOTE=1\nexec python3 "+shlex.quote(str(root/"cli.py"))+" "+name+" \"$@\"\n")
 p.chmod(0o755)
if codex:
 p=root/"bin/codex"
 if pathlib.Path(codex).resolve()==pathlib.Path("/opt/devbox/bin/codex").resolve():
  codex="/usr/local/bin/codex"
 executable=shlex.quote(codex)
 text="#!/bin/sh\nfor arg in \"$@\"; do\n  if [ \"$arg\" = --no-daemon ]; then exec "+executable+" \"$@\"; fi\ndone\nexec "+executable+" --no-daemon \"$@\"\n"
 p.write_text(text)
 p.chmod(0o755)
p=root/"bin/xdg-open"
p.write_text("#!/bin/sh\nexec "+shlex.quote(str(root/"bin/devbox-open"))+" \"$@\"\n")
p.chmod(0o755)
sys.path.insert(0,str(root))
from bootstrap import install_hooks
install_hooks(root,pathlib.Path.home())
print("Installed helper and user hooks. Review Codex hooks with /hooks before relying on status.")
