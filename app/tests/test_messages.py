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
    monkeypatch.setenv("DEVBOX_CONFIG", str(tmp_path / "config.toml"))
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


# --- configurable urgency ---------------------------------------------------

def urgent(name="a", kind="update", **kw):
    messages.deliver(name, SENDER, None, kind, "x", **kw)
    return messages.inbox(name)[-1]["urgent"]


def write_config(state, text):
    (state / "config.toml").write_text(text)


def test_defaults_match_the_old_kinds():
    assert [urgent(kind=k) for k in messages.KINDS] == [k in messages.URGENT for k in messages.KINDS]


def test_message_flag_overrides_the_kind():
    assert urgent(kind="update", urgency="urgent") is True
    assert urgent(kind="question", urgency="quiet") is False
    with pytest.raises(ValueError):
        messages.deliver("a", SENDER, None, "update", "x", urgency="loud")


def test_session_setting_beats_user_config_and_quiet_beats_nudge(state):
    import devbox_store as store
    write_config(state, '[messages]\nnudge = ["update", "review"]\n')
    store.update_meta("a", {"messages": {"nudge": ["done", "question"], "quiet": ["question", "review"]}})
    assert urgent(kind="done") is True       # session nudge, not in the user config
    assert urgent(kind="question") is False  # quiet beats nudge within the session
    assert urgent(kind="review") is False    # session quiet beats the user config
    assert urgent(kind="update") is True     # unnamed by the session: the user config decides


def test_user_config_replaces_the_default_set(state):
    write_config(state, '[messages]\nnudge = ["done", "update"]\n')
    assert [urgent(kind=k) for k in ("done", "update", "question")] == [True, True, False]


def test_message_flag_beats_session_setting(state):
    import devbox_store as store
    store.update_meta("a", {"messages": {"quiet": ["blocker"]}})
    assert urgent(kind="blocker") is False
    assert urgent(kind="blocker", urgency="urgent") is True


def test_a_parent_session_nudges_on_done_by_default():
    import devbox_store as store
    assert urgent("pm", "done") is False
    store.update_meta("kid", {"parent": "pm"})
    assert urgent("pm", "done") is True
    assert urgent("pm", "update") is False


def test_broken_user_config_falls_back_to_defaults(state):
    write_config(state, "[messages\nnudge = ")
    assert urgent(kind="question") is True and urgent(kind="done") is False


def test_legacy_messages_without_a_stamp_use_the_kind():
    assert messages.is_urgent({"kind": "question"}) is True
    assert messages.is_urgent({"kind": "done"}) is False
    assert messages.is_urgent({"kind": "question", "urgent": False}) is False


def test_pending_and_nudge_follow_the_stamp():
    messages.deliver("a", SENDER, None, "update", "act now", urgency="urgent")
    messages.deliver("a", SENDER, None, "question", "meh", urgency="quiet")
    assert [m["body"] for m in messages.pending("a", "1", urgent_only=True)] == ["act now"]
    typed = []
    later = time.time() + messages.NUDGE_AFTER + 1
    assert messages.nudge_due("a", lambda: status(("3", "idle")), lambda *a: typed.append(a), now=later) == ["3"]
    for _ in range(50):
        if typed:
            break
        time.sleep(0.01)
    assert typed == [("a", "3", "1 devbox message: run devbox-msg inbox --unread")]


def test_batch_waits_for_quiet_then_sends_one_line(state):
    for _ in range(3):
        messages.deliver("a", SENDER, None, "question", "q")
    t0 = time.time()
    typed = []
    send = lambda *a: typed.append(a)
    # newest message is only 5s old at t0+35 -> wait for batch_window (10s)
    for m in messages._all("a"):
        m["at"] = t0 - 35 + (30 if m["id"] == 3 else 0)
        messages._write("a", m)
    assert messages.nudge_due("a", lambda: status(("3", "idle")), send, now=t0) == []
    assert messages.nudge_due("a", lambda: status(("3", "idle")), send, now=t0 + 6) == ["3"]
    for _ in range(50):
        if typed:
            break
        time.sleep(0.01)
    assert typed == [("a", "3", "3 devbox messages: run devbox-msg inbox --unread")]


def test_batch_is_capped_so_a_stream_cannot_postpone_the_nudge():
    messages.deliver("a", SENDER, None, "question", "old")
    messages.deliver("a", SENDER, None, "question", "new")
    t0 = time.time()
    for m in messages._all("a"):
        m["at"] = t0 - (41 if m["id"] == 1 else 1)
        messages._write("a", m)
    assert messages.nudge_due("a", lambda: status(("3", "idle")), lambda *a: None, now=t0) == ["3"]


def test_nudge_after_and_batch_window_come_from_user_config(state):
    write_config(state, "[messages]\nnudge_after = 5\nbatch_window = 0\n")
    messages.deliver("a", SENDER, None, "question", "q")
    assert messages.nudge_due("a", lambda: status(("3", "idle")), lambda *a: None, now=time.time() + 6) == ["3"]
