#!/usr/bin/env bash
# Runs on target host from staged /tmp directory. stdin remains attached to
# terminal, allowing sudo to prompt normally during one installation session.
set -euo pipefail

STAGE="${1:?staging directory required}"
DEPLOY_CONFIG="${STAGE}/config.env"

# Parse only keys needed during installation. Do not source config.env: values
# such as `ALLOWED_IPS=0.0.0.0/0, ::/0` intentionally contain shell spaces.
config_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$DEPLOY_CONFIG"
}
WIREGUARD_INTERFACE="$(config_value WIREGUARD_INTERFACE)"
[[ -n "$WIREGUARD_INTERFACE" ]] || {
  echo "WIREGUARD_INTERFACE missing from deploy config" >&2
  exit 2
}

sudo -v
id -u wg-registry >/dev/null 2>&1 || \
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin wg-registry

sudo mkdir -p /opt/wg-registry /etc/wg-registry /var/lib/wg-registry
sudo install -m 0755 "${STAGE}/wg-registry.py" /opt/wg-registry/wg-registry.py
sudo install -m 0644 "${STAGE}/wg-registry.service" /etc/systemd/system/wg-registry.service
sudo chown -R wg-registry:wg-registry /opt/wg-registry /var/lib/wg-registry

WG_BIN="$(command -v wg)"
sudo tee /etc/sudoers.d/wg-registry >/dev/null <<SUDOERS
wg-registry ALL=(root) NOPASSWD: ${WG_BIN} set ${WIREGUARD_INTERFACE} peer *, ${WG_BIN} show ${WIREGUARD_INTERFACE} dump
SUDOERS
sudo chmod 0440 /etc/sudoers.d/wg-registry
sudo visudo -cf /etc/sudoers.d/wg-registry

sudo install -m 0640 -o root -g wg-registry "$DEPLOY_CONFIG" /etc/wg-registry/config.env
sudo systemctl daemon-reload
sudo systemctl enable --now wg-registry
sudo -u wg-registry sudo -n "${WG_BIN}" show "${WIREGUARD_INTERFACE}" dump >/dev/null

rm -rf "${STAGE}"

echo "wg-registry installed and active"
sudo systemctl status wg-registry --no-pager -l | head -8
