---
name: devbox-notes
description: Read or write this devbox session's notepad (the Notes widget in the devbox web UI sidebar). Use when the user asks to note, jot down, track, remember for this session, or add something to "the notes"/"notepad"/"session notes", or asks what's in them. Only works inside a devbox zellij session (ZELLIJ_SESSION_NAME is set) or with --session.
---

# devbox session notes

Each devbox zellij session has a markdown notepad. The user sees it live in the
session sidebar, and edits it there too.
Treat it as a shared scratchpad with the user. It is not your private memory.

The `devbox-notes` CLI finds the current session itself. For a Claude
background job, that's the pane you're attached from (`claude attach <id>`).
Otherwise it's `$ZELLIJ_SESSION_NAME`. `devbox-notes cat` reads the current session:

```bash
devbox-notes cat                     # read the notes
devbox-notes append "- [ ] retry the migration after the backfill"
printf '%s\n' "## Findings" "- cause: stale cache" | devbox-notes append -
devbox-notes write < new-notes.md    # replace everything (read first!)
devbox-notes --session other cat     # another session's notes
```

Rules:
- Prefer `append`. The user may be typing in the widget at the same time, and
  `append` never removes their text.
- Run `write` only after `cat`, when the user asked you to reorganize or
  rewrite the notes. Preserve anything you weren't asked to change.
- Keep entries short and scannable: bullets, `- [ ]` checkboxes, `##` headings.
- Don't write secrets or tokens into notes.
- If `devbox-notes` reports that no session is set, you're not in a devbox
  session. Tell the user and ask which session they mean. Don't guess.

On managed remote environments, notes are relayed to devbox over the control connection. The path command is available only inside the local devbox container.
