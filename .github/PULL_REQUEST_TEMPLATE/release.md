## Release vX.Y.Z

Release gate (see "Release gate" in `docs/operations.md`). Complete every step before opening the homelab-deployment pin bump for devbox-trial.

- [ ] Deployed the release candidate to devbox-dev with `scripts/devbox-dev-deploy.sh` (homelab-deployment, `docs/devbox-standalone.md`)
- [ ] Started 2+ sessions there, at least one Claude
- [ ] Restarted the pod: `kubectl -n devbox-dev rollout restart deploy/devbox`
- [ ] Confirmed each session came back connected, with its conversation resumed (#79)
