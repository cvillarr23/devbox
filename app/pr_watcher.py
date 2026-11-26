"""Tell sessions about their PR's events: merged, reviewed, failed checks.

One `gh api graphql` search per cycle with the runtime's own `gh` auth. A PR
belongs to the local session whose view URL is in the PR body, else the one
whose cwd has the PR's repository and head branch checked out.
"""
import asyncio
import calendar
import json
import re
import sys
import time
from pathlib import Path

import devbox_store as store
import environments
import message_router
import messages

CYCLE_TICKS = 60
MERGED_WINDOW = 24 * 3600
SENDER = {"session": "devbox", "pane": None}
QUERY = """
query($q: String!) { search(query: $q, type: ISSUE, first: 50) { nodes { ... on PullRequest {
  number title url body headRefName merged state mergedAt
  repository { nameWithOwner }
  reviews(last: 10) { nodes { id state author { login } } }
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state
    contexts(first: 50) { nodes { __typename
      ... on CheckRun { name conclusion }
      ... on StatusContext { context state } } } } } } }
} } } }
"""
FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ERROR"}
REVIEW_STATES = {"CHANGES_REQUESTED": "requested changes on", "COMMENTED": "commented on",
                 "APPROVED": "approved"}
_warned = False
STARTED = time.time()


def _merged_since_start(pr):
    merged_at = pr.get("mergedAt")
    return bool(merged_at) and calendar.timegm(time.strptime(merged_at, "%Y-%m-%dT%H:%M:%SZ")) > STARTED


def _state_path():
    return Path(store.state_dir()) / "pr-events.json"


def _load():
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _failed_checks(pr):
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    names = []
    for node in rollup.get("contexts", {}).get("nodes", []):
        if node.get("conclusion") in FAILED:
            names.append(node["name"])
        elif node.get("state") in FAILED:
            names.append(node["context"])
    return commit.get("oid", ""), sorted(names)


async def _git(cwd, *args):
    try:
        return (await environments.run(["git", "-C", cwd, *args])).decode().strip()
    except (RuntimeError, OSError, asyncio.TimeoutError):
        return ""


def _repo_of(url):
    url = url.removesuffix(".git").replace(":", "/")
    return "/".join(url.split("/")[-2:]).lower()


async def owner(rt, pr):
    """The local session that owns PR, or None."""
    local = {n: e for n, e in rt.sessions.items() if e["target"]["type"] == "local"}
    for name in local:
        url = store.public_view_url(name)
        if url and re.search(re.escape(url) + r"(?![\w-])", pr["body"] or ""):
            return name
    repo = pr["repository"]["nameWithOwner"].lower()
    for name, entry in local.items():
        if await _git(entry["cwd"], "rev-parse", "--abbrev-ref", "HEAD") != pr["headRefName"]:
            continue
        if _repo_of(await _git(entry["cwd"], "remote", "get-url", "origin")) == repo:
            return name
    return None


async def _send(rt, session, kind, body, pane=None):
    try:
        pane = pane or message_router._primary_pane(rt, session)
        await message_router._route(rt, SENDER, session, pane, kind, body)
    except ValueError as exc:
        print("pr_watcher: %s: %s" % (session, exc), file=sys.stderr)


async def _notify_parent(rt, session, kind, body):
    link = message_router._parent(session)
    if link:
        parent, pane = message_router._split(link)
        if parent in rt.sessions:
            await _send(rt, parent, kind, body, pane)


async def _handle(rt, pr, seen, session):
    """Send what is new about PR into SEEN (its stored state); returns the new state."""
    ref = "%s#%d" % (pr["repository"]["nameWithOwner"], pr["number"])
    first = not seen
    reviews = [r for r in pr["reviews"]["nodes"] if r["state"] in REVIEW_STATES]
    oid, failed = _failed_checks(pr)
    state = {"merged": bool(pr["merged"]), "reviews": sorted(r["id"] for r in reviews),
             "failed": {"sha": oid, "names": failed}}
    if pr["merged"]:
        # Unrecorded merged PRs seed silently unless they merged after we started,
        # so a deploy or lost state file doesn't replay old merges.
        if not seen.get("merged") and (seen or _merged_since_start(pr)):
            text = "%s merged: %s (%s)" % (ref, pr["title"], pr["url"])
            await _send(rt, session, "github", text + ". Run the post-merge skill.")
            await _notify_parent(rt, session, "done", "%s's PR %s" % (session, text))
        return state
    if not first:
        for review in reviews:
            if review["id"] not in seen.get("reviews", []):
                who = (review.get("author") or {}).get("login", "someone")
                await _send(rt, session, "review", "%s %s %s: %s" % (who, REVIEW_STATES[review["state"]], ref, pr["url"]))
        old = seen.get("failed", {})
        new = [n for n in failed if old.get("sha") != oid or n not in old.get("names", [])]
        if new:
            await _send(rt, session, "blocker", "Failed checks on %s: %s (%s)" % (ref, ", ".join(new), pr["url"]))
    return state


async def cycle(rt):
    """One watcher pass over the user's recent PRs."""
    global _warned
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - MERGED_WINDOW))
    query = "is:pr author:@me sort:updated-desc (is:open OR merged:>=%s)" % since
    try:
        out = await environments.run(["gh", "api", "graphql", "-f", "query=" + QUERY, "-f", "q=" + query], timeout=30)
        prs = [p for p in json.loads(out)["data"]["search"]["nodes"] if p]
    except (RuntimeError, OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
        if not _warned:
            print("pr_watcher: gh query failed: %s" % exc, file=sys.stderr)
            _warned = True
        return
    _warned = False
    stored = _load()
    result = dict(stored)
    for pr in prs:
        ref = "%s#%d" % (pr["repository"]["nameWithOwner"], pr["number"])
        session = await owner(rt, pr)
        if not session:
            continue
        result[ref] = await _handle(rt, pr, stored.get(ref, {}), session)
    if result != stored:
        store._atomic_write(str(_state_path()), json.dumps(result))
