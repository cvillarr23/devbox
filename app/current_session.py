"""Resolve a devbox session from identity evidence, without cwd-based guessing."""

import json
import os
from pathlib import Path

import agent_process
import devbox_store as store


class SessionDetectionError(ValueError):
    pass


def _result(session, pane, source):
    if not store.valid_name(session):
        raise SessionDetectionError("invalid session name: %r" % session)
    return {"session": session, "pane": pane, "source": source}


def detect(explicit=None):
    if explicit is not None:
        return _result(explicit, None, "explicit")

    claude_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    attached = store.attached_pane(claude_id)
    if attached:
        return _result(*attached, "attached-pane")
    # Set by the remote relay wrappers; managed targets have no devbox Zellij.
    session = os.environ.get("DEVBOX_SESSION")
    if session:
        return _result(session, None, "devbox-environment")
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    if session:
        return _result(session, os.environ.get("ZELLIJ_PANE_ID"), "environment")

    thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    agent = "codex" if thread else "claude"
    thread = thread or claude_id
    matches = set()
    if thread:
        for path in Path(store.sessions_root()).glob("*/agents/*.json"):
            name, pane = path.parent.parent.name, path.stem
            if not store.valid_name(name) or not pane.isdigit():
                continue
            try:
                entry = json.loads(path.read_text())
                if entry.get("session_id") != thread or entry.get("agent") != agent:
                    continue
                pid, start = entry.get("pid"), entry.get("pid_start")
                # A recycled PID must not associate this thread with an old pane.
                if (not isinstance(pid, int) or pid <= 0 or start is None
                        or agent_process.identity(pid) != start):
                    continue
                matches.add((name, pane))
            except (OSError, ValueError, AttributeError):
                continue
    if len(matches) == 1:
        name, pane = next(iter(matches))
        return _result(name, pane, "agent-registry")
    if len(matches) > 1:
        raise SessionDetectionError("ambiguous devbox session for this agent; use --session NAME")
    raise SessionDetectionError(
        "cannot detect current devbox session: no ZELLIJ_SESSION_NAME or unique live "
        "agent registration; use --session NAME"
    )
