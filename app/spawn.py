"""Start a child session: validate arguments, make a worktree, launch the agent.

Shared by the gateway (local targets) and the remote helper (managed targets),
so the same checks and the same Zellij layout apply on both sides of the relay.
"""
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import devbox_store as store

ROOT = Path(__file__).resolve().parent
AGENTS = ("claude", "codex")
CLAUDE_MODELS = ("opus", "sonnet", "haiku")
PERMISSION_MODES = ("plan", "auto")
# Modes a resumed Claude is put back into: wider than what spawn offers, since the user
# may have switched. bypassPermissions is left out so a restart never turns it back on.
RESUME_PERMISSION_MODES = PERMISSION_MODES + ("default", "manual", "acceptEdits", "dontAsk")
CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
MAX_PROMPT = 100_000


def plan(args):
    """Validate spawn arguments and return them with defaults filled in."""
    agent = args.get("agent") or "claude"
    if agent not in AGENTS:
        raise ValueError("agent must be one of: " + ", ".join(AGENTS))
    result = {"agent": agent, "model": _model(agent, args.get("model")),
              "permission_mode": _permission_mode(agent, args.get("permission_mode")),
              "prompt": _prompt(args.get("prompt")), "worktree": _branch(args.get("worktree")),
              "cwd": args.get("cwd") or "", "environment": args.get("environment") or "local",
              "name": args.get("name") or ""}
    if result["name"] and not store.valid_name(result["name"]):
        raise ValueError("Use 1-64 letters, numbers, underscores or hyphens for the name")
    if result["cwd"] and result["worktree"]:
        raise ValueError("Use --cwd or --worktree, not both")
    return result


def _model(agent, model):
    if not model:
        return ""
    if agent == "claude" and model not in CLAUDE_MODELS:
        raise ValueError("claude model must be one of: " + ", ".join(CLAUDE_MODELS))
    if agent == "codex" and not CODEX_MODEL_RE.match(model):
        raise ValueError("invalid codex model name")
    return model


def _permission_mode(agent, mode):
    if not mode:
        return ""
    if agent != "claude":
        raise ValueError("--permission-mode is only supported for the claude agent")
    if mode not in PERMISSION_MODES:
        raise ValueError("permission mode must be one of: " + ", ".join(PERMISSION_MODES))
    return mode


def _prompt(prompt):
    if prompt is None:
        return ""
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT or "\0" in prompt:
        raise ValueError("prompt must be text of at most %d characters" % MAX_PROMPT)
    return prompt


def _branch(branch):
    if not branch:
        return ""
    if (not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch
            or branch.endswith(("/", ".lock"))):
        raise ValueError("invalid branch name")
    return branch


def child_name(parent, taken):
    """First free `<parent>-<n>` (or `spawn-<n>` for a parentless spawn)."""
    base = parent or "spawn"
    for n in range(1, 1000):
        candidate = ("%s-%d" % (base, n))[:64]
        if candidate not in taken:
            return candidate
    raise ValueError("no free session name; pass --name")


def child_prompt(prompt, parent):
    """Append the one context line that tells the child who spawned it."""
    if not parent:
        return prompt
    session = parent.partition(":")[0]
    line = ("You were spawned by devbox session `%s`. If anything is ambiguous, ask it with "
            "`devbox msg send --to parent --kind question \"...\"` instead of guessing, and "
            "finish with `devbox msg send --to parent --kind done \"...\"`." % session)
    return (prompt.rstrip() + "\n\n" if prompt.strip() else "") + line


def worktree_path(repo, branch):
    return str(Path(repo).parent / ("%s-%s" % (Path(repo).name, re.sub(r"[^A-Za-z0-9._-]+", "-", branch))))


def create_worktree(cwd, branch):
    """`git worktree add` a sibling of the repo containing `cwd`; returns its path."""
    top = _git(cwd, "rev-parse", "--show-toplevel").strip()
    path = worktree_path(top, branch)
    if os.path.exists(path):
        raise ValueError("worktree path already exists: " + path)
    exists = subprocess.run(["git", "-C", top, "show-ref", "--verify", "--quiet", "refs/heads/" + branch]).returncode == 0
    _git(top, "worktree", "add", *(() if exists else ("-b", branch)), path, *((branch,) if exists else ()))
    return path


