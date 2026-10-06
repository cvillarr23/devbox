#!/usr/bin/env bash
# Requires Node >=22.12, npm and git. Run as your normal user.
set -euo pipefail
revision=f05e8b07d2d71b7613e84fde8ff96522aa37f5d2
dest="$(cd "$(dirname "$0")" && pwd)/web/vendor/driftty"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
git clone --quiet https://github.com/mdp/driftty.git "$build_dir/src"
cd "$build_dir/src"
git checkout --quiet "$revision"
# Allow the dashboard to select the existing ttyd route without a proxy or
# another terminal process. The value is supplied only by our session registry.
python3 - <<'PY'
from pathlib import Path
p = Path('src/components/app.tsx')
s = p.read_text()
old = "const path = window.location.pathname.replace(/[/]+$/, '');"
new = "const path = document.querySelector<HTMLMetaElement>('meta[name=devbox-terminal-base]')?.content || window.location.pathname.replace(/[/]+$/, '');"
assert s.count(old) == 1, 'Upstream endpoint selection changed; review adapter'
p.write_text(s.replace(old, new))
PY
npm ci --no-audit --no-fund
npm exec vitest run src
npm run build
mkdir -p "$dest"
cp dist/index.html LICENSE "$dest/"
printf '%s\n' "$revision" > "$dest/REVISION"
