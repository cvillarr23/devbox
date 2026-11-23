"""Type one line into a specific Zellij pane.

Zellij 0.41 actions only reach the focused pane of an attached client, so
this attaches a short-lived client when none is attached, moves focus until
the target pane has it, then types the line and presses Enter.
"""
import subprocess
import time

# Panes per tab in a devbox session are few; this bounds the focus search.
MAX_FOCUS_STEPS = 8


def _action(session, *args):
    result = subprocess.run(["zellij", "-s", session, "action", *args],
                            capture_output=True, text=True, timeout=10)
    return result.stdout if result.returncode == 0 else None


def focused_panes(session):
    """Pane id focused by each attached client (`terminal_3` -> "3")."""
    out = _action(session, "list-clients") or ""
    rows = [row.split(None, 2) for row in out.splitlines()[1:]]
    return [cols[1].rsplit("_", 1)[-1] for cols in rows if len(cols) >= 2]


def _attach_temporary_client(session):
    """A detached session silently drops typed text; `script` gives the client a pty."""
    client = subprocess.Popen(["script", "-qfc", "zellij attach " + session, "/dev/null"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if focused_panes(session):
            return client
        time.sleep(0.25)
    _detach(client)
    return None


def _detach(client):
    client.terminate()
    try:
        client.wait(timeout=5)
    except subprocess.TimeoutExpired:
        client.kill()


def _focus(session, pane):
    for _ in range(MAX_FOCUS_STEPS):
        if pane in focused_panes(session):
            return True
        _action(session, "focus-next-pane")
        time.sleep(0.2)
    return pane in focused_panes(session)


def type_line(session, pane, text):
    """Type TEXT plus Enter into SESSION's PANE; False when the pane can't be focused."""
    text = " ".join(text.split())
    client = None if focused_panes(session) else _attach_temporary_client(session)
    try:
        if not _focus(session, str(pane)):
            return False
        _action(session, "write-chars", text)
        time.sleep(0.3)
        _action(session, "write", "13")
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if client:
            _detach(client)
