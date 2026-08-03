#!/usr/bin/env bash
# Deploy wg-registry to a WireGuard server.
# Credentials are NEVER baked into git: host/user/port/token/subnet/etc. come
# from args or prompts and are written server-side to /etc/wg-registry/config.env
# (mode 0600). Re-running with the same options is safe (idempotent).
#
# Usage:
#   ./deploy.sh                                  # prompts for host + token
#   ./deploy.sh --host 203.0.113.1 --port 22     # or set WG_* env vars
set -euo pipefail

HOST="${WG_HOST:-}"
SSH_USER="${WG_SSH_USER:-root}"
SSH_PORT="${WG_SSH_PORT:-22}"
INTERFACE="${WG_INTERFACE:-wg0}"
WG_PORT="${WG_PORT:-15221}"
SUBNET="${WG_SUBNET:-10.0.0.0/24}"
CLIENT_RESERVED="${WG_CLIENT_RESERVED:-1}"
DNS="${WG_DNS:-1.1.1.1}"
ALLOWED_IPS="${WG_ALLOWED_IPS:-0.0.0.0/0, ::/0}"
PERSISTENT_KEEPALIVE="${WG_PERSISTENT_KEEPALIVE:-25}"
BIND="${WG_REGISTRY_BIND:-127.0.0.1:8080}"
STATE_FILE="${WG_STATE_FILE:-/var/lib/wg-registry/clients.json}"
SERVER_PUB_KEY="${WG_SERVER_PUBLIC_KEY:-}"
TOK="${WG_TOKEN:-}"

usage() { sed -n '1,15p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --host) HOST="${2:?--host value}"; shift 2 ;;
    --user) SSH_USER="${2:?--user value}"; shift 2 ;;
    --port) SSH_PORT="${2:?--port value}"; shift 2 ;;
    --iface) INTERFACE="${2:?--iface value}"; shift 2 ;;
    --wg-port) WG_PORT="${2:?--wg-port value}"; shift 2 ;;
    --subnet) SUBNET="${2:?--subnet value}"; shift 2 ;;
    --token) TOK="${2:?--token value}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

require() { [[ -n "${2:-}" ]] || { echo "ERROR: $1" >&2; exit 2; }; }
require "WG_HOST" "$HOST"
ENDPOINT="${WG_ENDPOINT:-${HOST}:${WG_PORT}}"

if [[ -z "$TOK" ]]; then
  read -r -p "Registry token (X-Registry-Token): " TOK
  require "token" "$TOK"
fi
if [[ -z "$SERVER_PUB_KEY" ]]; then
  echo "Discovering server public key via SSH (enter the VPS sudo password when prompted)..."
  # Keep stderr attached so sudo's prompt stays visible. Timeout avoids an
  # indefinite wait when SSH or sudo cannot become interactive.
  SERVER_PUB_KEY="$(timeout 60 ssh -tt -p "$SSH_PORT" "${SSH_USER}@${HOST}" \
    "sudo -p '[sudo] VPS password for %u: ' wg show ${INTERFACE} public-key" \
    | tr -d '\r' | grep -E '^[A-Za-z0-9+/]{43}=$' | tail -1)" || true
  require "WG_SERVER_PUBLIC_KEY" "$SERVER_PUB_KEY"
fi

REMOTE_DIR="/opt/wg-registry"
CONFIG_FILE="/etc/wg-registry/config.env"
TAG="wg-registry.deploy.$$"

echo "==> Deploying to ${SSH_USER}@${HOST}:${SSH_PORT}"
echo "    interface=${INTERFACE} wg-port=${WG_PORT} subnet=${SUBNET} reserved-first=${CLIENT_RESERVED}"
echo "    server-pubkey=${SERVER_PUB_KEY:0:8}...  endpoint=${ENDPOINT}"

deploy() {
  ssh -p "$SSH_PORT" "${SSH_USER}@${HOST}" "rm -rf /tmp/${TAG} && mkdir -m 700 /tmp/${TAG}"

  scp -P "$SSH_PORT" server/wg-registry.py \
                           server/wg-registry.service \
                           server/install.sh \
      "${SSH_USER}@${HOST}:/tmp/${TAG}/" 2>/dev/null

  CONFIG_TMP="$(mktemp)"
  chmod 600 "$CONFIG_TMP"
  trap 'rm -f "$CONFIG_TMP"' RETURN
  cat >"$CONFIG_TMP" <<CFGENV
REGISTRY_TOKEN=${TOK}
REGISTRY_BIND=${BIND}
WIREGUARD_INTERFACE=${INTERFACE}
WIREGUARD_SUBNET=${SUBNET}
SERVER_PUBLIC_KEY=${SERVER_PUB_KEY}
SERVER_ENDPOINT=${ENDPOINT}
DNS=${DNS}
ALLOWED_IPS=${ALLOWED_IPS}
PERSISTENT_KEEPALIVE=${PERSISTENT_KEEPALIVE}
STATE_FILE=${STATE_FILE}
CLIENT_RESERVED=${CLIENT_RESERVED}
CFGENV
  scp -P "$SSH_PORT" "$CONFIG_TMP" "${SSH_USER}@${HOST}:/tmp/${TAG}/config.env" >/dev/null
  rm -f "$CONFIG_TMP"
  trap - RETURN

  # Installer is already on target. stdin is now free exclusively for sudo's
  # password prompt; no heredoc/script bytes can be consumed as a password.
  ssh -tt -p "$SSH_PORT" "${SSH_USER}@${HOST}" \
    "chmod 700 /tmp/${TAG}/install.sh && /tmp/${TAG}/install.sh /tmp/${TAG}"
}

deploy
echo "==> Installed."
ssh -p "$SSH_PORT" "${SSH_USER}@${HOST}" \
  'systemctl is-enabled wg-registry; systemctl status wg-registry --no-pager -l | head -5'
echo "==> Healthcheck on the server:"
echo "    curl -H \"X-Registry-Token: $TOK\" http://127.0.0.1:${BIND#*:}/health"
echo "==> Token lives only in /etc/wg-registry/config.env on the server."
