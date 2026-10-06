"""Per-session devbox state: <state>/sessions/<name>/{meta.json,notes.md}.

Shared by the dashboard (runs as root) and the devbox-notes CLI (runs as the
session user inside zellij), so both go through the same flock + atomic
replace and can't interleave a browser save with an agent append. Files
written as root are handed to whoever owns sessions/, so the CLI can still
write them afterwards.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import agent_process
import codex_daemon

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
META_DEFAULTS = {"title": "", "description": "", "cwd": "", "color": ""}
META_MAX_LEN = {"title": 80, "description": 500, "cwd": 4096, "color": 7}


class InvalidMeta(ValueError):
    pass


class Conflict(Exception):
    """Notes changed since the caller's base_version."""

    def __init__(self, content, version):
        super().__init__("notes changed since base_version")
        self.content = content
        self.version = version


def state_dir():
    return os.environ.get("DEVBOX_STATE_DIR", "/var/lib/dev-workspace")


def sessions_root():
    return os.path.join(state_dir(), "sessions")


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name))


def session_dir(name):
    if not valid_name(name):
        raise ValueError("invalid session name: %r" % (name,))
    return os.path.join(sessions_root(), name)


def notes_path(name):
    return os.path.join(session_dir(name), "notes.md")


def _hand_to_owner(path):
    # Root writes (the dashboard) would otherwise leave files the session
    # user's CLI can't replace; sessions/ itself is created owned by them.
    if os.geteuid() != 0:
        return
    st = os.stat(sessions_root())
    os.chown(path, st.st_uid, st.st_gid)


def _ensure_dir(name):
    d = session_dir(name)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        _hand_to_owner(d)
    return d


