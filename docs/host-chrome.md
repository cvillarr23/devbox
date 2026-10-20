# Host Chrome for container agents

Decision record for the spike on driving the host Mac's Chrome from agents in a
devbox container (Docker Desktop on Apple Silicon). It covers Claude in Chrome,
the Chrome DevTools Protocol (CDP), and the in-container browser for the
`linux/arm64` image.

## Decision

| Need | Path | devbox change |
| --- | --- | --- |
| Claude Code drives the user's real Chrome (SSO, extension) | **Claude in Chrome over its cloud bridge** | None. Log in to Claude Code in the session with the same claude.ai account as the extension, then use `claude --chrome` or `/chrome`. |
| Other agents (Codex, Playwright, chrome-devtools-mcp) drive a host Chrome | **CDP to a dedicated host Chrome profile**, opt-in | Follow-up: a helper that resolves the host address and writes the MCP config. |
| Browser inside the arm64 image | **Debian `chromium`** on the existing Xvfb/noVNC path, selected by `DEVBOX_CHROME` | Lands with the multi-arch image work. |

A relay of Claude in Chrome's native-messaging socket into the container was
evaluated and rejected: the bridge makes it unnecessary, and the socket's
ownership checks make a relay fragile.

## Option 1: Claude in Chrome

### How it connects

The extension and Claude Code have two transports:

- **Native host.** Chrome starts `claude --chrome-native-host` through the
  manifest `com.anthropic.claude_code_browser_extension.json` in Chrome's
  `NativeMessagingHosts` directory. The host listens on
  `/tmp/claude-mcp-browser-bridge-<user>/<pid>.sock`. Before dialling, the CLI
  requires the directory to be mode 0700, the socket 0600, and both owned by its
  own UID, and it then runs a signed local pairing handshake.
- **Cloud bridge.** The CLI's `claude-in-chrome` MCP server (`claude
  --claude-in-chrome-mcp`) connects to `wss://bridge.claudeusercontent.com`,
  authenticated as the claude.ai account Claude Code is logged in with. It lists
  the extension instances signed in to the same account and pairs with one;
  the extension may show a pairing prompt the first time.

In Claude Code 2.1.289 (the version this image pins) and 2.1.302, the MCP server
context always carries the bridge configuration, so tool calls go over the
bridge. A CLI in a container therefore reaches the Mac's extension with nothing
more than outbound HTTPS/WSS.

A socket relay (socat on the Mac exposing the socket over TCP, socat in the
container recreating it) would have to fake the per-user path and the UID and
mode checks, would expose a browser-control socket on a TCP port, and buys
nothing over the bridge. Not pursued.

### Requirements

- Claude Code in the session is logged in with a **claude.ai account** (OAuth,
  not an API key, Bedrock or Vertex), and it is the same account and
  organization the extension in the Mac's Chrome is signed in to. A mismatched
  `CLAUDE_CODE_OAUTH_TOKEN` is reported as an account mismatch.
- The organization allows Claude in Chrome (`allow_claude_browser_extension`),
  and no managed `deniedMcpServers` policy blocks it.
- The container can reach `bridge.claudeusercontent.com:443`. Allow it on a
  corporate proxy.
- Chrome is open on the Mac with the extension running. With several connected
  browsers, choose one through the pairing prompt or `select_browser`;
  `CLAUDE_CHROME_PAIRED_DEVICE_ID` pins a device.

### Setup

1. On the Mac: install the Claude extension in your normal Chrome profile and
   sign in to claude.ai.
2. In a devbox terminal: `claude` → `/login` with the same account.
3. Start with `claude --chrome`, or run `/chrome` in an existing session. Ask for
   a browser task; approve the pairing prompt in Chrome the first time.

### Security notes

- The agent acts in your real, signed-in profile: SSO sessions, cookies and any
  site you are logged in to. The extension's per-site permissions and the
  default `ask` permission mode are the guardrails. Do not set
  `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_checks` in devbox.
- Anything that can run `claude` with your login in the container can reach the
  browser, so this inherits the trust boundary of the devbox home volume where
  the login lives.
- Traffic between the CLI and the extension goes through Anthropic's bridge
  service rather than staying on the machine.

## Option 2: CDP to host Chrome

### Findings

- Chrome 136 and later ignore `--remote-debugging-port` on the default user data
  directory. Debugging needs a **separate profile** (`--user-data-dir`), which is
  signed in to SSO once and then persists. It cannot drive the everyday profile.
