# Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEVBOX_PORT` | `7680` | Container HTTP port |
| `DEVBOX_STATE_DIR` | `/data` | Notes, runtime registry, and generated access token |
| `DEVBOX_WORKSPACE` | `/workspace` | Local session starting directory |
| `DEVBOX_ENVIRONMENTS` | `/etc/devbox/environments.yaml` | Named target configuration |
| `DEVBOX_OPEN_TARGET` | `remote` | Destination used by `BROWSER` and `xdg-open` |
| `DEVBOX_AUTH_MODE` | `token` | Token login, or `proxy` for externally authenticated access |
| `DEVBOX_TOKEN_FILE` | `<state>/access-token` | Existing token file or generated token location |
| `DEVBOX_SECURE_COOKIE` | unset | Set `1` when HTTPS terminates at a reverse proxy |
| `DEVBOX_CHROME_SANDBOX` | `0` | Set `1` when the runtime permits Chrome's sandbox namespaces |
| `DEVBOX_CHROME` | `google-chrome` | Browser executable for a derived image |
| `DEVBOX_SOCKET` | `/tmp/devbox-control.sock` | Private local CLI socket, mode 0600 |
| `DEVBOX_CODEX_SOCKET` | autodetected | Optional existing local Codex daemon Unix socket |

`DEVBOX_HOST_PORT` and `DEVBOX_PROJECTS` are Compose substitutions, not application settings. `.env` is ignored by Git and excluded from image builds.

## Persistent directories

Docker uses a home volume at `/home/devbox` and a state volume at `/data`; projects are a bind mount at `/workspace`. Kubernetes uses one PVC for `/data/home`, `/data/state`, and `/workspace`. UID/GID 1000 must have write access.

The server generates a random access token only if the configured token file does not exist. Read it using `docker compose exec` or `kubectl exec`; it is not printed into server logs. Tokens must have at least 24 characters.

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

The container image installs managed agent hooks without changing mounted user config. Remote bootstrap uses user hook configuration and makes a `.pre-devbox` backup of existing JSON settings. Review Codex user hooks through `/hooks`; bootstrap does not bypass trust review or approval policies.

Bootstrap is explicit and repeatable. It checks Python/Zellij first, replaces the helper bundle, replaces only hooks whose command invokes devbox-status, and preserves unrelated settings. Existing skill directories are kept. Remote targets require Python 3.10 or newer.

## Current-session detection

`devbox-session` prints the current session name; `devbox-session --json` adds the pane and detection source. `devbox-notes`, `devbox-open`, `devbox forward` and `devbox-status show` use the same resolver, and an explicit `--session NAME` overrides it.

Detection tries these in order: the pane a Claude background job is attached from, `DEVBOX_SESSION` (set by the remote relay), `ZELLIJ_SESSION_NAME`, and an exact Codex/Claude thread ID in the session agent registry with a matching live PID and process start time. Missing or ambiguous identity exits nonzero with an actionable error. Working directories and the existence of a single session are never used as guesses. Status hooks keep their silent no-op behavior outside a session.

## Network behavior

Only the HTTP gateway is exposed. Individual ttyd servers, VNC, websockify, and forwarded preview ports listen on container loopback. SSH and Kubernetes exec connections originate from devbox. Remote helpers do not expose an inbound network listener.

Browser requests and remote forwarding use argument lists and quoted SSH commands. Environment targets come from operator-mounted configuration; the API does not accept arbitrary backend addresses. HTTP requests reject cross-origin mutations, and each viewer must claim a link before acknowledging it.

In `proxy` authentication mode, protect the whole gateway, including `/s/`, `/browser/`, and `/api/`, with the upstream authentication layer. For local trials, use the default token mode and port-forwarding.

## Runtime profiles

Compose selects `config/seccomp-docker.json` and the named `devbox-workspace`
AppArmor profile. Kubernetes selects the corresponding OCI profile
`devbox.json` from the kubelet's seccomp directory and the same AppArmor name.
The provided installer loads only these named profiles; it does not change
global kernel settings, add outer capabilities, or bypass agent approval policies.

`DEVBOX_APPARMOR_PROFILE` chooses the Compose AppArmor profile. On hosts without
AppArmor, use `unconfined`; the custom seccomp filter and Codex's own sandbox
remain enabled. Kubernetes nodes must have the OCI profile installed before
scheduling this workload. `devbox doctor` reports whether actual sandbox
execution succeeds, as well as tool versions and hook permissions.
