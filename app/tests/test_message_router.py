"""Server-side addressing: primary agents, parent links and remote delivery."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devbox_store as store
import messages
import runtime
import server


def session(name, kind="local"):
    return {"name": name, "port": 7690, "connection": "connected", "environment": "local",
            "target": {"type": kind}, "cwd": "/", "started_at": 1}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVBOX_ENVIRONMENTS", str(tmp_path / "environments.yaml"))
    r = runtime.Runtime()
    for name in ("pm", "c1", "c2", "solo", "multi", "remote"):
        r.sessions[name] = session(name, "ssh" if name == "remote" else "local")
    agents = {"pm": ["2", "5"], "c1": ["0"], "c2": ["0"], "solo": ["4"], "multi": ["3", "7"]}
    monkeypatch.setattr(store, "session_status", lambda n: {"state": "idle", "agents": [
        {"pane": p, "agent": "claude", "state": "idle"} for p in agents.get(n, [])]})
    return r


def link(name, parent):
    path = Path(store.session_dir(name)) / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": "", "parent": parent}))


async def send(rt, sender, to, kind="question", body="hi", pane="0", **extra):
    return (await rt.operation(sender, "msg-send", {"from_pane": pane, "to": to, "kind": kind, "body": body,
                                                    **extra}))["delivered"]


@pytest.mark.asyncio
async def test_session_goes_to_its_only_agent(rt):
    [item] = await send(rt, "c1", "solo")
    assert item == {"to": "solo:4", "id": 1, "delivery": "inbox"}
    [message] = messages.inbox("solo")
    assert message["from"] == {"session": "c1", "pane": "0"}
    assert message["to"] == {"session": "solo", "pane": "4"}


@pytest.mark.asyncio
async def test_several_agents_is_refused_with_the_candidates(rt):
    with pytest.raises(ValueError, match=r"ambiguous: multi has agents in panes 3 \(claude\), 7 \(claude\); use multi:<pane>"):
        await send(rt, "c1", "multi")
    assert messages.inbox("multi") == []
    [item] = await send(rt, "c1", "multi:7")
    assert item["to"] == "multi:7"


@pytest.mark.asyncio
async def test_parent_children_and_siblings_follow_the_link(rt):
    link("c1", "pm:2")
    link("c2", "pm:2")
    assert [i["to"] for i in await send(rt, "c1", "parent")] == ["pm:2"]
    assert [i["to"] for i in await send(rt, "c1", "siblings")] == ["c2:0"]
    assert [i["to"] for i in await send(rt, "pm", "children", pane="2")] == ["c1:0", "c2:0"]
    with pytest.raises(ValueError, match="no children recorded for pm"):
        await send(rt, "pm", "children", pane="5")


@pytest.mark.asyncio
async def test_missing_parent_and_unknown_sessions_are_errors(rt):
    with pytest.raises(ValueError, match="no parent recorded for solo"):
        await send(rt, "solo", "parent")
    with pytest.raises(ValueError, match="unknown session: nope"):
        await send(rt, "solo", "nope")
    with pytest.raises(ValueError, match="kind must be one of"):
        await send(rt, "solo", "c1", kind="chat")


@pytest.mark.asyncio
async def test_remote_recipient_is_queued_while_offline_then_flushed(rt):
    [item] = await send(rt, "c1", "remote")
    assert item["delivery"] == "queued until remote reconnects"
    assert rt.status("remote")["unread"] == 1
    remote = AsyncMock()
    remote.call.return_value = {"id": 1}
    rt.remotes["remote"] = remote
    await runtime.message_router.flush_outbox(rt, "remote")
    call = remote.call.call_args.args[0]
    assert (call["operation"], call["session"], call["from"]["session"], call["body"]) == ("deliver", "remote", "c1", "hi")
    assert rt.status("remote")["unread"] == 0


@pytest.mark.asyncio
async def test_connected_remote_recipient_gets_it_directly(rt):
    remote = AsyncMock()
    remote.call.return_value = {"id": 7}
    rt.remotes["remote"] = remote
    [item] = await send(rt, "c1", "remote")
    assert item == {"to": "remote", "id": 7, "delivery": "inbox"}


@pytest.mark.asyncio
async def test_relay_request_sender_is_the_relay_session(rt, monkeypatch):
    """A managed target can only send as the session its relay serves."""
    request = {"id": "a" * 32, "session": "remote", "operation": "msg-send",
               "args": {"from_pane": "1", "to": "solo", "kind": "update", "body": "from afar",
                        "from": {"session": "pm", "pane": "2"}}}
    rt.remotes["remote"] = AsyncMock()
    rt.remotes["remote"].call.return_value = {"states": {"remote": {"state": "idle", "agents": []}},
                                              "requests": [request]}
    monkeypatch.setattr(runtime.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await rt.poll()
    [message] = messages.inbox("solo")
    assert message["from"] == {"session": "remote", "pane": "1"}


@pytest.mark.asyncio
async def test_who_and_unread_in_listing(rt, tmp_path):
    link("c1", "pm:2")
    await send(rt, "pm", "c1", pane="2")
    result = await rt.operation("c1", "msg-who", {"from_pane": "0"})
    assert (result["me"], result["parent"], result["children"]) == ("c1:0", "pm:2", [])
    row = next(r for r in result["sessions"] if r["name"] == "c1")
    assert (row["unread"], row["parent"]) == (1, "pm:2")
    app = server.create_app(rt)
    rt.poll = AsyncMock()
    async with TestClient(TestServer(app), headers={"Authorization": "Bearer " + app["token"]}) as client:
        entry = await (await client.get("/api/sessions/c1")).json()
        assert entry["unread"] == 1
        sent = await client.post("/api/sessions/c1/msg-send", json={"from_pane": "0", "to": "parent", "kind": "done", "body": "shipped"})
        assert (await sent.json())["delivered"][0]["to"] == "pm:2"


@pytest.mark.asyncio
async def test_urgency_flag_is_stamped_on_local_delivery(rt):
    await send(rt, "c1", "solo", kind="update", urgency="urgent")
    await send(rt, "c1", "solo", kind="question", urgency="quiet")
    await send(rt, "c1", "solo", kind="question")
    assert [m["urgent"] for m in messages.inbox("solo")] == [True, False, True]
    with pytest.raises(ValueError, match="urgency must be"):
        await send(rt, "c1", "solo", urgency="loud")


@pytest.mark.asyncio
async def test_remote_delivery_carries_the_resolved_urgency(rt):
    link("c1", "remote")
    remote = AsyncMock()
    remote.call.return_value = {"id": 1}
    rt.remotes["remote"] = remote
    await send(rt, "c1", "remote", kind="done")           # remote has a child: done is urgent
    await send(rt, "c1", "remote", kind="question", urgency="quiet")
    assert [c.args[0]["urgent"] for c in remote.call.call_args_list] == [True, False]


@pytest.mark.asyncio
async def test_a_childs_done_nudges_its_parent_pm(rt):
    link("c1", "pm:2")
    await send(rt, "c1", "parent", kind="done")
    assert [m["urgent"] for m in messages.inbox("pm")] == [True]
    await send(rt, "c1", "solo", kind="done")
    assert [m["urgent"] for m in messages.inbox("solo")] == [False]


@pytest.mark.asyncio
async def test_msg_config_sets_shows_and_resets_a_session_setting(rt):
    shown = await rt.operation("c1", "msg-config", {"session": "solo"})
    assert shown["nudges"] == ["question", "blocker", "contract", "review", "github"]
    result = await rt.operation("c1", "msg-config", {"session": "solo", "nudge": ["done", "update"], "quiet": ["github"]})
    assert result["setting"] == {"nudge": ["done", "update"], "quiet": ["github"]}
    assert "done" in result["nudges"] and "github" not in result["nudges"]
    assert store.read_meta("solo")["messages"] == result["setting"]
    reset = await rt.operation("c1", "msg-config", {"session": "solo", "reset": True})
    assert reset["setting"] == {}
    with pytest.raises(ValueError, match="kind must be"):
        await rt.operation("c1", "msg-config", {"session": "solo", "nudge": ["chat"]})
    with pytest.raises(ValueError, match="unknown session"):
        await rt.operation("c1", "msg-config", {"session": "nope"})
