# Devbox

A personal development environment with browser terminals, Claude Code and Codex status, session notes, and on-demand Chrome through web VNC. Run it with Docker or Kubernetes, and work locally or on managed SSH/Kubernetes targets.

Each installation belongs to one trusted user. Share this project by deploying separate instances.

## Runtime prerequisites

Codex uses Bubblewrap's private namespaces for normal sandboxed tool execution.
Default container policies block those operations. Devbox ships a dedicated
AppArmor profile and a seccomp policy derived from Docker's default: other
blocked syscalls stay blocked, while namespace/mount operations are enabled.
Kernel namespace ownership checks, an unprivileged UID, dropped outer
capabilities, and Codex's own filesystem/approval policies remain in force.

On Linux hosts with AppArmor, install the profiles before starting devbox:

```sh
sudo bash scripts/install-runtime-profiles.sh
```

This installs only `/etc/apparmor.d/devbox-workspace` and
`/var/lib/kubelet/seccomp/devbox.json`; it does not change global sysctls or
existing profiles. Run it on each eligible Kubernetes node. Set
`DEVBOX_KUBELET_SECCOMP_ROOT` if the kubelet uses another seccomp directory.
On Docker hosts without AppArmor, set `DEVBOX_APPARMOR_PROFILE=unconfined`;
for Kubernetes without AppArmor, use an overlay selecting AppArmor's
`Unconfined` type while retaining the provided seccomp policy.

