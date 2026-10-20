#!/usr/bin/env bash
# Probe which host-browser paths work for agents in a devbox container on this
# machine. Written for Docker Desktop on an Apple Silicon Mac; also runs on a
# Linux Docker host. It only starts throwaway containers and a temporary Chrome
# profile, and prints a PASS/FAIL/SKIP report that can be pasted into an issue.
#
# Usage: scripts/spike-host-chrome-probe.sh [--only c4c|cdp|chromium]...
#
# Environment:
#   DEVBOX_CONTAINER   running devbox container for the Claude in Chrome check
#                      (default: the Compose service container, if running)
#   CHROME_BIN         host Chrome executable (default: macOS Google Chrome)
#   CDP_PORT           host port for the temporary debugging Chrome (default 9222)
#   PROBE_NAVIGATE     1 (default) also opens example.com through Claude in Chrome;
#                      approve the pairing or site prompt in Chrome if it appears
set -uo pipefail

NODE_IMAGE=node:22.23.3-alpine
DEVTOOLS_MCP=chrome-devtools-mcp@1.11.0
CHROMIUM_IMAGE=debian:trixie-slim
CDP_PORT=${CDP_PORT:-9222}
PROBE_NAVIGATE=${PROBE_NAVIGATE:-1}
ONLY=()
while [ $# -gt 0 ]; do
  case $1 in
    --only) ONLY+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
want() { [ ${#ONLY[@]} -eq 0 ] && return 0; local s; for s in "${ONLY[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/devbox-chrome-probe.XXXXXX")
CHROME_PID=
cleanup() {
  [ -n "$CHROME_PID" ] && kill "$CHROME_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

# One Node script serves every container-side check, so the containers need
# nothing beyond Node 22 (global fetch and WebSocket).
cat >"$WORK/probe.mjs" <<'EOF'
import { spawn } from 'node:child_process';
import { lookup } from 'node:dns/promises';
const out = (status, name, detail = '') => console.log(`${status} ${name}${detail ? ': ' + detail : ''}`);
const short = (s, n = 300) => String(s).replace(/\s+/g, ' ').slice(0, n);

class Mcp {
  constructor(cmd, args, env) {
    this.p = spawn(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, ...env } });
    this.buf = ''; this.waiters = new Map(); this.id = 0; this.stderr = '';
    this.p.stdout.on('data', d => {
      this.buf += d;
      let i;
      while ((i = this.buf.indexOf('\n')) >= 0) {
        const line = this.buf.slice(0, i); this.buf = this.buf.slice(i + 1);
        try { const m = JSON.parse(line); this.waiters.get(m.id)?.(m); } catch {}
      }
    });
    this.p.stderr.on('data', d => { this.stderr = (this.stderr + d).slice(-4000); });
  }
  send(m) { this.p.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...m }) + '\n'); }
  request(method, params, ms = 30000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.waiters.delete(id); reject(new Error(`${method} timed out after ${ms / 1000}s`)); }, ms);
      this.waiters.set(id, m => { clearTimeout(t); this.waiters.delete(id); m.error ? reject(new Error(short(JSON.stringify(m.error)))) : resolve(m.result); });
      this.send({ id, method, params });
    });
  }
  async init() {
    await this.request('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'devbox-probe', version: '0' } }, 120000);
    this.send({ method: 'notifications/initialized' });
    return (await this.request('tools/list', {})).tools.map(t => t.name);
  }
  async call(name, args = {}, ms = 60000) {
    const r = await this.request('tools/call', { name, arguments: args }, ms);
    const text = (r.content || []).filter(c => c.type === 'text').map(c => c.text).join('\n');
    if (r.isError) throw new Error(short(text));
    return text;
  }
  close() { this.p.kill(); }
}

async function claudeInChrome(label) {
  const mcp = new Mcp('claude', ['--claude-in-chrome-mcp']);
  try {
    const tools = await mcp.init();
    out(tools.includes('list_connected_browsers') ? 'PASS' : 'FAIL', `${label} MCP server`, `${tools.length} tools`);
    const listed = await mcp.call('list_connected_browsers', {}, 60000);
    let browsers;
    try { browsers = JSON.parse(listed); } catch { throw new Error(short(listed)); }
    out(browsers.length ? 'PASS' : 'FAIL', `${label} bridge sees browsers`,
      browsers.length ? browsers.map(b => `${b.name || b.displayName || '?'} (${b.platform || b.os || '?'}, local=${b.isLocal})`).join('; ')
        : 'none: is Chrome open with the Claude extension signed in to the same claude.ai account?');
    if (!browsers.length || process.env.PROBE_NAVIGATE === '0') return;
    if (browsers.length === 1) await mcp.call('select_browser', { deviceId: browsers[0].deviceId });
    else out('INFO', `${label} several browsers`, 'approve the pairing prompt in the Chrome you want to use');
    const nav = await mcp.call('navigate', { url: 'https://example.com' }, 180000);
    const tabId = Number((/"tabId"\s*:\s*(\d+)/.exec(nav) || /tabId (\d+)/.exec(nav) || [])[1]);
    const text = Number.isFinite(tabId) ? await mcp.call('get_page_text', { tabId }, 60000) : nav;
    out(/Example Domain/.test(text) ? 'PASS' : 'FAIL', `${label} navigate+read`, short(text, 120));
    if (Number.isFinite(tabId)) await mcp.call('tabs_close_mcp', { tabId }).catch(() => {});
  } catch (e) {
    out('FAIL', `${label} Claude in Chrome`, `${e.message}; stderr: ${short(mcp.stderr)}`);
  } finally { mcp.close(); }
}

