# wg-registry

A tiny WireGuard peer auto-provisioning service. The app (`VPNSky`) generates a
per-install Curve25519 key, registers its public key here over an authenticated
HTTP call, gets a free tunnel address, and builds its own config. The server
adds the peer with `wg set` automatically — no hand-edited `wg0.conf` per client.

> Backend for the [`VPNSky`](https://github.com/DaniZakaria63/VPN-Sky) Android
> client. This project was extracted from that repo's `server/` directory, with
> commit history preserved. `deploy.sh` is the only entry point needed to stand
> the service up.

## Files

| file | role |
|------|------|
| `wg-registry.py` | HTTP service (Python >=3.8 stdlib, no deps) |
| `wg-registry.service` | systemd unit (runs as unprivileged `wg-registry` user) |
| `wg-registry.sudoers` | `sudoers.d` rule: lets `wg-registry` run only `wg set/show wg0 …` |
| `wg-registry-apply.sh` | restricted root helper CI runs to install staged files + restart the service |
| `deploy.sh` | one-shot deploy to a VPS — credentials entered at runtime, never hardcoded |
| `.github/workflows/deploy.yml` | CI/CD: deploys to the server on every push to `master` |

## Remote Config keys (Firebase)

| key | value |
|-----|-------|
| `base_api_url` | `https://your.server` (the registry endpoint host, no `/`) |
| `registry_token` | shared secret (matches `REGISTRY_TOKEN` on the server) |
| `vpn_dns` | `1.1.1.1` |
| `vpn_allowed_ips` | `0.0.0.0/0, ::/0` |
| `vpn_server_public_key` | copied from `wg show wg0 public-key` |
| `vpn_endpoint` | `203.0.113.1:15221` |
| `vpn_persistent_keepalive` | `25` |

## How a peer joins (client side)

1. `ensureClientKey()` (native) → loads/generates persisted private key, returns pubkey.
2. `registerClient(pubKey)` (JS) → `POST <base_api_url>/register` with `X-Registry-Token`.
3. Server assigns the lowest free `10.0.0.x/32`, runs `wg set wg0 peer <pub> allowed-ips <ip>/32`, returns `{address, server_public_key, server_endpoint, dns, allowed_ips, persistent_keepalive}`.
4. App builds a standard `.conf` and connects — no per-client server config.

Re-registering with the same pubkey reuses the same IP (idempotent). When the
pool is full, the registry recycles the address owned by its least-recently
active client (latest WireGuard handshake or registration time), removes that
old peer, and gives the address to the new key. Manually configured peers are
never recycled. If an evicted device returns, it registers again and receives
the next free/recycled address.

## Security model

- The server public key, server endpoint, DNS, AllowedIPs, and the registry
  token are **not secrets** (the server key is public by design; the token only
  gates random internet noise). They are safe in public Remote Config.
- The per-client **private key is generated and stored only on the device** — it
  never enters the repo, the remote config, or the registry.
- On the server, the registry runs unprivileged and is constrained by a
  narrow passwordless sudoers entry to `wg set/show` on the one interface.
  Its code in `/opt/wg-registry` is root-owned, so the service user cannot
  rewrite its own executable. The deploy SSH user enters its sudo password
  during installation only; registration never prompts for a password.
- CI deploys run as a dedicated `deploy` user whose only passwordless root
  command is `wg-registry-apply` — it installs staged service files and
  restarts the unit; it cannot edit the server config or run anything else.

## Server prerequisites (WireGuard)

The registry manages peers on an **existing** WireGuard interface; it does not
create the tunnel. On a fresh VPS (Ubuntu shown):

```bash
sudo apt-get update && sudo apt-get install -y wireguard-tools
```

Create `/etc/wireguard/wg0.conf` (server takes the first pool address `.1`):

```ini
[Interface]
Address = 10.0.0.1/24
ListenPort = 15221
PrivateKey = <output of: wg genkey>
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o <wan-if> -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o <wan-if> -j MASQUERADE
```

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wireguard.conf
sudo sysctl -p /etc/sysctl.d/99-wireguard.conf
sudo systemctl enable --now wg-quick@wg0
```

Open **UDP** `WG_PORT` (default 15221) in any provider/cloud firewall — clients
connect to the endpoint directly; HTTP tunnels/proxies do not carry WireGuard
traffic. A Cloudflare-proxied hostname cannot be the endpoint.

## Deploy (runtime credentials only)

```
./deploy.sh
```

Prompts for: host, and the registry token. Optional env/args:

| env var | default | meaning |
|---------|---------|---------|
| `WG_HOST` | — | server public IP/hostname (required) |
| `WG_SSH_USER` | root | login user |
| `WG_SSH_PORT` | 22 | ssh port |
| `WG_INTERFACE` | wg0 | wg interface name |
| `WG_PORT` | 15221 | WG listen port (used for endpoint hint) |
| `WG_SUBNET` | 10.0.0.0/24 | client address pool |
| `WG_CLIENT_RESERVED` | 1 | first N pool IPs to keep free (usually `.1` = server) |
| `WG_TOKEN` | prompt | X-Registry-Token (config readable only by root/service group) |
| `WG_SERVER_PUBLIC_KEY` | auto-discovered | server pubkey returned to clients |
| `WG_ENDPOINT` | `host:wg_port` | what clients connect to |

After deploy:

```
systemctl status wg-registry
curl -H "X-Registry-Token: <token>" http://127.0.0.1:8080/health   # -> {"ok": true}
```

> The token is stored **only** in `/etc/wg-registry/config.env` on the server
> (`root:wg-registry`, mode `0640`).

## CI/CD (GitHub Actions)

`.github/workflows/deploy.yml` deploys on every push to `master` (and manually
via Actions → Deploy → Run workflow). It stages `wg-registry.py` and
`wg-registry.service` to `/tmp/wg-registry.stage` over SSH as the `deploy`
user, then runs the restricted root helper, which installs the files, restarts
the service, and health-checks it:

```
sudo -n /usr/local/sbin/wg-registry-apply
```

Server config (`/etc/wg-registry/config.env`) is never touched by CI — it stays
server-side, exactly as `deploy.sh` wrote it. `install.sh` installs the helper
and, when a `deploy` user exists, this narrow sudoers rule:

```
/etc/sudoers.d/wg-registry-deploy:
deploy ALL=(root) NOPASSWD: /usr/local/sbin/wg-registry-apply
```

### Repository secrets

| secret | value |
|--------|-------|
| `DEPLOY_HOST` | server IP or hostname |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | private key whose public half is in `/home/deploy/.ssh/authorized_keys` |
| `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -p <port> <host>` |
| `DEPLOY_PORT` | optional, defaults to 22 |

```
gh secret set DEPLOY_HOST --body '<server-ip>'
gh secret set DEPLOY_USER --body 'deploy'
gh secret set DEPLOY_PORT --body '22'           # skip if using port 22
gh secret set DEPLOY_SSH_KEY < deploy_key
gh secret set DEPLOY_KNOWN_HOSTS < known_hosts
```

Rollback: revert the bad commit on `master` and push (deploys always apply
master). Rotate the SSH key by replacing the `authorized_keys` line and
updating `DEPLOY_SSH_KEY`. If the host key changes, refresh
`DEPLOY_KNOWN_HOSTS` with a new `ssh-keyscan`.

## Manual smoke (no app)

```
curl -X POST http://<host>:8080/register \
  -H "X-Registry-Token: <token>" \
  -H 'Content-Type: application/json' \
  -d '{"public_key":"$(echo $(cat key.priv) | wg pubkey)"}'
```

Returns the `[Interface]/[Peer]`-buildable JSON for that pubkey.
