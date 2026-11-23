import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import messages


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBOX_STATE_DIR", str(tmp_path))
    return tmp_path


SENDER = {"session": "pm", "pane": "2"}


def test_ids_are_a_per_session_sequence():
    assert [messages.deliver("a", SENDER, None, "update", "x") for _ in range(3)] == [1, 2, 3]
    assert messages.deliver("b", SENDER, None, "update", "x") == 1


def test_rejects_bad_kind_empty_and_oversized_bodies():
    for kind, body in (("chat", "x"), ("update", "  "), ("update", "x" * (messages.MAX_BODY + 1))):
        with pytest.raises(ValueError):
            messages.deliver("a", SENDER, None, kind, body)
    with pytest.raises(ValueError):
        messages.deliver("a", {"session": "../x"}, None, "update", "x")


def test_pane_targeted_messages_reach_only_that_pane():
    messages.deliver("a", SENDER, "3", "question", "for three")
    messages.deliver("a", SENDER, None, "update", "for anyone")
    assert [m["body"] for m in messages.inbox("a", "3")] == ["for three", "for anyone"]
    assert [m["body"] for m in messages.inbox("a", "4")] == ["for anyone"]
    assert len(messages.inbox("a")) == 2


def test_ack_marks_read_and_unread_filter():
    for _ in range(3):
        messages.deliver("a", SENDER, None, "update", "x")
    assert messages.ack("a", ids=[2]) == [2]
    assert [m["id"] for m in messages.inbox("a", unread=True)] == [1, 3]
    assert messages.ack("a") == [1, 3]
    assert messages.unread_count("a") == 0


def test_pending_urgent_only():
    messages.deliver("a", SENDER, None, "update", "fyi")
    messages.deliver("a", SENDER, None, "blocker", "stuck")
    assert [m["kind"] for m in messages.pending("a", "1", urgent_only=True)] == ["blocker"]


def test_messages_survive_a_restart():
    messages.deliver("a", SENDER, None, "question", "still there?")
    importlib.reload(messages)
    assert messages.inbox("a")[0]["body"] == "still there?"


def status(*agents):
    return {"agents": [{"pane": p, "agent": "claude", "state": s} for p, s in agents]}


def test_nudge_types_once_into_an_idle_agent():
    typed = []
    messages.deliver("a", SENDER, None, "question", "q")
    later = time.time() + messages.NUDGE_AFTER + 1
    assert messages.nudge_due("a", lambda: status(("3", "idle")), lambda *a: typed.append(a), now=later) == ["3"]
    for _ in range(50):
        if typed:
            break
        time.sleep(0.01)
    assert typed == [("a", "3", "1 devbox message: run devbox-msg inbox --unread")]
    assert messages.nudge_due("a", lambda: status(("3", "idle")), lambda *a: typed.append(a), now=later) == []


@pytest.mark.parametrize("agents", [(("3", "working"),), (("3", "waiting"),), (("3", "idle"), ("4", "idle")), ()])
def test_nudge_never_types_into_busy_or_ambiguous_sessions(agents):
    messages.deliver("a", SENDER, None, "question", "q")
    later = time.time() + messages.NUDGE_AFTER + 1
    assert messages.nudge_due("a", lambda: status(*agents), lambda *a: pytest.fail("typed"), now=later) == []


def test_nudge_waits_and_skips_inbox_only_kinds():
    messages.deliver("a", SENDER, None, "done", "finished")
    messages.deliver("a", SENDER, None, "question", "fresh")
    called = []
    assert messages.nudge_due("a", lambda: called.append(1), now=time.time() + 1) == []
    assert called == []