@contextlib.contextmanager
def _locked(name):
    lock = os.path.join(_ensure_dir(name), ".lock")
    with open(lock, "a") as f:
        _hand_to_owner(lock)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(text)
        _hand_to_owner(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _version(content):
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def read_meta(name):
    meta = dict(META_DEFAULTS)
    raw = _read(os.path.join(session_dir(name), "meta.json"))
    if raw:
        stored = json.loads(raw)
        meta.update({k: v for k, v in stored.items() if k in META_DEFAULTS})
    return meta


def _validate(patch):
    for key, value in patch.items():
        if key not in META_DEFAULTS:
            raise InvalidMeta("unknown field: %s" % key)
        if not isinstance(value, str):
            raise InvalidMeta("%s must be a string" % key)
        if len(value) > META_MAX_LEN[key]:
            raise InvalidMeta("%s is longer than %d characters" % (key, META_MAX_LEN[key]))
    color = patch.get("color")
    if color and not HEX_COLOR_RE.match(color):
        raise InvalidMeta("color must look like #rrggbb")
    cwd = patch.get("cwd")
    if cwd and not (os.path.isabs(cwd) and os.path.isdir(cwd)):
        raise InvalidMeta("cwd must be an existing absolute directory")


def update_meta(name, patch):
    _validate(patch)
    with _locked(name):
        meta = read_meta(name)
        meta.update(patch)
        _atomic_write(os.path.join(session_dir(name), "meta.json"), json.dumps(meta, indent=2) + "\n")
    return meta


def read_notes(name):
    content = _read(notes_path(name))
    return content, _version(content)


def write_notes(name, content, base_version=None):
    """Replace the notes. With base_version, refuse (Conflict) if the file
    changed since the caller read it; without it, overwrite unconditionally."""
    with _locked(name):
        current, version = read_notes(name)
        if base_version is not None and base_version != version:
            raise Conflict(current, version)
        _atomic_write(notes_path(name), content)
    return _version(content)


def append_notes(name, text):
    with _locked(name):
        current, _ = read_notes(name)
        if current and not current.endswith("\n"):
            current += "\n"
        if not text.endswith("\n"):
            text += "\n"
        new = current + text
        _atomic_write(notes_path(name), new)
    return _version(new)


# --- agent status: sessions/<name>/agents/<pane>.json ----------------------
# Written by devbox-status from Claude Code / Codex hooks (one file per zellij
# pane), read by the dashboard for the favicon/title/notifications.

AGENTS = ("claude", "codex")
PANE_RE = re.compile(r"^[0-9]{1,6}$")
# Most urgent first; an acked "done" counts as idle.
STATE_PRIORITY = {"waiting": 4, "working": 3, "done": 2, "unavailable": 1, "idle": 0}
_EVENT_STATES = {
    "SessionStart": "idle",
    "Interrupt": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    # PostToolUse is what moves a pane from waiting back to working once
    # a permission prompt is answered and the tool actually runs.
    "PostToolUse": "working",
    "PermissionRequest": "waiting",
    "Stop": "done",
    "SessionEnd": "end",
}
_WAITING_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog")


def event_state(payload):
    """Hook payload -> idle/working/waiting/done, "end" (drop the pane), or
    None for events that say nothing about status (e.g. idle_prompt)."""
    event = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if event == "Notification":
        return "waiting" if payload.get("notification_type") in _WAITING_NOTIFICATIONS else None
    return _EVENT_STATES.get(event)


def _agents_dir(name):
    return os.path.join(session_dir(name), "agents")


def _proc_root():
    return os.environ.get("DEVBOX_PROC_ROOT", "/proc")


def attached_pane(session_id):
    """(zellij session, pane) of the newest `claude attach <id>` client for
    this Claude session, else None.

    A background job's own environment is from wherever it was launched; the
    pane the user is actually watching it from is the attach client's. The
    client's <id> is a prefix of the full session id (8+ chars)."""
    if not session_id:
        return None
    best = None
    root = _proc_root()
    for pid in os.listdir(root):
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(root, pid, "cmdline"), "rb") as f:
                argv = f.read().decode(errors="replace").split("\0")
            if "claude" not in os.path.basename(argv[0]) or "attach" not in argv:
                continue
            ident = next((a for a in argv[argv.index("attach") + 1:] if a and not a.startswith("-")), "")
            if len(ident) < 8 or not session_id.startswith(ident):
                continue
            with open(os.path.join(root, pid, "environ"), "rb") as f:
                env = dict(kv.split("=", 1) for kv in f.read().decode(errors="replace").split("\0") if "=" in kv)
            session, pane = env.get("ZELLIJ_SESSION_NAME"), env.get("ZELLIJ_PANE_ID")
            if not (valid_name(session) and pane and PANE_RE.match(pane)):
                continue
            with open(os.path.join(root, pid, "stat")) as f:
                raw = f.read()
            start = int(raw[raw.rindex(")") + 2:].split()[19])
        except (OSError, ValueError, IndexError):
            continue
        if best is None or start > best[0]:
            best = (start, session, pane)
    return best[1:] if best else None


def _drop_elsewhere(agent_session, keep_path):
    """An agent session lives in one pane: remove its entries anywhere else
    (a background job re-attached from a different pane)."""
    root = sessions_root()
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return
    for name in names:
        adir = os.path.join(root, name, "agents")
        if not valid_name(name) or not os.path.isdir(adir):
            continue
        for f in os.listdir(adir):
            path = os.path.join(adir, f)
            if path == keep_path or not f.endswith(".json"):
                continue
            try:
                entry = json.loads(_read(path))
            except ValueError:
                continue
            if entry.get("session_id") == agent_session:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def record_agent_event(name, pane, agent, payload, pid):
    if not PANE_RE.match(str(pane)):
        raise ValueError("invalid pane id: %r" % (pane,))
    if agent not in AGENTS:
        raise ValueError("unknown agent: %r" % (agent,))
    state = event_state(payload)
    if state is None:
        return
    with _locked(name):
        adir = _agents_dir(name)
        path = os.path.join(adir, "%s.json" % pane)
        now = time.time()
        _atomic_write(os.path.join(session_dir(name), "activity.json"), json.dumps({"at": now}) + "\n")
        if state == "end":
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            return
        if not os.path.isdir(adir):
            os.makedirs(adir, exist_ok=True)
            _hand_to_owner(adir)
        prev = json.loads(_read(path) or "{}")
        same = prev.get("state") == state and prev.get("agent") == agent
        entry = {
            "agent": agent,
            "state": state,
            "since": prev.get("since", now) if same else now,
            "pid": int(pid),
            "pid_start": agent_process.identity(pid),
            "source": "hook",
            "acked": prev.get("acked", False) if same else False,
            "session_id": payload.get("session_id", ""),
            "updated_at": now,
            "cwd": payload.get("cwd", "") if isinstance(payload.get("cwd", ""), str) else "",
        }
        _atomic_write(path, json.dumps(entry) + "\n")
    if entry["session_id"]:
        _drop_elsewhere(entry["session_id"], path)


