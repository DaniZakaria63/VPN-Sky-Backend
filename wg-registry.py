#!/usr/bin/env python3
"""
wg-registry: auto-provision WireGuard peers on a server.

A client sends its public key over an authenticated POST; the service assigns a
free tunnel address (the lowest unused /32 in the configured network), adds the
peer with `wg set`, and returns the client's full config details so the app can
build its own wg conf. Addresses are reused for an already-known pubkey.

Run as a normal system user; it shells out to `sudo -n wg set ...` through the
restricted sudoers rule installed by deploy.sh. Stdlib only.

Configuration (env). Anything missing at startup => the process refuses to run.
  REGISTRY_TOKEN            - bearer-like shared secret (X-Registry-Token header)
  REGISTRY_BIND             - host:port (default 127.0.0.1:8080)
  WIREGUARD_INTERFACE       - wg interface name (default wg0)
  WIREGUARD_SUBNET          - client pool, e.g. 10.0.0.0/24 (default 10.0.0.0/24)
  SERVER_PUBLIC_KEY         - server tunnel public key returned to clients
  SERVER_ENDPOINT           - "host:port" returned to clients (or empty)
  DNS                       - DNS servers returned to clients (default 1.1.1.1)
  ALLOWED_IPS               - AllowedIPs returned to clients (default 0.0.0.0/0, ::/0)
  PERSISTENT_KEEPALIVE      - seconds (default 25)
  STATE_FILE                - JSON store pubkey -> ip (default /var/lib/wg-registry/clients.json)
  CLIENT_RESERVED           - first N IPs of the pool to keep free (default 1, i.e. .1=server)
"""
import ipaddress
import itertools
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PUBKEY_RE = re.compile(r"[A-Za-z0-9+/]{43}=")
_CONFIG_FILE = "/etc/wg-registry/config.env"


def load_config_file(path: str):
    """Read systemd-style KEY=value lines without shell evaluation.

    Values may contain spaces, commas, URLs, or CIDRs. Environment variables
    supplied explicitly by the process still override file values.
    """
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def required(name: str) -> str:
    v = env(name)
    if not v:
        raise SystemExit(f"missing required env var: {name}")
    return v


