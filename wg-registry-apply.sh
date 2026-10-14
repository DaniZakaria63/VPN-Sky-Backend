#!/usr/bin/env bash
# wg-registry-apply: privileged remote step for CI deploys.
#
# Installs the staged service files (pushed by the deploy user to
# /tmp/wg-registry.stage), restarts wg-registry and health-checks it.
# This is the only root command the `deploy` user may run without a password
# (see /etc/sudoers.d/wg-registry-deploy, installed by install.sh).
#
# Server configuration in /etc/wg-registry/config.env is never touched here.
set -euo pipefail

STAGE="/tmp/wg-registry.stage"
APP="/opt/wg-registry/wg-registry.py"
UNIT="/etc/systemd/system/wg-registry.service"
CONFIG="/etc/wg-registry/config.env"

[[ $EUID -eq 0 ]] || { echo "must run as root (sudo -n wg-registry-apply)" >&2; exit 2; }
[[ -s "$STAGE/wg-registry.py" ]] || { echo "missing or empty: $STAGE/wg-registry.py" >&2; exit 2; }

mode="$(stat -c %a "$STAGE")"
if (( 8#$mode & 8#022 )); then
  echo "$STAGE is group/world writable (mode $mode); refusing" >&2
  exit 2
fi

install -m 0755 -o root -g root "$STAGE/wg-registry.py" "$APP"
if [[ -s "$STAGE/wg-registry.service" ]]; then
  install -m 0644 -o root -g root "$STAGE/wg-registry.service" "$UNIT"
  systemctl daemon-reload
fi

systemctl restart wg-registry
if ! systemctl is-active --quiet wg-registry; then
  echo "wg-registry failed to start; recent log:" >&2
  journalctl -u wg-registry -n 30 --no-pager >&2 || true
  exit 1
fi

# The unit is Type=simple: restart returns before the socket is bound, so
# retry the health check briefly instead of racing the startup.
bind="$(awk -F= '$1 == "REGISTRY_BIND" {sub(/^[^=]*=/, ""); print; exit}' "$CONFIG" 2>/dev/null || true)"
url="http://${bind:-127.0.0.1:8080}/health"
for _ in $(seq 1 20); do
  if body="$(curl -fsS --max-time 2 "$url" 2>/dev/null)"; then
    echo "$body"
    echo "wg-registry applied from $STAGE (sha256 $(sha256sum "$APP" | cut -c1-16))"
    exit 0
  fi
  sleep 0.5
done

echo "health check failed: $url" >&2
journalctl -u wg-registry -n 30 --no-pager >&2 || true
exit 1
