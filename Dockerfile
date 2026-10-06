# syntax=docker/dockerfile:1
FROM node:24.21.0-bookworm-slim AS ttyd-build
ARG TTYD_COMMIT=2922cb89f518bae4d0fcf4d757a7419638fc71fc
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential cmake libjson-c-dev libwebsockets-dev libuv1-dev libssl-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/tsl0922/ttyd.git /src && cd /src && git checkout "$TTYD_COMMIT" && cmake -S . -B build && cmake --build build -j2 && tar --exclude=.git --exclude=build -czf /ttyd-source.tar.gz -C /src .

FROM node:24.21.0-bookworm-slim
ARG ZELLIJ_VERSION=0.41.2
ARG CODEX_VERSION=0.160.0
ARG CLAUDE_VERSION=2.1.289
ARG KUBECTL_VERSION=v1.35.8
ARG CHROME_VERSION=154.0.8037.97-1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client python3 python3-venv libjson-c5 libwebsockets17 libwebsockets-evlib-uv libuv1 libssl3 zlib1g tini xvfb x11vnc openbox novnc websockify fonts-liberation fonts-dejavu procps htop xauth && rm -rf /var/lib/apt/lists/*
RUN test "$(dpkg --print-architecture)" = amd64 && curl --retry 3 --retry-all-errors -fsSLo /tmp/chrome.deb "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb" && apt-get update && apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*
RUN curl --retry 3 --retry-all-errors -fsSLo /tmp/zellij.tar.gz "https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-x86_64-unknown-linux-musl.tar.gz" && tar -xzf /tmp/zellij.tar.gz -C /usr/local/bin zellij && rm /tmp/zellij.tar.gz
RUN curl --retry 3 --retry-all-errors -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && curl --retry 3 --retry-all-errors -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c && chmod +x /usr/local/bin/kubectl && rm /tmp/kubectl.sha256
RUN npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} && npm cache clean --force
COPY --from=ttyd-build /src/build/ttyd /usr/local/bin/ttyd
COPY --from=ttyd-build /ttyd-source.tar.gz /usr/local/share/devbox/ttyd-source.tar.gz
LABEL org.opencontainers.image.source="https://github.com/cvillarr23/devbox"
LABEL org.opencontainers.image.licenses="MIT"
WORKDIR /opt/devbox
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app app
COPY bin bin
COPY config config
RUN usermod -l devbox node && groupmod -n devbox node && usermod -d /home/devbox devbox && mkdir -p /home/devbox /workspace /data /etc/devbox && chown -R 1000:1000 /home/devbox /workspace /data && PYTHONPATH=/opt/devbox/app /opt/venv/bin/python -c 'from pathlib import Path; from bootstrap import install_hooks; install_hooks(Path("/opt/devbox/app"), "/home/devbox", managed=True)' && chown -R 1000:1000 /home/devbox
ENV PATH="/opt/devbox/bin:/opt/venv/bin:${PATH}" HOME=/home/devbox DEVBOX_STATE_DIR=/data DEVBOX_WORKSPACE=/workspace DEVBOX_ENVIRONMENTS=/etc/devbox/environments.yaml BROWSER=/opt/devbox/bin/devbox-open
USER 1000:1000
EXPOSE 7680
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/devbox/bin/entrypoint"]