async function cdp(host, port) {
  let ip;
  try { ip = (await lookup(host, { family: 4 })).address; out('PASS', 'resolve', `${host} -> ${ip}`); }
  catch (e) { out('FAIL', 'resolve', `${host}: ${e.message}`); return; }
  for (const [label, h] of [['by name', host], ['by IP', ip]]) {
    try {
      const r = await fetch(`http://${h}:${port}/json/version`, { signal: AbortSignal.timeout(5000) });
      const body = await r.text();
      out(r.ok ? 'PASS' : 'FAIL', `/json/version ${label}`, `HTTP ${r.status} ${short(body, 140)}`);
    } catch (e) { out('FAIL', `/json/version ${label}`, e.cause?.code || e.message); }
  }
  let ws;
  try {
    const v = await (await fetch(`http://${ip}:${port}/json/version`, { signal: AbortSignal.timeout(5000) })).json();
    out('INFO', 'webSocketDebuggerUrl', v.webSocketDebuggerUrl);
    ws = new WebSocket(v.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('WebSocket error')); setTimeout(() => rej(new Error('WebSocket timeout')), 5000); });
    let n = 0; const pending = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); pending.get(m.id)?.(m); };
    const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
      const id = ++n; pending.set(id, m => m.error ? rej(new Error(m.error.message)) : res(m.result));
      ws.send(JSON.stringify({ id, method, params, ...(sessionId && { sessionId }) }));
      setTimeout(() => rej(new Error(`${method} timeout`)), 20000);
    });
    const { product } = await send('Browser.getVersion');
    const { targetId } = await send('Target.createTarget', { url: 'https://example.com' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    let title = '';
    for (let i = 0; i < 20 && !/Example/.test(title); i++) {
      await new Promise(r => setTimeout(r, 500));
      title = (await send('Runtime.evaluate', { expression: 'document.title', returnByValue: true }, sessionId)).result.value;
    }
    await send('Target.closeTarget', { targetId });
    out(/Example/.test(title) ? 'PASS' : 'FAIL', 'CDP drive host Chrome', `${product}, page title "${title}"`);
  } catch (e) { out('FAIL', 'CDP WebSocket', e.message); }
  finally { ws?.close(); }
  return ip;
}

async function devtoolsMcp(url) {
  const [cmd, ...args] = process.env.DEVTOOLS_MCP_CMD.split(' ');
  const mcp = new Mcp(cmd, [...args, `--browser-url=${url}`]);
  try {
    const tools = await mcp.init();
    out('PASS', 'chrome-devtools-mcp start', `${tools.length} tools`);
    const pages = await mcp.call('list_pages', {}, 60000);
    out('PASS', 'chrome-devtools-mcp list_pages', short(pages, 160));
  } catch (e) { out('FAIL', 'chrome-devtools-mcp', `${e.message}; stderr: ${short(mcp.stderr)}`); }
  finally { mcp.close(); }
}

const [mode, ...rest] = process.argv.slice(2);
if (mode === 'c4c') await claudeInChrome(rest[0] || 'container');
else if (mode === 'cdp') { const ip = await cdp(rest[0], rest[1]); if (ip && process.env.DEVTOOLS_MCP_CMD) await devtoolsMcp(`http://${ip}:${rest[1]}`); }
process.exit(0);
EOF

