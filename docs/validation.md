# Validation

The unit suite covers session notes/conflicts, Claude attachment mapping, PID identity/reuse, Codex hook events and daemon state transitions, ambiguous target selection, URL validation, link claim/acknowledgment, authenticated gateway access, cross-origin rejection, startup restoration, and remote helper packaging.

```sh
python -m pytest app/tests tests -q
node --test app/tests/*.test.cjs
```

For a real container smoke test:

```sh
docker build -t devbox-test .
sudo bash scripts/install-runtime-profiles.sh
docker run -d --name devbox-ci --cap-drop ALL --security-opt no-new-privileges=true --security-opt seccomp=config/seccomp-docker.json --security-opt apparmor=devbox-workspace --shm-size=1g -p 127.0.0.1:17680:7680 devbox-test
python tests/container_smoke.py
docker rm -f devbox-ci
```

The smoke test uses an isolated session, executes the notes CLI over a real terminal WebSocket, verifies actual Codex sandbox execution, claims/acknowledges a local link, starts Chrome, fetches noVNC, completes a VNC handshake, opens a remote URL, and cleans up its session. The token is read internally and is never printed. CI runs this test before pushing images.

For a Kubernetes trial with an active port-forward:

```sh
python tests/container_smoke.py --namespace devbox-trial --url http://127.0.0.1:17860
```

Remote acceptance checks use a disposable SSH host and a prepared Kubernetes container. Explicitly bootstrap each target, create a session, run `devbox-notes append`, request a local link, and forward a localhost-only preview server. Terminate or replace the target, verify disconnected status, then reconnect explicitly. Narrow selectors must reject multiple ready pods.

Browser acceptance checks cover desktop terminal rendering, coarse-pointer mobile selection, the Chrome panel, and a clickable pending link after popup blocking. Real mobile keyboard/dictation behavior and user account OAuth flows still require the relevant device/account.

Chrome profile persistence is also checked through a forced replacement of a disposable container with a dedicated home volume. Native Codex hook execution is checked with a localhost-only mock provider and no account/model calls.
