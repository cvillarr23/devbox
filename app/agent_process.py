"""Linux agent identity. Read process metadata, never transcripts or prompts."""
import os
import time


def proc_root():
    return os.environ.get("DEVBOX_PROC_ROOT", "/proc")


def identity(pid):
    try:
        with open(os.path.join(proc_root(), str(pid), "stat")) as f:
            raw = f.read()
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def processes():
    found = []
    try:
        with open(os.path.join(proc_root(), 'stat')) as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith('btime '))
    except (OSError, StopIteration, ValueError):
        boot = None

    for pid in os.listdir(proc_root()):
        if not pid.isdigit():
            continue
        base = os.path.join(proc_root(), pid)
        try:
            with open(base + "/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").rstrip("\0").split("\0")
            executable = os.path.basename(argv[0])
            agent = executable if executable in ("codex", "claude") else None
            if executable in ("node", "nodejs") and len(argv) > 1:
                if "/@openai/codex/" in argv[1]:
                    agent = "codex"
                elif "/@anthropic-ai/claude-code/" in argv[1]:
                    agent = "claude"
            if not agent or any(a in ("app-server", "exec-server", "--managed-daemon") for a in argv[1:]):
                continue
            with open(base + "/environ", "rb") as f:
                env = dict(kv.split("=", 1) for kv in f.read().decode(errors="replace").split("\0") if "=" in kv)
            session, pane = env.get("ZELLIJ_SESSION_NAME"), env.get("ZELLIJ_PANE_ID")
            if not session or not pane or not pane.isdigit():
                continue
            start = identity(pid)
            if start is None:
                continue
            resumed = ""
            if "resume" in argv:
                following = argv[argv.index("resume") + 1:]
                if following and not following[0].startswith("-"):
                    resumed = following[0]
            found.append({"agent": agent, "pid": int(pid), "pid_start": start,
                          "session": session, "pane": pane, "cwd": os.readlink(base + "/cwd"),
                          "resumed": resumed, "fresh": not any(a in ("resume", "fork", "agents") for a in argv[1:]),
                          "started_at": boot + start / os.sysconf("SC_CLK_TCK") if boot else None})
        except (OSError, ValueError, IndexError):
            continue
    # Native binaries can be children of npm launchers: prefer the newest
    # process in each pane without presenting both as separate agents.
    unique = {}
    for item in sorted(found, key=lambda p: p["pid_start"]):
        unique[item["session"], item["pane"], item["agent"]] = item
    return list(unique.values())


def locate(agent, payload, session=None, pane=None):
    candidates = [p for p in processes() if p["agent"] == agent]
    if session and pane:
        candidates = [p for p in candidates if (p["session"], p["pane"]) == (session, pane)]
    else:
        sid = payload.get("session_id")
        exact = [p for p in candidates if sid and p["resumed"] == sid]
        candidates = exact or [p for p in candidates if p["cwd"] == payload.get("cwd")]
    return candidates[0] if len(candidates) == 1 else None


def presence(name):
    now = time.time()
    return [{**p, "state": "unavailable", "since": p.get("started_at") or now, "acked": False,
             "session_id": p["resumed"], "source": "process"}
            for p in processes() if p["session"] == name]
