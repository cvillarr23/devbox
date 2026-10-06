---
name: devbox-browser
description: Open a URL from a devbox terminal in the user's viewing browser or in devbox's remote Chrome, and forward a remote preview server into Chrome. Use when working inside devbox and the task calls for opening a web page or showing a preview.
---

Use `devbox-open --target local|remote URL` to choose the destination explicitly.

- `local`: the user's browser viewing this session. Use for documentation or links intended for the user. A `pending` result means queued, not opened; popup blocking may require the user to click the link in the sidebar.
- `remote`: Chrome inside devbox, visible in its Browser panel. Use for development previews, container-local URLs, or login flows whose callback server runs inside devbox.

For a web server on a managed SSH or Kubernetes target, run `devbox forward PORT` there, then open the returned URL with `--target remote`. This forwards only the selected loopback port. Forwarded localhost URLs belong to devbox and are not reachable in the user's local browser.

The current session is selected automatically. Use `--session NAME` only to target another known session. `BROWSER` and `xdg-open` use the configured default destination, normally remote.

Chrome has one shared persistent profile per devbox. This command opens pages; it does not itself provide browser automation. Do not report a queued or failed request as a successfully opened page.
