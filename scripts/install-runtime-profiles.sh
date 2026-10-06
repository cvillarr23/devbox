#!/usr/bin/env bash
# Install devbox-only profiles; no global sysctls or existing profiles change.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run with sudo to install the devbox runtime profiles.' >&2
  exit 1
fi
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v apparmor_parser >/dev/null 2>&1; then
  profile_file="$(mktemp)"
  trap 'rm -f "$profile_file"' EXIT
  parser_major="$(apparmor_parser --version | awk 'NR==1 {split($NF, version, "."); print version[1]}')"
  if [[ "$parser_major" -ge 4 ]]; then
    sed 's/# DEVBOX_USERNS_RULE/userns,/' "$project_dir/config/apparmor-devbox" > "$profile_file"
  else
    cp "$project_dir/config/apparmor-devbox" "$profile_file"
  fi
  apparmor_parser -Q -K "$profile_file"
  install -D -m644 "$profile_file" /etc/apparmor.d/devbox-workspace
  apparmor_parser -r /etc/apparmor.d/devbox-workspace
else
  echo 'AppArmor is unavailable; use the unconfined AppArmor overlay described in the README.' >&2
fi
profile_root="${DEVBOX_KUBELET_SECCOMP_ROOT:-/var/lib/kubelet/seccomp}"
install -D -m644 "$project_dir/config/seccomp-kubernetes.json" "$profile_root/devbox.json"
echo 'Installed devbox profiles. Containers remain unprivileged and agent sandbox policies remain enabled.'