section "environment"
say "date: $(date -u +%FT%TZ)"
say "host: $(uname -sm) $( [ "$(uname -s)" = Darwin ] && sw_vers -productVersion)"
say "docker: $(docker version --format '{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}} ({{.Server.Platform.Name}})' 2>&1)"
if [ -z "${CHROME_BIN:-}" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" google-chrome google-chrome-stable chromium; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then CHROME_BIN=$c; break; fi
  done
fi
say "chrome: ${CHROME_BIN:-not found} $( [ -n "${CHROME_BIN:-}" ] && "$CHROME_BIN" --version 2>/dev/null)"
say "claude on host: $(command -v claude >/dev/null && claude --version 2>/dev/null || echo 'not installed')"
if [ "$(uname -s)" = Darwin ]; then
  m="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.anthropic.claude_code_browser_extension.json"
  say "native host manifest: $([ -f "$m" ] && echo present || echo absent)"
fi

if want c4c; then
  section "1. Claude in Chrome from a container (cloud bridge)"
  if command -v claude >/dev/null && command -v node >/dev/null; then
    (cd "$WORK" && PROBE_NAVIGATE=0 node probe.mjs c4c "host claude (control)")
  else
    say "SKIP host claude (control): needs claude and node on the host"
  fi
  if [ -z "${DEVBOX_CONTAINER:-}" ]; then
    DEVBOX_CONTAINER=$(docker ps --filter label=com.docker.compose.service=devbox --format '{{.Names}}' | head -1)
  fi
  if [ -n "$DEVBOX_CONTAINER" ]; then
    say "INFO devbox container: $DEVBOX_CONTAINER ($(docker exec "$DEVBOX_CONTAINER" claude --version 2>&1))"
    docker exec -i "$DEVBOX_CONTAINER" sh -c 'cat > /tmp/devbox-chrome-probe.mjs' <"$WORK/probe.mjs"
    docker exec -e PROBE_NAVIGATE="$PROBE_NAVIGATE" "$DEVBOX_CONTAINER" node /tmp/devbox-chrome-probe.mjs c4c "devbox container"
    docker exec "$DEVBOX_CONTAINER" rm -f /tmp/devbox-chrome-probe.mjs
  else
    say "SKIP devbox container: none running; start devbox with Compose and log in to Claude Code in it, or set DEVBOX_CONTAINER"
  fi
fi

if want cdp; then
  section "2. Chrome DevTools Protocol to host Chrome"
  if [ -z "${CHROME_BIN:-}" ]; then
    say "SKIP cdp: no host Chrome found; set CHROME_BIN"
  elif curl -fsS "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    say "INFO something already serves CDP on 127.0.0.1:$CDP_PORT; probing it as is"
  else
    # Chrome 136+ ignores --remote-debugging-port on the default profile, so the
    # debugging Chrome always gets its own profile directory.
    "$CHROME_BIN" --user-data-dir="$WORK/chrome-profile" --remote-debugging-port="$CDP_PORT" \
      --no-first-run --no-default-browser-check about:blank >"$WORK/chrome.log" 2>&1 &
    CHROME_PID=$!
    for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1 && break; sleep 1; done
  fi
  if curl -fsS "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    say "PASS host CDP listening on 127.0.0.1:$CDP_PORT"
    say "INFO host listeners: $( (lsof -nP -iTCP:"$CDP_PORT" -sTCP:LISTEN 2>/dev/null || ss -ltn "sport = :$CDP_PORT" 2>/dev/null) | awk 'NR>1{print $9 $4}' | sort -u | tr '\n' ' ')"
    add_host=()
    [ "$(uname -s)" = Linux ] && add_host=(--add-host host.docker.internal:host-gateway)
    docker run --rm "${add_host[@]}" -v "$WORK/probe.mjs:/probe.mjs:ro" \
      -e DEVTOOLS_MCP_CMD="npx -y $DEVTOOLS_MCP" "$NODE_IMAGE" \
      node /probe.mjs cdp host.docker.internal "$CDP_PORT"
  elif [ -n "${CHROME_BIN:-}" ]; then
    say "FAIL host CDP: Chrome did not open port $CDP_PORT; log: $(tail -3 "$WORK/chrome.log" | tr '\n' ' ')"
  fi
fi

if want chromium; then
  section "3. In-container Chromium for linux/arm64"
  docker run --rm --platform linux/arm64 "$CHROMIUM_IMAGE" sh -c '
    apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends chromium ca-certificates fonts-liberation >/dev/null 2>&1 || { echo "FAIL install chromium"; exit 0; }
    echo "PASS install: $(chromium --version) on $(dpkg --print-architecture)"
    if chromium --headless=new --no-sandbox --disable-gpu --dump-dom https://example.com 2>/dev/null | grep -q "Example Domain"
    then echo "PASS headless render example.com"; else echo "FAIL headless render example.com"; fi' >"$WORK/chromium.log" 2>&1
  if grep -qE '^(PASS|FAIL)' "$WORK/chromium.log"; then grep -E '^(PASS|FAIL)' "$WORK/chromium.log"
  else say "SKIP chromium: this host cannot run linux/arm64 containers ($(tail -1 "$WORK/chromium.log"))"; fi
fi

section "done"
say "Paste everything from '== environment ==' down into the spike issue."
