# Third-party components

Devbox's MIT license applies to its own code. Included tools retain their respective licenses and distribution terms.

- **Driftty**: https://github.com/mdp/driftty, revision recorded in `app/web/vendor/driftty/REVISION`. Its MIT license is preserved beside the bundled client, including the upstream copyright notices.
- **ttyd**: https://github.com/tsl0922/ttyd, commit `2922cb89f518bae4d0fcf4d757a7419638fc71fc`. GPL-3.0; the image includes corresponding source and build inputs at `/usr/local/share/devbox/ttyd-source.tar.gz`. This repository's Dockerfile documents the compilation procedure.
- **Zellij**: https://github.com/zellij-org/zellij, version 0.41.2, MIT.
- **noVNC**: https://github.com/novnc/noVNC, Debian package; component licenses are preserved under `/usr/share/novnc` and `/usr/share/doc/novnc` in the image.
- **websockify, x11vnc, Xvfb, Openbox**, and other Debian packages: original package copyright and license files are retained under `/usr/share/doc` in the image.
- **Codex CLI** and **Claude Code**: pinned npm packages, with their upstream license notices retained in their installed packages.
- **Google Chrome**: distributed under its original Google Chrome terms and bundled third-party notices. Devbox's MIT license does not apply to Chrome.
- **Node, Python, kubectl**, and Python dependencies: their original upstream licenses apply.

- **Docker seccomp policy**: https://github.com/moby/profiles, commit `2ceae35d351c156cb5a8efc0fdc4a08cf94569d8`. Apache-2.0; license preserved in `config/MOBY-LICENSE`. Devbox adds private namespace/mount support and resolves an OCI variant for Kubernetes.
