---
name: devbox-notes
description: Read or write the current devbox session's notepad (the Notes widget in the devbox web UI sidebar), with automatic session detection or an explicit --session. Use when the user asks to note, jot down, track, remember for this session, add something to the notes/notepad/session notes, or asks what's in them.
---

# devbox session notes

Each devbox zellij session has a markdown notepad. The user sees it live in the
session sidebar, and edits it there too.
Treat it as a shared scratchpad with the user. It is not your private memory.

The CLI detects the current session from the attached Claude pane, the relay's
`DEVBOX_SESSION`, the Zellij environment, or an exact agent thread registration
whose process is still alive. It fails if detection is missing or ambiguous; it
never guesses from a working directory. `devbox-session` prints the session name;
`--json` includes pane and detection source.

```bash
devbox-session                     # print the current session name
devbox-session --json              # include pane and detection source
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
- Try automatic detection before asking the user for a session name. If it fails,
  report the error and ask which session they mean, then use `--session`. Don't guess.

On managed remote environments, notes are relayed to devbox over the control connection. The path command is available only inside the local devbox container.
