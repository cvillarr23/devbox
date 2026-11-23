"""Resolve devbox-msg addresses and route messages to each recipient's home store.

Runs in the devbox server. The sender is always the session the request came
from (the Unix-socket URL or the relay connection), never a message field.
"""
import asyncio
import contextlib
import json
import time
from pathlib import Path

import devbox_store as store
import messages

RELATIONS = ("parent", "children", "siblings")


def _split(address):
    session, _, pane = address.partition(":")
    if not store.valid_name(session):
        raise ValueError("invalid address: %r (use SESSION or SESSION:PANE)" % address)
    return session, messages._pane_or_none(pane)


def _parent(name):
    """The parent link spawn records: "SESSION" or "SESSION:PANE", else None."""
    return store.read_meta(name)["parent"] or None


def _is_child_of(link, session, pane):
    """A link to SESSION:PANE belongs to that agent; a bare SESSION link (or a
    sender with no pane) matches any agent of the session."""
    if not link:
        return False
    parent_session, parent_pane = _split(link)
    return parent_session == session and (pane is None or parent_pane is None or parent_pane == pane)


def _children(rt, session, pane):
    return sorted(name for name, link in store.child_links(session).items()
                  if name in rt.sessions and _is_child_of(link, session, pane))


def _siblings(rt, session):
    link = _parent(session)
    if not link:
        return []
    parent_session, parent_pane = _split(link)
    return [name for name in _children(rt, parent_session, parent_pane) if name != session]


def _primary_pane(rt, session):
    """The pane of the session's only agent. None when no agent runs (the
    first one to start claims it). Several agents is refused, never guessed."""
    agents = rt.status(session)["agents"]
    panes = sorted({a["pane"] for a in agents}, key=int)
    if len(panes) <= 1:
        return panes[0] if panes else None
    listing = ", ".join("%s (%s)" % (p, "/".join(sorted({a["agent"] for a in agents if a["pane"] == p})))
                        for p in panes)
    raise ValueError("ambiguous: %s has agents in panes %s; use %s:<pane>" % (session, listing, session))


def _addresses(rt, sender, pane, to):
    """Recipient addresses for TO, before primary-agent resolution."""
    if to == "parent":
        link = _parent(sender)
        if not link:
            raise ValueError("no parent recorded for %s (see devbox-msg who)" % sender)
        return [_split(link)]
    if to == "children":
        return [(name, None) for name in _children(rt, sender, pane)]
    if to == "siblings":
        return [(name, None) for name in _siblings(rt, sender)]
    return [_split(to)]


def _targets(rt, sender, pane, to):
    addresses = _addresses(rt, sender, pane, to)
    if not addresses:
        raise ValueError("no %s recorded for %s (see devbox-msg who)" % (to, sender))
    for session, _ in addresses:
        if session not in rt.sessions:
            raise ValueError("unknown session: %s" % session)
    return [(session, target or _primary_pane(rt, session)) for session, target in addresses]


def _outbox(name):
    return Path(store.state_dir()) / "outbox" / name


def _queue(name, record):
    folder = _outbox(name)
    folder.mkdir(parents=True, exist_ok=True)
    store._atomic_write(str(folder / ("%d.json" % time.time_ns())), json.dumps(record))


def _remote_connected(rt, name):
    return name in rt.remotes and rt.sessions[name]["connection"] == "connected"


async def _route(rt, sender, session, pane, kind, body):
    record = {"from": sender, "pane": pane, "kind": kind, "body": body}
    address = session + (":" + pane if pane else "")
    if rt.sessions[session]["target"]["type"] == "local":
        ident = messages.deliver(session, sender, pane, kind, body)
        return {"to": address, "id": ident, "delivery": "inbox"}
    if _remote_connected(rt, session):
        try:
            result = await rt.remotes[session].call({"operation": "deliver", "session": session, **record})
            return {"to": address, "id": result["id"], "delivery": "inbox"}
        except (RuntimeError, OSError, asyncio.TimeoutError):
            pass
    _queue(session, record)
    return {"to": address, "id": None, "delivery": "queued until %s reconnects" % session}


async def send(rt, sender, args):
    pane = messages._pane_or_none(args.get("from_pane"))
    origin = messages._validated({"session": sender, "pane": pane}, args.get("kind"), args.get("body"))
    targets = _targets(rt, sender, pane, args.get("to", ""))
    return {"delivered": [await _route(rt, origin, session, target, args["kind"], args["body"])
                          for session, target in targets]}


async def flush_outbox(rt, name):
    """Deliver messages queued while NAME's remote target was offline, oldest first."""
    for path in sorted(_outbox(name).glob("*.json")):
        record = json.loads(path.read_text())
        # ValueError is the target rejecting this record; drop it rather than
        # let one bad message fail every later poll. Offline errors propagate.
        with contextlib.suppress(ValueError):
            await rt.remotes[name].call({"operation": "deliver", "session": name, **record})
        path.unlink()


def queued_count(name):
    return len(list(_outbox(name).glob("*.json")))


def _session_row(rt, name):
    status = rt.status(name)
    return {"name": name, "environment": rt.sessions[name]["environment"],
            "connection": status["connection"], "status": status["state"],
            "parent": _parent(name), "unread": status.get("unread", 0),
            "agents": [{k: a.get(k) for k in ("pane", "agent", "state")} for a in status["agents"]]}


def who(rt, sender, args):
    pane = messages._pane_or_none(args.get("from_pane"))
    return {"me": sender + (":" + pane if pane else ""), "parent": _parent(sender),
            "children": _children(rt, sender, pane), "siblings": _siblings(rt, sender),
            "sessions": [_session_row(rt, name) for name in rt.sessions]}
