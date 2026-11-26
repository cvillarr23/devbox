"""PR event watcher: ownership, dedupe and which message each event sends."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devbox_store as store
import environments
import pr_watcher
import runtime


NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
EARLIER = "2000-01-01T00:00:00Z"


def session(name):
    return {"name": name, "port": 7690, "connection": "connected", "environment": "local",
            "target": {"type": "local"}, "cwd": "/", "started_at": 1}


def make_pr(body="", merged=False, merged_at=None, reviews=(), checks=(), sha="a1"):
    nodes = [{"conclusion": c, "name": n, "__typename": "CheckRun"} for n, c in checks]
    return {"number": 5, "title": "T", "url": "https://github.com/o/r/pull/5", "body": body,
            "headRefName": "feat/x", "merged": merged, "state": "MERGED" if merged else "OPEN", "mergedAt": merged_at,
            "repository": {"nameWithOwner": "o/r"},
            "reviews": {"nodes": [{"id": i, "state": s, "author": {"login": "bob"}} for i, s in reviews]},
            "commits": {"nodes": [{"commit": {"oid": sha, "statusCheckRollup": {"state": "X", "contexts": {"nodes": nodes}}}}]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVBOX_ENVIRONMENTS", str(tmp_path / "environments.yaml"))
    monkeypatch.setenv("DEVBOX_PUBLIC_URL", "https://d.example")
    rt = runtime.Runtime()
    for name in ("pm", "c1", "c10"):
        rt.sessions[name] = session(name)
    meta = Path(store.session_dir("c1")) / "meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"parent": "pm"}))
    sent, prs = [], []

    async def route(rt_, sender, name, pane, kind, body):
        sent.append((name, kind, body))

    async def fake_run(argv, data=None, timeout=20):
        if argv[0] == "gh":
            return json.dumps({"data": {"search": {"nodes": prs}}}).encode()
        raise RuntimeError("no git")

    monkeypatch.setattr(pr_watcher.message_router, "_route", route)
    monkeypatch.setattr(pr_watcher.message_router, "_primary_pane", lambda rt_, n: "0")
    monkeypatch.setattr(environments, "run", fake_run)
    return rt, sent, prs


def cycle(rt):
    asyncio.run(pr_watcher.cycle(rt))


def test_merge_notifies_owner_and_parent_once(env):
    rt, sent, prs = env
    prs.append(make_pr(body="see https://d.example/view/c1", merged=True, merged_at=NOW))
    cycle(rt)
    cycle(rt)
    assert [(n, k) for n, k, _ in sent] == [("c1", "github"), ("pm", "done")]
    assert "post-merge" in sent[0][2]


def test_merge_before_watcher_start_is_seeded_silently(env):
    rt, sent, prs = env
    prs.append(make_pr(body="https://d.example/view/c1", merged=True, merged_at=EARLIER))
    cycle(rt)
    cycle(rt)
    assert sent == []


def test_view_url_prefix_does_not_match_other_session(env):
    rt, sent, prs = env
    prs.append(make_pr(body="https://d.example/view/c10", merged=True, merged_at=NOW))
    cycle(rt)
    assert [n for n, _, _ in sent] == ["c10"]


def test_unowned_pr_sends_nothing(env):
    rt, sent, prs = env
    prs.append(make_pr(merged=True))
    cycle(rt)
    assert sent == []


def test_review_and_checks_after_seeding(env):
    rt, sent, prs = env
    body = "https://d.example/view/c1"
    prs.append(make_pr(body, reviews=[("r1", "COMMENTED")], checks=[("lint", "FAILURE")]))
    cycle(rt)
    assert sent == []  # first sight seeds silently
    prs[:] = [make_pr(body, reviews=[("r1", "COMMENTED"), ("r2", "CHANGES_REQUESTED")],
                      checks=[("lint", "FAILURE"), ("test", "FAILURE")])]
    cycle(rt)
    cycle(rt)
    assert [(n, k) for n, k, _ in sent] == [("c1", "review"), ("c1", "blocker")]
    assert "test" in sent[1][2] and "lint" not in sent[1][2]
    prs[:] = [make_pr(body, reviews=[("r1", "COMMENTED"), ("r2", "CHANGES_REQUESTED")],
                      checks=[("lint", "FAILURE")], sha="b2")]
    cycle(rt)
    assert sent[-1][1] == "blocker" and "lint" in sent[-1][2]


def test_gh_failure_is_quiet(env, monkeypatch):
    rt, sent, _ = env

    async def boom(argv, data=None, timeout=20):
        raise RuntimeError("not logged in")

    monkeypatch.setattr(environments, "run", boom)
    cycle(rt)
    assert sent == []
