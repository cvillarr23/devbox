# Operations: storage, backup, restore and upgrades

## What persists

| Data | Docker | Kubernetes |
|---|---|---|
| Home: shell config, agent logins and settings, Git/SSH config, Chrome profile | `devbox-home` volume at `/home/devbox` | `devbox` PVC, `/data/home` |
| State: sessions, notes, metadata, `access-token` | `devbox-state` volume at `/data` | `devbox` PVC, `/data/state` |
| Projects | `DEVBOX_PROJECTS` bind mount at `/workspace` | `devbox-workspace` PVC at `/workspace` |

Running processes don't persist: terminals, agents, Chrome and port forwards end when the container or pod is replaced. On startup devbox restores local sessions (see below); a session that is still stopped, such as a remote-target one, is reconnected by opening `/view/<name>`, which reconnects automatically and resumes the agent's last conversation (once per page load, and again when the tab becomes visible, unless an attempt failed in the last 30s), or by pressing **Reconnect** on the dashboard card or the view toolbar. The "Reconnect and resume the agent automatically when opened" toggle in the view's settings widget turns the automatic part off per browser. Reconnect starts a fresh terminal from the default layout. It never replays shell commands: devbox doesn't let Zellij serialize sessions, and it deletes any copy of an exited session left in `~/.cache/zellij` by an older release, so it can't be resurrected. `tests/replacement_check.py` checks this.

What survives is files: notes, worktrees, and `sessions/<name>/resume.json`, which the agent hooks keep up to date with the last Claude or Codex conversation (session id, model, permission mode, cwd). When the server starts it recreates every local session that was connected before the restart, with no click or open page needed; remote-target sessions stay stopped and need Reconnect. For a session with a resume record, the main pane runs `claude --resume <id> --model … --permission-mode …` or `codex resume <id>` straight away. `--resume` only reopens the conversation and sends no prompt, and the initial prompt is never replayed. The pane gets the permission mode the conversation was last seen in, except `bypassPermissions`, which a restart never turns back on. A mode switched after the last report from the agent's hooks isn't known. If the resume fails (expired or missing conversation) the pane drops to a shell with a hint (`claude --resume` with no id opens the picker). With no record, a session comes back with the plain default layout. Reconnect does the same for one session by hand. The dashboard card shows "Resume" for a stopped session that has a record. For a spawned session only the spawned agent is remembered; otherwise the last agent to report wins.

If a session's working directory was deleted (for example its worktree was removed after a merge), attaching starts in the nearest existing parent directory, else `$HOME`, and prints a one-line notice; the session list marks such local sessions with `cwd_missing: true`.

## Sizing

Home and state are small: tens of MB, plus Chrome profiles and agent homes. Projects grow with every clone and worktree. Projects get their own 40Gi `devbox-workspace` volume, so a full workspace can't break logins or session state. Projects and home are also backed up as separate archives.

The default `csi-rawfile-default` class on the homelab cluster can't expand a volume in place. To grow one:

1. Back up.
2. Scale to 0.
3. Delete the PVC.
4. Raise the size in the overlay and sync.
5. Restore.

rawfile also counts the full requested size of every volume as reserved. It refuses new volumes (`ResourceExhausted: Not enough disk space`, PVC stays Pending) once the requests outgrow the node's free space, even when the disk itself has room.

## Ownership, storage and credentials

- Devbox runs as UID/GID 1000 with `fsGroup: 1000`. Every mounted volume must be writable by 1000. Restores keep numeric ownership.
- PVCs are `ReadWriteOnce`, and the Deployment uses `Recreate`, so only one pod writes at a time.
- The homelab rawfile volumes are node-local and have reclaim policy `Delete`. Deleting the PVC deletes the data, and so does pruning it from Argo CD, renaming the Application or namespace, or removing the resource from kustomize. Take a backup before any of these.
- Backups contain credentials: agent OAuth logins, Git and SSH configuration, kubeconfigs, any dotfiles in the home, and the devbox access token. The script writes them to a mode 0700 directory with mode 0600 files. Keep them out of Git. Encrypt them before they leave the host (for example restic). Delete old ones you don't need.
- Restoring a backup restores its `access-token`, so the token from the backup is valid again. To rotate the token, delete `<state>/access-token` and restart.
- Mounted Secrets (SSH keys, kubeconfig) aren't on the volumes. They are restored from their own source of truth, such as OpenBao or External Secrets.

## Backup and restore

`scripts/devbox-backup.sh` stops devbox and archives every volume it mounts. It writes one `<volume>.tar.gz` per volume plus `SHA256SUMS`, then starts devbox again, even if the copy fails. Stopping devbox makes the copy consistent: Chrome, Git and the JSON state files are quiescent. Expect about a minute of downtime and a Reconnect afterwards.

