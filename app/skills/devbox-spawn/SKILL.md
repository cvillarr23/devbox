---
name: devbox-spawn
description: Start a child devbox session with its own agent, prompt and optional git worktree, and collect its result. Use when the user asks to spawn, delegate, fan out or run work in parallel in another devbox session, or when a task splits into independent, well-specified pieces that a cheaper agent can do while you coordinate.
---

# devbox spawn

`devbox spawn` creates a session, optionally makes a git worktree from the current repo, starts an agent with your prompt, records you as its parent, and prints the child's name and URL.

```bash
devbox spawn --worktree feat/42-retry --agent claude --model sonnet \
  --permission-mode plan --prompt - <<'EOF2'
...the brief...
EOF2
devbox spawn --cwd /workspace/other --agent codex --prompt "run the migration dry-run"
devbox spawn --environment server --prompt "..." --json    # managed environment
```

Flags: `--name`, `--environment` (default: yours), `--cwd DIR` or `--worktree BRANCH` (not both; the worktree is a sibling of the repo you run it from and needs the same environment as you), `--agent claude|codex`, `--model` (claude: `opus|sonnet|haiku`; codex: any model name), `--permission-mode plan|auto` (claude only), `--prompt TEXT|-`, `--json`.

The parent link is taken from your detected session, never from an argument. Without a detected session the child is top-level. A parent can have `DEVBOX_SPAWN_LIMIT` live children (default 5, `0` = unlimited); past that spawn refuses, so end or reuse a child first.

## When to spawn
- The work splits into pieces that don't touch the same files and don't wait on each other.
- Each piece can be stated precisely enough that a cheaper agent can finish it without you.
- Don't spawn for small or strictly serial work; doing it yourself is faster than briefing.

## Briefing a child
Use a stronger coordinator (opus) with cheaper children (mostly sonnet). A cheap child needs a real spec, because it can't see your context:
1. **Objective**: one or two sentences on the outcome, not the steps.
2. **Files and contracts**: which files it may change, and the interfaces/behaviour it must preserve or provide.
3. **Acceptance checks**: exact commands that must pass.
4. **Definition of done**: what to hand back (branch, PR, summary) and what is out of scope.

Spawn appends one line to the prompt naming you and how to reach you. The child sends ambiguity up with `devbox msg send --to parent --kind question ...` instead of guessing, and ends with `devbox msg send --to parent --kind done ...`. Answer its questions rather than letting it stall.

## Collecting results
- `devbox msg inbox` shows questions and `done` reports from your children.
- `devbox wait <child>... --state done` blocks until they finish, without polling.
- Review the child's branch or PR yourself before reporting it done.
- End finished children from the dashboard when you're done with them.