def remove_worktree(cwd, path):
    subprocess.run(["git", "-C", cwd, "worktree", "remove", "--force", path], capture_output=True)


def _git(cwd, *argv):
    done = subprocess.run(["git", "-C", cwd, *argv], capture_output=True, text=True)
    if done.returncode:
        raise ValueError("git %s failed: %s" % (argv[0], done.stderr.strip()[-500:] or "not a git repository"))
    return done.stdout


def agent_script(agent, model, mode, prompt_file):
    """Shell run in the agent pane. Only validated tokens and a state-dir path are inlined."""
    parts = [agent]
    if model:
        parts += ["--model" if agent == "claude" else "-m", model]
    if mode:
        parts += ["--permission-mode", mode]
    command = " ".join(parts)
    # The prompt is read from a file so no prompt text is ever shell- or KDL-quoted.
    return '%s -- "$(cat %s)"; exec "${SHELL:-sh}"' % (command, _sh_quote(prompt_file))


def _sh_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _kdl(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def layout(cwd, script):
    return """layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        children
        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }
    tab name="main" {
        pane split_direction="vertical" {
            pane size="70%%" command="sh" cwd=%s {
                args "-c" %s
            }
            pane split_direction="horizontal" size="30%%" {
                pane
                pane {
                    command "htop"
                    start_suspended false
                }
            }
        }
    }
}
""" % (_kdl(cwd), _kdl(script))


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
RESUME_HINT = "Could not resume; run '%s' to pick a conversation."


def resume_script(resume):
    """Shell for the main pane that continues the remembered conversation, or None.

    Everything inlined is validated here, since resume.json is plain data on disk.
    `--resume` only reopens the conversation and sends no prompt, so the pane runs it
    straight away; a failure drops to a shell with a hint.
    """
    agent, sid = resume.get("agent"), resume.get("session_id")
    if agent not in AGENTS or not isinstance(sid, str) or not UUID_RE.match(sid):
        return None
    model, mode = resume.get("model"), resume.get("permission_mode")
    model = model if isinstance(model, str) and CODEX_MODEL_RE.match(model) else ""
    if agent == "claude":
        command = ["claude", "--resume", sid]
        command += ["--model", model] if model else []
        command += ["--permission-mode", mode] if mode in RESUME_PERMISSION_MODES else []
        picker = "claude --resume"
    else:
        command = ["codex", "resume"] + (["-m", model] if model else []) + [sid]
        picker = "codex resume"
    return '%s || echo %s; exec "${SHELL:-sh}"' % (" ".join(command), _sh_quote(RESUME_HINT % picker))


def existing_dir(cwd):
    """(cwd as given, cwd or its nearest existing parent, else $HOME)."""
    cwd = os.path.expanduser(cwd) if cwd else str(Path.home())
    target = Path(cwd)
    while not target.is_dir() and target != target.parent:
        target = target.parent
    if not target.is_absolute() or not target.is_dir():
        target = Path.home()
    return cwd, target


def enter_cwd(cwd):
    """chdir to cwd, or its nearest existing parent, else $HOME, with a one-line notice."""
    cwd, target = existing_dir(cwd)
    os.chdir(target)
    if str(target) != cwd:
        print(f'devbox: {cwd} no longer exists; starting in {target}', flush=True)


def color_env(env):
    """Default TERM/COLORTERM for zellij and its panes; a value the operator set wins.

    Nothing in the pod sets them and the zellij server (and every pane) inherits the
    environment of whoever created the session. xterm.js in ttyd handles 24-bit color.
    """
    for key, value in (("TERM", "xterm-256color"), ("COLORTERM", "truecolor")):
        if not env.get(key):
            env[key] = value
    return env


def zellij_config():
    bundled = ROOT / "zellij.kdl"  # remote helper bundle keeps config beside the code
    return str(bundled if bundled.is_file() else ROOT.parent / "config/zellij.kdl")


def live_sessions(env=None):
    """Names of running Zellij sessions; EXITED (resurrectable) ones are left out."""
    listed = subprocess.run(["zellij", "list-sessions", "-n"], env=env,
                            capture_output=True, text=True).stdout.splitlines()
    return [line.split()[0] for line in listed if line.strip() and "(EXITED" not in line]


def terminal_command(name, layout_file):
    """Attach to the session if it is running, otherwise start it fresh from the layout.

    Zellij caches each session's tabs, cwds and running commands under ~/.cache/zellij,
    which lives on the persistent volume. After a container or pod replacement the
    session is listed as EXITED, and attaching would resurrect it with the old commands
    one keypress away from running again. Deleting that copy keeps Reconnect a new
    terminal that never replays shell commands.
    """
    command = ["zellij", "--config", zellij_config()]
    if name in live_sessions():
        return command + ["attach", name]
    subprocess.run(["zellij", "delete-session", name], capture_output=True, text=True)
    return command + ["--new-session-with-layout", str(_reconnect_layout(name, layout_file)), "--session", name]


def _reconnect_layout(name, default_layout):
    """The default layout, or one whose main pane resumes the last conversation."""
    resume = store.read_resume(name)
    script = resume_script(resume) if resume else None
    if not script:
        return default_layout
    cwd = resume.get("cwd")
    cwd = str(existing_dir(cwd)[1]) if isinstance(cwd, str) and cwd else os.getcwd()
    path = Path(store.session_dir(name)) / "resume-layout.kdl"
    path.write_text(layout(cwd, script))
    return path


def default_layout():
    bundled = ROOT / "layout.kdl"  # remote helper bundle keeps the layout beside the code
    return bundled if bundled.is_file() else ROOT.parent / "config/layout.kdl"


def restore_session(name, cwd):
    """Recreate a stopped session without a client: resume layout if there is a record, else default.

    Used on server startup, so sessions survive a restart without anyone opening them.
    A session that is still live is left alone.
    """
    if name in live_sessions():
        return
    subprocess.run(["zellij", "delete-session", name], capture_output=True, text=True)
    env = {**os.environ, "DEVBOX_SESSION": name, "SHELL": os.environ.get("SHELL") or "/bin/zsh",
           "BROWSER": str(ROOT / "bin/devbox-open")}
    env["PATH"] = str(ROOT / "bin") + ":" + env.get("PATH", "")
    color_env(env)
    _create_detached(name, cwd, _reconnect_layout(name, default_layout()), env)


def start_agent(name, cwd, spec, prompt):
    """Create the detached Zellij session with the agent already running in it."""
    folder = Path(store.session_dir(name))
    folder.mkdir(parents=True, exist_ok=True)
    prompt_file = folder / "prompt.md"
    prompt_file.write_text(prompt)
    store.write_spawn(name, spec)
    layout_file = folder / "spawn-layout.kdl"
    layout_file.write_text(layout(cwd, agent_script(spec["agent"], spec["model"], spec["permission_mode"], str(prompt_file))))
    env = {**os.environ, "DEVBOX_SESSION": name, "SHELL": os.environ.get("SHELL") or "/bin/zsh"}
    env["PATH"] = str(ROOT / "bin") + ":" + env.get("PATH", "")
    color_env(env)
    _create_detached(name, cwd, layout_file, env)


def _create_detached(name, cwd, layout_file, env):
    """Start the session under a throwaway pty client, then detach it.

    `attach --create-background` can't take a layout and tab actions need a
    client, so a pty client creates the layout and is dropped once the session
    exists. The session and the agent in it keep running.
    """
    command = shlex.join(["zellij", "--config", zellij_config(), "--new-session-with-layout",
                          str(layout_file), "--session", name])
    client = subprocess.Popen(["script", "-qfc", command, "/dev/null"], cwd=cwd, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        _wait_for_session(name, env)
    finally:
        client.terminate()
        client.wait()


def _wait_for_session(name, env, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = subprocess.run(["zellij", "list-sessions", "-s", "-n"], env=env,
                                capture_output=True, text=True).stdout.splitlines()
        if name in listed:
            return
        time.sleep(0.2)
    raise RuntimeError("zellij session %s did not start" % name)
