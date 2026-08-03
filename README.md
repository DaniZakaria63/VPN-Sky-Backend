# wg-registry

A tiny WireGuard peer auto-provisioning service. The app (`VPNSky`) generates a
per-install Curve25519 key, registers its public key here over an authenticated
HTTP call, gets a free tunnel address, and builds its own config. The server
adds the peer with `wg set` automatically — no hand-edited `wg0.conf` per client.

## Files

| file | role |
|------|------|
| `wg-registry.py` | HTTP service (Python >=3.8 stdlib, no deps) |
| `wg-registry.service` | systemd unit (runs as unprivileged `wg-registry` user) |
| `wg-registry.sudoers` | `sudoers.d` rule: lets `wg-registry` run only `wg set/show wg0 …` |
| `deploy.sh` | one-shot deploy to a VPS — credentials entered at runtime, never hardcoded |

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
  The deploy SSH user enters its sudo password during installation only;
  registration never prompts for a password.

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

## Manual smoke (no app)

```
curl -X POST http://<host>:8080/register \
  -H "X-Registry-Token: <token>" \
  -H 'Content-Type: application/json' \
  -d '{"public_key":"$(echo $(cat key.priv) | wg pubkey)"}'
```

Returns the `[Interface]/[Peer]`-buildable JSON for that pubkey.
