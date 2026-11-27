# Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEVBOX_PORT` | `7680` | Container HTTP port |
| `DEVBOX_STATE_DIR` | `/data` | Notes, runtime registry, and generated access token |
| `DEVBOX_WORKSPACE` | `/workspace` | Local session starting directory |
| `DEVBOX_ENVIRONMENTS` | `/etc/devbox/environments.yaml` | Named target configuration |
| `DEVBOX_OPEN_TARGET` | `remote` | Destination used by `BROWSER` and `xdg-open` |
| `DEVBOX_AUTH_MODE` | `token` | `token` login, `proxy` for externally authenticated access, or `cloudflare-access` to verify the Cloudflare Access JWT |
| `DEVBOX_ACCESS_TEAM_DOMAIN` | unset | `cloudflare-access` only, required: team domain, e.g. `myteam.cloudflareaccess.com` |
| `DEVBOX_ACCESS_AUD` | unset | `cloudflare-access` only, required: Access application AUD tag; comma-separated to accept several applications |
| `DEVBOX_ACCESS_EMAILS` | unset | `cloudflare-access` only, optional: comma-separated allowlist matched case-insensitively against the JWT `email` claim |
| `DEVBOX_TOKEN_FILE` | `<state>/access-token` | Existing token file or generated token location, used by `token` mode and by `DEVBOX_TOKEN_PORT` |
| `DEVBOX_TOKEN_PORT` | unset | Optional second HTTP listener that always uses token auth, whatever `DEVBOX_AUTH_MODE` is. For a private fallback ingress only; never expose it publicly ([token port](#token-port)) |
| `DEVBOX_SECURE_COOKIE` | unset | Set `1` when HTTPS terminates at a reverse proxy |
| `DEVBOX_CHROME_SANDBOX` | `0` | Set `1` when the runtime permits Chrome's sandbox namespaces |
| `DEVBOX_CHROME` | `google-chrome` | Browser executable for a derived image, such as `chromium` on arm64 ([host Chrome](host-chrome.md)) |
| `DEVBOX_SPAWN_LIMIT` | `5` | Most live child sessions one parent may `devbox spawn`; `0` disables the limit |
| `DEVBOX_PUBLIC_URL` | unset (`devbox spawn` prints `http://localhost:$DEVBOX_PORT`) | Public origin, for example `https://devbox-next.roo-net.cc`. Sets `publicHost` in the dashboard config and adds an absolute `view_url` to each session in the API and to `devbox-session --json`; `devbox spawn` prints it too |
| `DEVBOX_TRUST_PROXY` | unset | Set `1` to honor `X-Forwarded-Proto: https` for the session cookie's `Secure` flag. Off by default because any client can send the header |
| `DEVBOX_CLAUDE_REMOTE_CONTROL` | `1` | `0`, `false`, `no` or `off` stops devbox from enabling Claude remote control at startup. Devbox sets `remoteControlAtStartup` in `~/.claude/settings.json` (local home at each start, remote targets at bootstrap) only when the key is absent, so a value you set is kept, and a pre-existing file is backed up to `settings.json.pre-devbox`. A symlinked `settings.json` (for example from a dotfiles installer) is never written through; see [Hooks and bootstrap](#hooks-and-bootstrap) |
| `DEVBOX_SOCKET` | `/tmp/devbox-control.sock` | Private local CLI socket, mode 0600 |
| `DEVBOX_CODEX_SOCKET` | autodetected | Optional existing local Codex daemon Unix socket |

`DEVBOX_HOST_PORT` and `DEVBOX_PROJECTS` are Compose substitutions, not application settings. `.env` is ignored by Git and excluded from image builds.

## Persistent directories

Docker uses a home volume at `/home/devbox` and a state volume at `/data`; projects are a bind mount at `/workspace`. Kubernetes uses the `devbox` PVC for `/data/home` and `/data/state`, and the `devbox-workspace` PVC for `/workspace`. UID/GID 1000 must have write access.

The server generates a random access token only if the configured token file does not exist, in `token` mode or whenever `DEVBOX_TOKEN_PORT` is set. Read it using `docker compose exec` or `kubectl exec`; it is not printed into server logs. Tokens must have at least 24 characters.

Agent login state and browser profiles belong to home. Session definitions and notes belong to state. Live processes, open links, and temporary forwards do not survive container replacement. Remote Zellij sessions may remain on the target; reconnect attaches to them if present.

## SSH and kubeconfig mounts

For Docker, add the required mounts through `compose.override.yaml`, which is local configuration and should not contain secret values:

```yaml
services:
  devbox:
    volumes:
      - ./credentials/ssh:/home/devbox/.ssh:ro
      - ./credentials/kubeconfig:/home/devbox/.kube/config:ro
```

Store only the credentials needed for the configured targets. SSH uses `BatchMode=yes`; password prompts are not supported. Populate `known_hosts` before connecting. Kubeconfig exec-auth plugins must also exist in the image.

For Kubernetes, create a Secret separately and mount its kubeconfig at `$HOME/.kube/config` using an overlay. Do not commit the credential contents. A target role typically needs pod listing, `pods/exec`, and only the namespace(s) you intend to manage.

## Hooks and bootstrap

The container image installs managed agent hooks without changing mounted user config. Remote bootstrap uses user hook configuration and makes a `.pre-devbox` backup of existing JSON settings. If `~/.claude/settings.json` or `~/.codex/hooks.json` (or the `~/.claude` or `~/.codex` directory) is a symlink, bootstrap and startup leave it alone: no hooks, no `remoteControlAtStartup`, no `.pre-devbox` copy next to the link. Bootstrap prints `Skipped: ...` and `devbox doctor` reports `agent_settings.<agent>.state: managed-link` with `hooks: false` and a hint. Add the entries to the file the link points at (see the README's dotfiles section). `devbox doctor` also shows `hooks` and `remote_control` for every target. Review Codex user hooks through `/hooks`; bootstrap does not bypass trust review or approval policies.

Bootstrap is explicit and repeatable. It checks Python/Zellij first, replaces the helper bundle, replaces only hooks whose command invokes devbox-status, and preserves unrelated settings. Remote targets require Python 3.10 or newer.

## Skills plugin

The devbox skills (`devbox-browser`, `devbox-msg`, `devbox-notes`, `devbox-spawn`) ship as a Claude Code plugin from this repository: marketplace `devbox` (`.claude-plugin/marketplace.json`), plugin `devbox`, install id `devbox@devbox`. Both names are a stable public interface. The skills live in `plugins/devbox/skills/` and version with devbox releases.

- **Image:** managed settings declare the marketplace from `/opt/devbox`; startup (`devbox-setup-home`) sets `enabledPlugins["devbox@devbox"]` in `~/.claude/settings.json` only if you have not set it.
- **Remote targets:** bootstrap unpacks the marketplace under `~/.local/share/devbox` and declares and enables it in `~/.claude/settings.json` the same way. Re-running bootstrap replaces the bundle, which is how upgrades reach the skills.
- **Codex** has no marketplace, so `~/.codex/skills/devbox-*` are symlinks into the bundle. Bootstrap and startup re-point only symlinks that already point into a devbox bundle; a real directory or a link to elsewhere is left alone. Old devbox-owned links in `~/.claude/skills` are re-pointed at the bundle until Claude Code lists `devbox@devbox` in `~/.claude/plugins/installed_plugins.json`, then removed, so a session never loses its skills mid-upgrade.
- **Symlinked `settings.json`** (for example managed by dotfiles) is never written through. Add the marketplace and `enabledPlugins` entry there yourself, or install from the published marketplace: `claude plugin marketplace add cvillarr23/devbox` then `claude plugin install devbox@devbox`.

To disable it, run `claude plugin disable devbox@devbox`, or set `"enabledPlugins": {"devbox@devbox": false}` in `~/.claude/settings.json`; devbox keeps that choice. For Codex, delete the `~/.codex/skills/devbox-*` links, though startup recreates any that are missing. Replace a link with your own directory to keep it.

## Current-session detection

`devbox-session` prints the current session name; `devbox-session --json` adds the pane and detection source. `devbox-notes`, `devbox-open`, `devbox forward`, `devbox-msg` and `devbox-status show` use the same resolver, and an explicit `--session NAME` overrides it, except for `devbox-msg send`, whose sender can't be overridden.

Detection tries these in order: the pane a Claude background job is attached from, `DEVBOX_SESSION` (set by the remote relay), `ZELLIJ_SESSION_NAME`, and an exact Codex/Claude thread ID in the session agent registry with a matching live PID and process start time. Missing or ambiguous identity exits nonzero with an actionable error. Working directories and the existence of a single session are never used as guesses. Status hooks keep their silent no-op behavior outside a session.

## MCP server

`app/mcp_server.py` is a stdio MCP server that exposes session operations as typed tools. It calls the same functions as the CLI (`cli.py`), so the control socket, the relay and the validation are shared. The session comes from [current-session detection](#current-session-detection) on every call; outside a devbox session a tool returns an error result.

| Tool | Same as |
|---|---|
| `notes_read`, `notes_append` | `devbox-notes cat`, `append` |
| `open_url` | `devbox-open` |
| `msg_send`, `msg_inbox` | `devbox msg send`, `inbox` (inbox does not acknowledge) |
| `session_spawn` | `devbox spawn` |
| `session_kill` | `devbox kill`, limited to your own descendants and yourself |

Widget, todo, wait and delegated-control tools are not exposed yet.

Devbox registers the server as `devbox` in the user's `~/.claude.json` (`mcpServers`) and `~/.codex/config.toml` (`[mcp_servers.devbox]`), at home setup and on remote bootstrap. Other servers are never touched, and an existing `devbox` entry is kept. Managed MCP config is not used, because Claude treats it as the only MCP source.

Worktrees made by `devbox spawn --worktree` inherit Claude's folder trust: when the repo is already trusted in `~/.claude.json` (`projects[<path>].hasTrustDialogAccepted`), the new worktree is marked trusted so the child skips the trust dialog. Untrusted repos still prompt, and a missing, unparsable or symlinked `~/.claude.json` is left alone.

## Message urgency

Which `devbox-msg` kinds interrupt the receiver is configurable. An urgent message blocks the receiver's `Stop` until it has seen the message, and an idle agent gets a typed pointer line. Other messages wait for the receiver's next prompt. Urgency is resolved when the message is delivered, and the first match wins:

1. The message flag: `devbox msg send ... --urgent` or `--quiet`.
2. The recipient session's setting, kept in its meta as `messages: {"nudge": [kinds], "quiet": [kinds]}`. Set it with `devbox msg config SESSION --nudge done,update --quiet github` (`--reset` clears it), or `PATCH /api/sessions/{name}/meta`. `quiet` beats `nudge`.
3. The user config file, `$DEVBOX_CONFIG` or `~/.config/devbox/config.toml`, read by the devbox server:

   ```toml
   [messages]
   nudge = ["question", "blocker", "contract", "review", "github", "done"]  # replaces the default set
   nudge_after = 30    # seconds an urgent message waits before the nudge
   batch_window = 10   # wait this long after the newest message so a burst gets one line
   ```

   A missing or invalid file means the defaults.
4. The defaults: `question`, `blocker`, `contract`, `review` and `github` are urgent. `update` and `done` are not, except that a session which has spawned children (a PM) also nudges on `done`.

A message gets one pointer line per pane per batch, never one per message. An idle PM wakes within `nudge_after` + `batch_window` of a child's `done`. On a managed target the server resolves each message's urgency and the target stores it, but `nudge_after` and `batch_window` use the defaults there.

## Spawning child sessions

`devbox spawn [--name N] [--environment E] [--cwd DIR | --worktree BRANCH] [--agent claude|codex] [--model M] [--effort LEVEL] [--permission-mode plan|auto] [--prompt TEXT|-]` creates a session, optionally a git worktree beside the current repository, starts the agent with the prompt, and prints the child's name and URL. `--effort` is checked against a per-agent list (claude `--effort low|medium|high|xhigh|max`; codex `-c model_reasoning_effort=minimal|low|medium|high|xhigh`), is recorded in `spawn.json` and `resume.json`, and is passed again when a restart resumes the conversation; unset adds no flag. The child's `meta.json` records `parent: <session>[:<pane>]` from the detected session (never from an argument), and the dashboard API returns it as `parent`. The prompt gets one line naming the parent and how to message it. It works for local and managed environments; a worktree needs the child in the caller's own environment. The `devbox-spawn` skill covers when to spawn and how to brief a child.

`devbox kill NAME... [--self] [--children] [--yes]` ends sessions through the control socket (the runtime's `kill`: zellij `delete-session`, disconnect, archive the session directory). The server refuses the calling session without `--self` and unknown sessions; `--children` ends only the caller's children that are done or idle with no unread urgent messages, listing them and asking unless `--yes`. `--self` replies first, then ends the session. Exit code 0 only if every named session was ended. Over the control socket and the remote relay the server always limits targets to the caller itself (with `--self`) and its descendants (children, and their children); ending an unrelated session is a user action through the dashboard's authenticated HTTP API. This is a guard against mistakes and confused agents, not a security boundary: the in-pod access token (`<state>/access-token`, used by dotfiles' `devbox-pm`) still grants full API access.

## Loopback OIDC logins (`devbox auth-relay`)

CLIs such as `bao login -method=oidc` wait for the browser's redirect on `localhost:8250` inside the session, which a Mac or phone browser can't reach. With `DEVBOX_PUBLIC_URL` set, devbox serves the callback itself at `GET <DEVBOX_PUBLIC_URL>/oidc/callback`, behind the same auth as the rest of the app, and replays it to the session's loopback port:

```bash
devbox auth-relay --port 8250 -- bao login -method=oidc \
  callbackmethod=https callbackhost=devbox-next.roo-net.cc callbackport=443 skip_browser=true
```

The command prints an authorization URL. `auth-relay` registers its `state` with devbox for 5 minutes, then opens the URL in the viewer's browser (`devbox-open --target local`) and waits for the command to exit. When the browser lands on the callback, devbox looks up `state`, makes one `GET /oidc/callback?...` to `127.0.0.1:<registered port>` in that session's environment (through the forward for SSH/Kubernetes targets) and shows "Signed in, you can close this tab".

- `bao` always builds the redirect URI as `<callbackmethod>://<callbackhost>:<callbackport>/oidc/callback`, so the OpenBao OIDC role and the Google OAuth client must allow `https://<public host>:443/oidc/callback`. The path can't be changed.
- Registration is refused unless the URL's `redirect_uri` is this devbox's callback, and for ports below 1024 or ports devbox itself uses. Unknown, expired and already-used `state` values get a 4xx and nothing is replayed.
- With no `DEVBOX_PUBLIC_URL`, `auth-relay` reports why it can't relay and the command keeps running, so the callback URL can still be pasted back by hand.
- `gh auth login --web` needs none of this: it uses the device-code flow.

## Network behavior

Only the HTTP gateway is exposed (plus its token port, if `DEVBOX_TOKEN_PORT` is set). Individual ttyd servers, VNC, websockify, and forwarded preview ports listen on container loopback. SSH and Kubernetes exec connections originate from devbox. Remote helpers do not expose an inbound network listener.

Browser requests and remote forwarding use argument lists and quoted SSH commands. Environment targets come from operator-mounted configuration; the API does not accept arbitrary backend addresses. HTTP requests reject cross-origin mutations, and each viewer must claim a link before acknowledging it.

Behind a reverse proxy, forward the public `Host` header unchanged. The cross-origin check compares the `Origin` header's host with `Host`, so a proxy that rewrites `Host` to the upstream address makes every mutation fail with 403. When TLS ends at the proxy, set `DEVBOX_TRUST_PROXY=1` (with the proxy sending `X-Forwarded-Proto`) or `DEVBOX_SECURE_COOKIE=1` so the login cookie is `Secure`.

In `proxy` authentication mode, protect the whole gateway, including `/s/`, `/browser/`, and `/api/`, with the upstream authentication layer. There is no token form: `/login` redirects to the dashboard (the [token port](#token-port), if set, keeps its own). For local trials, use the default token mode and port-forwarding.

`cloudflare-access` mode removes the token form and verifies the Access JWT on every HTTP request and WebSocket upgrade, including `/s/`, `/browser/`, and `/api/`. The JWT comes from the `Cf-Access-Jwt-Assertion` header, falling back to the `CF_Authorization` cookie. Devbox checks the RS256 signature against the team's keys at `https://<team domain>/cdn-cgi/access/certs`, plus `aud`, `iss=https://<team domain>`, `exp` and `nbf` with 30 seconds of leeway. Keys are cached by `kid`; an unknown `kid` triggers a refetch, at most once every 30 seconds, and devbox rejects every request while it has no keys. Unlike `proxy` mode, a request that bypasses Access, such as one sent straight to the ClusterIP from another pod, gets `401`. `/healthz` stays open for probes. Startup fails if the team domain or AUD is missing. The pod must be able to reach the team domain over HTTPS.

## Token port

`DEVBOX_TOKEN_PORT` (for example `7681`) opens a second TCP listener next to `DEVBOX_PORT`. It is meant for a private fallback route, such as a Tailscale operator Ingress on the tailnet, that still works when Cloudflare or Access is down. The main port keeps its `DEVBOX_AUTH_MODE`; the token port always uses token auth: the `devbox_session` cookie or an `Authorization: Bearer` header, with the `/login` form for browsers. An Access JWT is neither needed nor accepted there.

Devbox picks the policy from the local port the connection was accepted on, never from `Host`, `X-Forwarded-*` or any other header, so a request to the main port can't reach token auth or `/login` in `cloudflare-access` mode. The cross-origin `Origin` check and the JSON content-type check apply on both ports. Startup creates the token file if needed and fails if the port is invalid or equals `DEVBOX_PORT`. When unset, only `DEVBOX_PORT` listens and nothing changes.

Don't publish the token port through a public ingress, tunnel or `LoadBalancer`: the token is then the only thing between the internet and a shell. Route only a private network to it, and rotate the token by replacing the token file and restarting.

## Runtime profiles

Compose selects `config/seccomp-docker.json` and the named `devbox-workspace`
AppArmor profile. Kubernetes selects the corresponding OCI profile
`devbox.json` from the kubelet's seccomp directory and the same AppArmor name.
The provided installer loads only these named profiles; it does not change
global kernel settings, add outer capabilities, or bypass agent approval policies.

`DEVBOX_APPARMOR_PROFILE` chooses the Compose AppArmor profile. On hosts without
AppArmor, use `unconfined`; the custom seccomp filter and Codex's own sandbox
remain enabled. Kubernetes nodes must have the OCI profile installed before
scheduling this workload. Docker Desktop on macOS has no AppArmor: use
`unconfined` there, with the custom seccomp profile, which covers both amd64
and arm64. `devbox doctor` reports the CPU architecture, the AppArmor label
and seccomp mode the container actually runs with, whether actual sandbox
execution succeeds, tool versions and hook permissions. For Codex and Claude
Code it also shows the latest published npm version and flags a stale pin
(`latest`/`stale`; omitted when offline).

## Agent versions

The image pins Codex and Claude Code (`CODEX_VERSION`, `CLAUDE_VERSION` in the
Dockerfile) under the root-owned `/usr/local` npm prefix, and sessions run as
UID 1000, so agent self-update cannot work. It is disabled instead:
`DISABLE_AUTOUPDATER=1` in the image environment and in
`/etc/claude-code/managed-settings.json`, and
`check_for_update_on_startup = false` in `/etc/codex/config.toml`. To update an
agent, bump its pin and cut a release; `devbox doctor` shows when a pin is stale.