class Server:
    def __init__(self):
        self.token = required("REGISTRY_TOKEN")
        bind = env("REGISTRY_BIND", "127.0.0.1:8080")
        self.host, self.port = self._split_bind(bind)
        self.interface = env("WIREGUARD_INTERFACE", "wg0")
        self.server_pubkey = required("SERVER_PUBLIC_KEY")
        self.endpoint = env("SERVER_ENDPOINT")  # may be empty
        self.dns = env("DNS", "1.1.1.1")
        self.allowed_ips = env("ALLOWED_IPS", "0.0.0.0/0, ::/0")
        self.pka = int(env("PERSISTENT_KEEPALIVE", "25") or "25")
        self.state_file = env("STATE_FILE", "/var/lib/wg-registry/clients.json")
        self.reserved = int(env("CLIENT_RESERVED", "1") or "1")
        self.network = ipaddress.ip_network(env("WIREGUARD_SUBNET", "10.0.0.0/24"), strict=False)
        if self.network.version != 4:
            raise SystemExit("WIREGUARD_SUBNET must be IPv4")
        self.lock = threading.Lock()
        self._load_state()

    @staticmethod
    def _split_bind(addr: str):
        if ":" not in addr:
            raise SystemExit(f"invalid REGISTRY_BIND: {addr!r}")
        host, _, port = addr.rpartition(":")
        return host, int(port)

    # -- state persistence (pubkey -> {ip, last_registered}) -- #
    def _load_state(self):
        try:
            with open(self.state_file, "r") as f:
                self.state = json.load(f)
            if not isinstance(self.state, dict):
                self.state = {}
            # Migrate v1 state, where values were bare IP strings.
            self.state = {
                pubkey: ({"ip": entry, "last_registered": 0}
                         if isinstance(entry, str) else entry)
                for pubkey, entry in self.state.items()
                if isinstance(entry, (str, dict))
            }
        except FileNotFoundError:
            self.state = {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, sort_keys=True)
        except OSError as e:
            print(f"[registry] cannot persist state to {self.state_file}: {e}", file=sys.stderr)

    # -- wg interaction -- #
    def wg(self, *args: str) -> str:
        """Run `sudo -n wg <args>`. Never read secrets from env."""
        cmd = ["sudo", "-n", "wg", *args]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout

    def add_peer(self, pubkey: str, ip: str):
        # `wg set peer X allowed-ips Y` is idempotent: on an existing peer it
        # updates AllowedIPs (no error); on a new peer it appends.
        self.wg("set", self.interface, "peer", pubkey, "allowed-ips", f"{ip}/32")

    def remove_peer(self, pubkey: str):
        self.wg("set", self.interface, "peer", pubkey, "remove")

    def pool_hosts(self):
        # islice avoids materializing huge pools (e.g. /8) in memory.
        return itertools.islice(self.network.hosts(), self.reserved, None)

    def assign_ip(self, pubkey: str) -> tuple[str, str | None] | None:
        # Already registered (persisted) -> reuse.
        existing = self.state.get(pubkey)
        if isinstance(existing, dict) and existing.get("ip"):
            return str(existing["ip"]), None

        # Otherwise find the lowest free /32. Pull live `wg show dump` so we never
        # collide with a peer that exists on the box but not in our state file.
        live_own: str | None = None
        used: set[str] = {
            str(entry["ip"])
            for entry in self.state.values()
            if isinstance(entry, dict) and entry.get("ip")
        }
        live_handshakes: dict[str, int] = {}
        try:
            dump = self.wg("show", self.interface, "dump")
        except subprocess.CalledProcessError:
            dump = ""
        for line in dump.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 4 or fields[1] == "(none)":
                continue
            peer_pk = fields[1]
            if len(fields) > 4:
                try:
                    live_handshakes[peer_pk] = int(fields[4])
                except ValueError:
                    live_handshakes[peer_pk] = 0
            for cidr in fields[3].split(","):
                cidr = cidr.strip()
                if not cidr:
                    continue
                # cidr looks like "10.0.0.5/32" — extract the bare ip
                bare = cidr.split("/", 1)[0]
                if peer_pk == pubkey:
                    live_own = bare
                else:
                    used.add(bare)

        if live_own:
            return live_own, None  # peer already exists live; reuse its IP

        for host in self.pool_hosts():
            if str(host) not in used:
                return str(host), None

        # Pool full: recycle the least-recently-active registry-managed peer.
        # Manually configured peers are absent from state and are never evicted.
        candidates = []
        for old_pubkey, entry in self.state.items():
            if old_pubkey == pubkey or not isinstance(entry, dict) or not entry.get("ip"):
                continue
            registered = int(entry.get("last_registered", 0) or 0)
            last_active = max(registered, live_handshakes.get(old_pubkey, 0))
            candidates.append((last_active, old_pubkey, str(entry["ip"])))
        if not candidates:
            return None
        _, victim, recycled_ip = min(candidates)
        return recycled_ip, victim

    def register(self, pubkey: str) -> dict | None:
        with self.lock:
            assignment = self.assign_ip(pubkey)
            if assignment is None:
                return None
            ip, victim = assignment
            # Always reconcile live state. This restores persisted peers after
            # wg0 or the host restarts, while `wg set` remains idempotent.
            if victim:
                self.remove_peer(victim)
            try:
                self.add_peer(pubkey, ip)
            except subprocess.CalledProcessError:
                # Avoid losing the old client if replacing it fails midway.
                if victim:
                    self.add_peer(victim, ip)
                raise
            if victim:
                del self.state[victim]
            self.state[pubkey] = {
                "ip": ip,
                "last_registered": int(time.time()),
            }
            self._save_state()
        return self._client_payload(pubkey, ip)

    def _client_payload(self, pubkey: str, ip: str) -> dict:
        return {
            "address": f"{ip}/32",
            "server_public_key": self.server_pubkey,
            "server_endpoint": self.endpoint,
            "dns": self.dns,
            "allowed_ips": self.allowed_ips,
            "persistent_keepalive": self.pka,
        }


_PUBLIC_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


import sys  # noqa: E402  (kept here so config is read top-down)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wg-registry/1.0"

    def log_message(self, format, *args):  # noqa: A002 - match parent signature
        print(f"[registry] {self.client_address[0]} - {format % args}", flush=True)

    def _authorize(self) -> bool:
        token = self.headers.get("X-Registry-Token", "") or ""
        return bool(token) and token == SRV.token

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorize():
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path != "/register":
            self._reply(404, {"error": "not found"})
            return
        self._register()

    def _register(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid json"})
            return
        pubkey = str(payload.get("public_key") or "").strip()
        if not _PUBKEY_RE.fullmatch(pubkey):
            self._reply(400, {"error": "invalid public_key"})
            return
        try:
            result = SRV.register(pubkey)
        except subprocess.CalledProcessError as exc:
            # sudo -n deliberately fails rather than prompting. Keep service
            # alive and expose an actionable server-side error to the client.
            detail = (exc.stderr or "wg command failed").strip()
            print(f"[registry] peer update failed: {detail}", flush=True)
            self._reply(503, {"error": "WireGuard peer update unavailable"})
            return
        if result is None:
            self._reply(507, {"error": "no free address in pool"})
        else:
            self._reply(200, result)


def main():
    global SRV
    load_config_file(_CONFIG_FILE)
    SRV = Server()
    print(f"[registry] listening on {SRV.host}:{SRV.port} (pool {SRV.network}, "
          f".reserved={SRV.reserved})", flush=True)
    httpd = ThreadingHTTPServer((SRV.host, SRV.port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