### Kubernetes

```sh
scripts/devbox-backup.sh k8s backup -n devbox-trial --argo-app devbox-trial
# -> ~/backups/devbox/devbox-trial-<UTC timestamp>/{devbox,devbox-workspace}.tar.gz, SHA256SUMS
scripts/devbox-backup.sh k8s restore ~/backups/devbox/devbox-trial-<timestamp> -n devbox-trial --argo-app devbox-trial
```

- The script reads the PVC names and the image from the Deployment. It copies through a short-lived `devbox-backup-helper` pod that runs that image as UID 1000 and mounts each PVC at `/pvc/<name>`.
- Restore first checks the checksums and that every mounted PVC has an archive. It then empties each volume (keeping `lost+found`) and unpacks the archive.
- Restore into a new PVC: create it with the same name, then run restore.
- `--argo-app` is required for a Deployment that Argo CD manages. Without it, self-heal would scale devbox back up in the middle of the copy. The script pauses the Application's automated sync, then puts the original policy back on exit. Set `ARGO_NAMESPACE` if Argo CD doesn't run in `argocd`.

### Docker

```sh
scripts/devbox-backup.sh docker backup -c devbox-devbox-1
scripts/devbox-backup.sh docker restore ~/backups/devbox/devbox-devbox-1-<timestamp> -c devbox-devbox-1
```

The script archives every named volume the container mounts. The `DEVBOX_PROJECTS` bind mount is a host directory, so back it up with your host tooling.

### Schedule

Take a backup before every upgrade, and on a schedule. A scheduled, offsite (restic) backup for the homelab deployment is tracked separately.

## Selecting image versions

Each devbox release is a Git tag `vX.Y.Z`. At that tag, `deploy/kubernetes/kustomization.yaml` (`images: newTag`) and `compose.yaml` (`image:`) pin the same image `ghcr.io/cvillarr23/devbox:X.Y.Z`. CI checks this with `tests/manifest_check.py`.

- **GitOps (Argo CD):** the Application's `spec.source.targetRevision` names a devbox tag, for example `v0.1.7`. Upgrading means changing it to the new tag. Rolling back means reverting that commit, which goes back to the previous tag, and therefore to both the previous image and the previous manifests. Don't point the Application at a branch or `main`. Don't let Image Updater bump the image separately from the manifests.
- **Docker:** check out the release tag (or edit `image:` in `compose.yaml`), then run `docker compose pull && docker compose up -d`. To roll back, check out the previous tag and repeat.

The volumes keep working across upgrades and rollbacks between releases. A release that changes the volume layout says so in its notes, together with the move step. v0.1.8, for example, moves `/workspace` from the `devbox` PVC's `workspace/` subdirectory to the `devbox-workspace` PVC. Before syncing it, copy any projects in `/data/workspace` across, or remove them once you have a backup.

Every upgrade or rollback replaces the pod (`Recreate`):

1. Back up.
2. Change the tag.
3. Wait for the rollout.
4. Check notes, projects and the Chrome profile.
5. Reconnect the sessions you need.

## Release gate

Every devbox-trial bump restarts the pod and kills the live sessions, so a release candidate has to survive a restart on devbox-dev first. Work through these steps, each one a checkbox in the release PR template (`.github/PULL_REQUEST_TEMPLATE/release.md`; open the PR with `?template=release.md`):

1. Deploy the release candidate to devbox-dev with `scripts/devbox-dev-deploy.sh` in homelab-deployment (see its `docs/devbox-standalone.md`). The script needs `jq`, which the devbox image includes, so you can run it from inside devbox-trial.
2. Start 2+ sessions there, at least one of them Claude.
3. Restart the pod: `kubectl -n devbox-dev rollout restart deploy/devbox`.
4. Confirm each session comes back connected, with its conversation resumed (#79).

Only then open the homelab-deployment pin bump for devbox-trial.

## PR event notifications

The runtime checks the user's recent PRs once a minute (one `gh api graphql` search, using the runtime's own `gh` auth; if `gh` is missing or logged out it logs once and skips). A PR belongs to the local session whose view URL (`DEVBOX_PUBLIC_URL`/view/NAME) appears in the PR body, else to the session whose cwd has the PR's repository and branch checked out. Events:

| Event | Message |
| --- | --- |
| merged | `github` to the owner, `done` to its parent |
| review (changes requested, comment, approval) | `review` to the owner |
| failed check (new check or new head commit) | `blocker` to the owner |

Seen events persist in `<state>/pr-events.json`. A PR seen for the first time is recorded silently, and a merge only notifies when it happened after the watcher started, so a redeploy or lost state file does not replay old merges. Sessions on remote targets are not watched.