- Chrome binds the debugging port to `127.0.0.1`. Current versions (155 tested)
  ignore `--remote-debugging-address` outside the old headless mode.
- Chrome's DNS-rebinding guard rejects a `Host` header that is neither an IP
  address nor `localhost`:
  `GET http://host.docker.internal:9222/json/version` returns
  `HTTP 500 Host header is specified and is not an IP address or localhost.`
  Clients must connect by **IP**: resolve `host.docker.internal` first. The
  returned `webSocketDebuggerUrl` echoes that IP, so the WebSocket connects too.
- With the IP, raw CDP (`Target.createTarget`, `Runtime.evaluate`) and
  `chrome-devtools-mcp --browser-url=http://<ip>:9222` both work. Playwright MCP
  takes the same URL as `--cdp-endpoint`.

### Setup

On the Mac, start a dedicated debugging Chrome next to your normal one:

```sh
open -na "Google Chrome" --args \
  --user-data-dir="$HOME/Library/Application Support/devbox-chrome-cdp" \
  --remote-debugging-port=9222 --no-first-run
```

In the devbox container, connect by IP:

```sh
HOST_IP=$(getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}')
claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@1.11.0 --browser-url="http://$HOST_IP:9222"
codex mcp add chrome-devtools -- npx -y chrome-devtools-mcp@1.11.0 --browser-url="http://$HOST_IP:9222"
```

On a Linux Docker host, add `extra_hosts: ["host.docker.internal:host-gateway"]`
to the service. Chrome still listens on loopback there, so a Linux host also
needs a forwarder on the bridge gateway; Docker Desktop does that forwarding
itself (see results).

### Security notes

- CDP has no authentication. Whoever reaches the port controls the profile:
  reads every cookie and stored session, runs script in any origin, downloads
  files and opens `file://` URLs.
- On Docker Desktop, `host.docker.internal` is reachable from **every**
  container on the Mac, not only devbox, so any container you run can drive the
  debugging profile while it is open.
- Use a dedicated profile signed in only to what the agent needs, run it only
  while needed, never bind the port to a LAN interface, and quit it afterwards.
- Prefer Claude in Chrome for Claude Code; use CDP for agents that cannot use it.

## Option 3: Chromium in the container

Google publishes no Linux arm64 Chrome. Debian's `chromium` package is current
for arm64 through `bookworm-security` (155.0.8059.79, the base of
`node:24-bookworm-slim`) and in trixie. It runs on the existing Xvfb, x11vnc
and noVNC path with `DEVBOX_CHROME=chromium`, uses its own profile in
`~/.config/devbox-chrome`, and needs a separate SSO login made through the
Browser panel. It is the fallback when neither host path fits, for example on
Kubernetes targets.

For the multi-arch image: install `chromium` on arm64 and keep `google-chrome`
on amd64, default `DEVBOX_CHROME` per architecture, and make `devbox doctor`
check `$DEVBOX_CHROME` instead of the hardcoded `google-chrome`.

## How it was tested

`scripts/spike-host-chrome-probe.sh` runs every check and prints a PASS, FAIL or
SKIP report. It uses throwaway containers and a temporary Chrome profile only.

```sh
scripts/spike-host-chrome-probe.sh                 # all checks
scripts/spike-host-chrome-probe.sh --only cdp      # one section: c4c, cdp, chromium
```

The Claude in Chrome check runs inside the running devbox container
(`DEVBOX_CONTAINER`, defaulting to the Compose service), with the host's `claude`
as a control. It lists connected browsers and then opens example.com through the
extension; approve any prompt in Chrome.

### Linux validation (amd64 workstation, Docker 29.8.1)

- Claude Code 2.1.302 with no native host installed: the MCP server starts with
  22 tools, and `list_connected_browsers` is answered by the bridge (`[]`: no
  browser with the extension was open on that account). Claude Code 2.1.289 in an
  unauthenticated `node:22` container starts the same server and reports that
  no extension is connected.
- CDP against Chromium 155 behind a forwarder: by name `HTTP 500` (Host header),
  by IP `HTTP 200`, page driven to "Example Domain", chrome-devtools-mcp 1.11.0
  `list_pages` succeeds.
- `chromium:arm64` 155.0.8059.79 is published in bookworm-security and trixie.
  The arm64 run itself needs the Mac (no emulation on that host).

### Mac results

Pending: the probe output from the Apple Silicon work Mac goes here.