def session_status(name):
    """{state, agents}: agents sorted by pane, dead agent processes dropped."""
    adir = _agents_dir(name)
    try:
        files = sorted(f for f in os.listdir(adir) if f.endswith(".json"))
    except FileNotFoundError:
        files = []
    agents, dead = [], []
    for f in files:
        try:
            entry = json.loads(_read(os.path.join(adir, f)))
        except ValueError:
            continue
        if (not _pid_alive(entry.get("pid", 0)) or
                (entry.get("pid_start") is not None and
                 agent_process.identity(entry["pid"]) != entry["pid_start"])):
            dead.append(f)
            continue
        entry["pane"] = f[:-len(".json")]
        agents.append(entry)
    if dead:
        with _locked(name):
            for f in dead:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(adir, f))
    discovered = agent_process.processes()
    records = codex_daemon.threads() if any(p["agent"] == "codex" and p["session"] == name for p in discovered) else []
    for process in discovered:
        if process["session"] != name or process["agent"] != "codex":
            continue
        thread = codex_daemon.match(process, discovered, records)
        if not thread:
            continue
        previous = next((a for a in agents if a["pane"] == process["pane"] and a["agent"] == "codex"), {})
        state = codex_daemon.state(thread)
        if state == "idle" and previous.get("source") == "daemon" and previous.get("state") in ("working", "waiting", "done"):
            state = "done"
        now = time.time()
        entry = {"agent": "codex", "pane": process["pane"], "state": state,
                 "pid": process["pid"], "pid_start": process["pid_start"], "session_id": thread["id"],
                 "cwd": process["cwd"], "source": "daemon",
                 "since": previous.get("since", now) if previous.get("state") == state else now,
                 "updated_at": thread.get("updatedAt") or previous.get("updated_at", now),
                 "acked": previous.get("acked", False) if previous.get("state") == state else False}
        if entry != previous:
            with _locked(name):
                os.makedirs(adir, exist_ok=True)
                _hand_to_owner(adir)
                _atomic_write(os.path.join(adir, process["pane"] + ".json"), json.dumps(entry) + "\n")
        agents = [a for a in agents if a["pane"] != process["pane"] or a["agent"] != "codex"] + [entry]
    known = {(a["pane"], a["agent"]) for a in agents}
    agents.extend(a for a in agent_process.presence(name) if (a["pane"], a["agent"]) not in known)
    agents.sort(key=lambda a: int(a["pane"]))

    def effective(a):
        return "idle" if a["state"] == "done" and a.get("acked") else a["state"]

    state = max((effective(a) for a in agents), key=STATE_PRIORITY.get, default="idle")
    return {"state": state, "agents": agents}


def ack_done(name):
    """Mark every pane's "done" as seen (the tab was viewed)."""
    adir = _agents_dir(name)
    if not os.path.isdir(adir):
        return
    with _locked(name):
        for f in os.listdir(adir):
            if not f.endswith(".json"):
                continue
            path = os.path.join(adir, f)
            try:
                entry = json.loads(_read(path))
            except ValueError:
                continue
            if entry.get("state") == "done" and not entry.get("acked"):
                entry["acked"] = True
                _atomic_write(path, json.dumps(entry) + "\n")