See [OpenAI's sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing)
and [Docker's seccomp documentation](https://docs.docker.com/engine/security/seccomp/).

## Docker quickstart

```sh
git clone https://github.com/cvillarr23/devbox.git
cd devbox
mkdir -p workspace
cp .env.example .env
docker compose up -d --build
docker compose exec devbox sh -c 'cat /data/access-token; printf "\n"'
```

Open <http://localhost:7680> and enter the access token. Create a session, choose its environment, and open its terminal. Change `DEVBOX_HOST_PORT` if port 7680 is already occupied.

Home, agent login configuration, Chrome's profile, and session state persist in Docker volumes. Projects use the directory configured by `DEVBOX_PROJECTS`. Mount writable directories owned by UID/GID 1000. No credentials are built into the image; log in to the agent CLIs from a terminal or supply your configuration at runtime.

To use `gh`, run `gh auth login` in a session and choose the device-code flow, which needs no loopback callback. The token is stored in `~/.config/gh` and persists in the home volume; nothing is baked into the image.

Images are published for `linux/amd64` and `linux/arm64` under the same tag, so Docker pulls the native build. The initial release includes pinned Codex, Claude Code, Zellij, ttyd, Node, Python, kubectl, the GitHub CLI (`gh`), and Chrome versions. Extend the Dockerfile for project-specific dependencies.

## macOS (Apple Silicon, Docker Desktop)

Devbox runs natively on `linux/arm64`, so Docker Desktop doesn't emulate it. Docker Desktop's Linux VM has no AppArmor, so select the unconfined AppArmor profile; the custom seccomp filter and Codex's own sandbox stay enabled. Point `DEVBOX_PROJECTS` at a directory Docker Desktop shares (`/Users` is shared by default):

```sh
cp .env.example .env
sed -i '' 's/^DEVBOX_APPARMOR_PROFILE=.*/DEVBOX_APPARMOR_PROFILE=unconfined/' .env
sed -i '' "s|^DEVBOX_PROJECTS=.*|DEVBOX_PROJECTS=$HOME/dev|" .env
docker compose up -d --build
docker compose exec devbox devbox doctor
```

`devbox doctor` should report `platform.machine: aarch64`, `confinement.seccomp: 2`, `confinement.apparmor: unconfined` and `sandbox.supported: true`. Docker Desktop's file sharing maps ownership of bind-mounted files, so the container's UID 1000 can write files you own in the projects directory.

The in-container Chrome (Google Chrome for Linux arm64) works through the **Browser** panel as on Linux. It is a separate profile from your Mac's Chrome.

## Kubernetes quickstart

```sh
kubectl create namespace devbox-trial
kubectl -n devbox-trial apply -k deploy/kubernetes
kubectl -n devbox-trial rollout status deployment/devbox
kubectl -n devbox-trial exec deployment/devbox -- sh -c 'cat /data/state/access-token; printf "\n"'
kubectl -n devbox-trial port-forward service/devbox 17860:7680
```

Open <http://localhost:17860>. The manifests use one replica, a 20Gi ReadWriteOnce PVC for home and state, a 40Gi one for `/workspace`, a ClusterIP Service, health probes, and a memory-backed `/dev/shm`. Customize storage, resources, and environment configuration with Kustomize. The default ServiceAccount token is not mounted.

A container or pod replacement ends local processes. Project files, notes, browser profile, and session definitions persist; use **Reconnect** to start a new terminal. Reconnect never replays previous shell commands.

Back up, restore, upgrade and roll back as described in [docs/operations.md](docs/operations.md).

## Shell and private dotfiles

New local terminal panes use Zsh and a pinned Oh My Zsh installation. The
first startup seeds shell configuration only when it is absent. Existing
custom shell files are preserved, and existing panes keep their running shell.

Supply personal dotfiles at runtime, for example at `~/dev/dotfiles`, then run:

```sh
devbox-setup-home --dotfiles "$HOME/dev/dotfiles"
```

Devbox knows nothing about the checkout's layout. After seeding its own shell
files it backs up any custom `~/.zshrc` and `~/.zshenv`, then runs the
checkout's documented installer, `install.sh`, as the session user from inside
the checkout. Use `--dotfiles-installer PATH` (relative to the checkout) to run
a different script. What the installer sets up (shell files, Roo Coder, agent
plugins, skills, MCP registrations) and how it preserves existing registrations
is the dotfiles repo's business. Choose components by forwarding the
installer's own arguments with a repeatable `--dotfiles-arg`, for example
`--dotfiles-arg=--no-roo-coder`. Omit `--dotfiles` to run no installer at all;
the image's shell seeding works on its own. Restart your shell with `exec zsh`
or create a new session afterwards.

`--roo-coder` is deprecated, prints a warning and does nothing. It will be
removed in the next release; the dotfiles installer installs Roo Coder.

The installer runs only when you invoke `devbox-setup-home --dotfiles`, never
automatically at startup, and devbox never downloads private repos. No private
dotfiles, Git credentials, secrets, or Roo Coder source are included in the
public image. Copy a checkout without its `.git` directories, or mount it using
your deployment's configuration.

The shell setup keeps the image's Claude launcher working and uses the local
`devbox` CLI instead of a dotfiles shortcut to a separate host. Agent settings,
hooks, account credentials, and plugin configuration are not replaced.

## Environments

Edit `config/environments.yaml` for Docker or `deploy/kubernetes/environments.yaml` for Kubernetes:

```yaml
environments:
  local:
    type: local
  server:
    type: ssh
    host: development-server
  project-pod:
    type: kubernetes
    context: development
    namespace: development
    selector: app=project-workspace
    container: workspace
```

SSH targets use aliases from the mounted `~/.ssh/config`, strict host-key verification, and key authentication. Kubernetes targets use a supplied kubeconfig; the selector must match exactly one running pod with the named container ready. Use dedicated credentials scoped to the intended targets.

Remote targets require Python 3.10+, Zellij, and the desired agent CLIs. Install the user-scoped helper explicitly:

```sh
docker compose exec devbox devbox env list
docker compose exec devbox devbox env bootstrap server
```

Bootstrap installs under `~/.local/share/devbox`, merges only devbox's user hooks, and preserves other hooks/settings. Codex user hooks require review in `/hooks`. For ephemeral pods, prepare dependencies in the target image and rerun bootstrap after replacement. The helper does not install operating-system packages or start an agent.

Select the environment when creating a terminal session. Remote terminal processes and their Zellij sessions live on the target. Status, notes, and link requests travel over a separate control connection. A lost target is marked disconnected; explicitly reconnect to resolve its replacement. Devbox never follows a replacement pod automatically.

## Browser and agent commands

Chrome starts only when requested and has one persistent profile shared by this devbox's sessions. Use **Browser** to view it, **Back to terminal** to hide it, and **Stop shared browser** to release its processes.

```sh
devbox-open --target remote https://example.com  # Chrome inside devbox
devbox-open --target local https://example.com   # The user's viewing browser
devbox forward 3000                             # Selected remote localhost server
```

For a remote preview, open the URL returned by `devbox forward` with `--target remote`. That localhost URL belongs to devbox; it is not a URL for the user's local browser. Forwards listen on devbox's loopback interface and end with the session connection.

`BROWSER` and `xdg-open` use `DEVBOX_OPEN_TARGET`, normally `remote`. Agents can choose either destination using the CLI and bundled `devbox-browser` skill. No MCP server is required.

A local link request is queued for the session viewer and claimed once. Browser popup blocking or an absent viewer leaves a clickable pending link in the sidebar. A `pending` CLI result does not claim the page opened. Only HTTP(S) URLs without embedded credentials are accepted.

Chrome's internal sandbox is disabled by default because Docker's default seccomp policy blocks Chrome's namespace setup. It runs as the same unprivileged user inside this trusted single-user container. Set `DEVBOX_CHROME_SANDBOX=1` on a deployment whose namespace policy supports Chrome's sandbox.

On a Mac, agents can also drive the host's own Chrome: Claude Code through Claude in Chrome, and other agents over CDP to a dedicated profile. See [host Chrome](docs/host-chrome.md).

## Clipboard

Browser terminals can't reach a system clipboard, so devbox ships copy-only helpers that send an OSC 52 sequence to the terminal, and the browser sets your local clipboard:

```sh
echo hi | clip                      # stdin, or arguments
echo hi | xclip -selection clipboard
echo hi | xsel -bi
wl-copy hi
```

`xclip`, `xsel`, `wl-copy` and `wl-paste` exist so CLIs that probe for them (`gh auth login --web`, Claude Code) find a clipboard. Read modes (`xclip -o`, `xsel -o`, `wl-paste`) exit 1 with a message, because a browser terminal can't read the clipboard safely. Every selection (clipboard, primary) maps to the browser clipboard. Payloads over 100 KB are refused. With no terminal (cron, pipes) the helpers exit 0 and write nothing. Zellij forwards the sequence to the browser without extra configuration.

## Agent status and notes

The dashboard recognizes Claude Code and Codex processes independently of hook activity. Missing hooks show **status unavailable** rather than hiding the agent. Hooks report working, waiting, completion, interruption, and exit. Completion badges clear when acknowledged.

Codex launches inside devbox use `--no-daemon` to keep terminal identity with the agent. Devbox also reads session metadata from an existing local Codex daemon when it can unambiguously match a terminal. It never reads conversation transcripts for status. PID start times prevent stale status from being attached to a reused PID.

```sh
devbox doctor
devbox-notes cat
devbox-notes append "Verified the preview"
printf '%s\n' 'Replacement notes' | devbox-notes write
```

Notes are shared with the sidebar. Browser edits use version checks to detect conflicts. Remote CLI notes are relayed to the central store. Killing a session ends its processes and archives its notes/metadata.

## Messages between sessions

Agents in different sessions message each other with `devbox-msg` (also `devbox msg`). The `devbox-msg` skill tells agents when to use each kind.

```sh
devbox-msg who                                       # me, parent, children, siblings
devbox-msg send parent --kind question "A or B? I lean A because ..."
devbox-msg send ui-143:3 --kind review "Round 1 posted on PR #12"
devbox-msg inbox --unread
devbox-msg ack --all
```

An address is `SESSION`, `SESSION:PANE`, `parent`, `children` or `siblings`. The last three resolve through the parent link `devbox spawn` records. A bare session goes to its only agent. When a session runs several agents, `send` refuses and lists their panes. The sender always comes from session detection, never from an argument.

Each message is stored in the recipient's own state directory, so it survives the recipient being offline and container restarts. Messages to a disconnected managed target wait on devbox until it reconnects. The agent hook shows unread messages on the next prompt or session start. Urgent kinds (`question`, `blocker`, `contract`, `review`, `github`) also hold a busy agent at the end of its turn. An idle agent gets a typed one-line pointer after about 30 seconds. Nothing is ever typed into a working or waiting agent. Session cards and the view toolbar show the unread count.

## Configuration and access

See [configuration](docs/configuration.md) for variables, mounts, credential examples, and runtime behavior.

Docker binds to localhost by default. Kubernetes exposes a ClusterIP. Token authentication protects the gateway, API, terminal WebSockets, and VNC. For external access, use HTTPS and an authenticated reverse proxy. `DEVBOX_AUTH_MODE=proxy` disables app token checks and is intended only when every route is protected by that proxy. Behind Cloudflare Access, prefer `DEVBOX_AUTH_MODE=cloudflare-access`, which verifies the Access JWT itself (see `docs/configuration.md`).

No host networking, privileged container, host Docker socket, or preconfigured personal secrets are required.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest app/tests tests
node --test app/tests/*.test.cjs
```

Container integration tests exercise real ttyd/Zellij connections, Chrome/VNC, helper bootstrapping, remote notes/link relaying, and localhost forwarding. See [validation](docs/validation.md).

The Driftty mobile client is pinned in `app/web/vendor/driftty`. Rebuild it with `bash app/build-driftty.sh` using Node >=22.12.

## License

Devbox code is MIT licensed. Bundled components retain their original licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). The image includes the pinned ttyd corresponding source at `/usr/local/share/devbox/ttyd-source.tar.gz`.
