"""
AETHER Discovery Registry — HTTP Service
========================================

A tiny, zero-dependency HTTP service (Python standard library only) that
exposes the ``ManifestStore`` over JSON. It is intentionally small enough to
read in one sitting and to self-host anywhere.

Endpoints
---------
    POST   /publish            — publish a signed CapabilityManifest (verified)
    GET    /discover           — ?task_type=&max_price=&min_reputation=
    GET    /agents/{agent_id}  — fetch one agent's manifest
    DELETE /agents/{agent_id}  — remove your manifest (requires signed proof)
    GET    /stats              — network dashboard data
    GET    /healthz            — liveness probe
    GET    /peer/manifests     — ?since=<unix_ts>  manifests for peers to pull
    GET    /peer/info          — this registry's id + configured peer list

Run
---
    python -m registry.server            # binds 0.0.0.0:8080
    AETHER_REGISTRY_PORT=9000 python -m registry.server

Federation (gossip mirroring, not consensus)
--------------------------------------------
Set ``AETHER_PEERS`` to a comma-separated list of peer registry base URLs and
this registry additionally runs a background loop that periodically pulls each
peer's ``/peer/manifests?since=<ts>``, verifies **every** manifest signature,
rejects implausibly future-dated ones, and upserts with last-writer-wins on the
signed ``issued_at``. Manifests are self-signed, so authenticity never depends
on which registry served them — no trust between peers is required.

    AETHER_PEERS=https://b.example/api,https://c.example/api \
        python -m registry.server

Extra config (all optional, sane defaults):
    AETHER_SYNC_INTERVAL   seconds between sync passes           (default 30)
    AETHER_CLOCK_SKEW      allowed future-dating, seconds        (default 300)
    AETHER_PULL_GAP        seconds spaced between peer pulls      (default 0.5)
    AETHER_REGISTRY_ID     stable id for this registry           (default random)

With no ``AETHER_PEERS`` set, behavior is identical to v0.1 (no sync thread).

Signed deletion
---------------
``DELETE`` requires a JSON body ``{"agent_id", "issued_at", "signature"}`` where
``signature`` is the agent signing ``{"action":"delete","agent_id","issued_at"}``.
This proves the caller owns the private key behind ``agent_id``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from aether import crypto
from aether.manifest import CapabilityManifest
from aether.registry_store import ManifestStore
from aether.registry_client import RegistryClient

# A module-level store so every request handler shares the same state. It is the
# default when ``serve()`` is called without an explicit store; tests and the
# federation examples pass their own store so multiple registries can run in one
# process with independent state.
_DEFAULT_DB = os.environ.get("AETHER_REGISTRY_DB", "registry_data.json")
STORE = ManifestStore(persist_path=_DEFAULT_DB)

# Federation defaults (seconds).
DEFAULT_SYNC_INTERVAL = float(os.environ.get("AETHER_SYNC_INTERVAL", "30"))
DEFAULT_CLOCK_SKEW = float(os.environ.get("AETHER_CLOCK_SKEW", "300"))
DEFAULT_PULL_GAP = float(os.environ.get("AETHER_PULL_GAP", "0.5"))


def _peers_from_env() -> List[str]:
    """Parse ``AETHER_PEERS`` (comma-separated URLs) into a clean list."""
    raw = os.environ.get("AETHER_PEERS", "")
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


class FederationSync:
    """Background pull-sync loop that mirrors peer registries' manifests.

    Gossip, not consensus: this registry independently pulls from each configured
    peer, verifies signatures, and upserts locally with last-writer-wins. There
    is no leader and no global ordering — just eventual consistency, which is all
    discovery needs. Every network call uses the standard-library
    ``RegistryClient`` (urllib), keeping the core dependency-free.
    """

    def __init__(
        self,
        store: ManifestStore,
        peers: List[str],
        clock_skew: float = DEFAULT_CLOCK_SKEW,
        interval: float = DEFAULT_SYNC_INTERVAL,
        pull_gap: float = DEFAULT_PULL_GAP,
        timeout: float = 10.0,
    ) -> None:
        self.store = store
        self.peers = list(peers)
        self.clock_skew = clock_skew
        self.interval = interval
        self.pull_gap = pull_gap
        self.timeout = timeout
        # Per-peer cursor: the wall-clock of our last successful pull. We pull
        # ``since = cursor - clock_skew`` so manifests that arrived slightly out
        # of order are not missed (the store dedups, so overlap is harmless).
        self._cursor = {p: 0.0 for p in self.peers}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- one pass ----------------------------------------------------------

    def sync_once(self) -> int:
        """Pull from every peer once. Returns the number of manifests accepted.

        Peer pulls are spaced out by ``pull_gap`` to rate-limit outbound load.
        Safe to call manually (used by tests for a deterministic sync).
        """
        accepted = 0
        for i, peer in enumerate(self.peers):
            if i:
                time.sleep(self.pull_gap)  # space out peer pulls
            accepted += self._pull_from(peer)
        return accepted

    def _pull_from(self, peer: str) -> int:
        since = max(0.0, self._cursor.get(peer, 0.0) - self.clock_skew)
        client = RegistryClient(peer, timeout=self.timeout)
        try:
            manifests = client.peer_manifests(since=since)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return 0  # unreachable/garbled peer: skip this round, retry later
        now = time.time()
        accepted = 0
        for m in manifests:
            # Never trust the peer: verify the manifest's own signature.
            if not m.verify():
                continue
            # Reject implausibly future-dated manifests (clock-skew guard) so a
            # peer cannot pin an agent's identity with a far-future issued_at.
            if m.issued_at > now + self.clock_skew:
                continue
            try:
                stored = self.store.publish(m, origin=peer)
            except ValueError:
                continue  # invalid signature (defensive; already checked)
            if stored is m:
                accepted += 1  # a genuine new/newer manifest was stored
        self._cursor[peer] = now
        return accepted

    # -- thread lifecycle --------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="aether-federation-sync", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        # Wait first, then sync — so request serving is up before the first pull.
        while not self._stop.wait(self.interval):
            try:
                self.sync_once()
            except Exception:
                # A sync loop must never die on a transient error.
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


class RegistryHandler(BaseHTTPRequestHandler):
    server_version = "AETHERRegistry/1.0"

    # The store/config live on the server instance (set in ``serve()``), so
    # several registries can run in one process with independent state.
    @property
    def store(self) -> ManifestStore:
        return self.server.store  # type: ignore[attr-defined]

    # -- helpers -----------------------------------------------------------

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):  # quieter default logging
        return

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/healthz":
            return self._json(200, {"status": "ok"})

        if path == "/stats":
            return self._json(200, self.store.stats())

        if path == "/peer/info":
            return self._json(200, {
                "registry_id": self.server.registry_id,  # type: ignore[attr-defined]
                "peers": list(self.server.peers),         # type: ignore[attr-defined]
            })

        if path == "/peer/manifests":
            qs = parse_qs(parsed.query)
            since = self._opt_float(qs, "since") or 0.0
            manifests = self.store.updated_since(since)
            return self._json(200, {"manifests": [m.to_dict() for m in manifests]})

        if path == "/discover":
            qs = parse_qs(parsed.query)
            task_type = (qs.get("task_type") or [None])[0]
            if not task_type:
                return self._json(400, {"error": "task_type is required"})
            max_price = self._opt_float(qs, "max_price")
            min_rep = self._opt_float(qs, "min_reputation")
            matches = self.store.discover(task_type, max_price, min_rep)
            return self._json(200, {"results": [m.to_dict() for m in matches]})

        if path.startswith("/agents/"):
            agent_id = path[len("/agents/"):]
            manifest = self.store.get(agent_id)
            if not manifest:
                return self._json(404, {"error": "agent not found"})
            return self._json(200, manifest.to_dict())

        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/publish":
            return self._json(404, {"error": "not found"})
        try:
            data = self._read_body()
            manifest = CapabilityManifest.from_dict(data)
        except (ValueError, KeyError) as exc:
            return self._json(400, {"error": f"malformed manifest: {exc}"})
        try:
            stored = self.store.publish(manifest)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"published": True, "manifest": stored.to_dict()})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path.startswith("/agents/"):
            return self._json(404, {"error": "not found"})
        agent_id = path[len("/agents/"):]
        try:
            body = self._read_body()
        except ValueError:
            body = {}
        # Verify the caller owns agent_id by checking a signed delete intent.
        payload = {
            "action": "delete",
            "agent_id": agent_id,
            "issued_at": body.get("issued_at"),
        }
        signature = body.get("signature", "")
        if not signature or not crypto.verify(agent_id, payload, signature):
            return self._json(403, {"error": "invalid or missing signature"})
        removed = self.store.remove(agent_id)
        return self._json(200 if removed else 404, {"removed": removed})

    @staticmethod
    def _opt_float(qs, key):
        val = (qs.get(key) or [None])[0]
        return float(val) if val is not None else None


def serve(
    host: str = "0.0.0.0",
    port: int = 8080,
    store: Optional[ManifestStore] = None,
    registry_id: Optional[str] = None,
    peers: Optional[List[str]] = None,
    clock_skew: float = DEFAULT_CLOCK_SKEW,
    start_sync: bool = False,
    sync_interval: float = DEFAULT_SYNC_INTERVAL,
    pull_gap: float = DEFAULT_PULL_GAP,
) -> ThreadingHTTPServer:
    """Build a registry HTTP server.

    ``store`` defaults to the module-level ``STORE`` (v0.1 behavior). ``peers``
    are exposed via ``/peer/info``; when ``start_sync`` is True and peers are
    configured, a background :class:`FederationSync` is created, attached as
    ``httpd.sync``, and started. Tests pass ``start_sync=False`` and drive
    ``httpd.sync.sync_once()`` manually for determinism.
    """
    httpd = ThreadingHTTPServer((host, port), RegistryHandler)
    httpd.store = store if store is not None else STORE           # type: ignore[attr-defined]
    httpd.registry_id = (                                          # type: ignore[attr-defined]
        registry_id or os.environ.get("AETHER_REGISTRY_ID") or uuid.uuid4().hex
    )
    httpd.peers = list(peers) if peers is not None else []        # type: ignore[attr-defined]
    httpd.sync = None                                             # type: ignore[attr-defined]
    if httpd.peers:                                               # type: ignore[attr-defined]
        sync = FederationSync(
            httpd.store,                                          # type: ignore[attr-defined]
            httpd.peers,                                          # type: ignore[attr-defined]
            clock_skew=clock_skew,
            interval=sync_interval,
            pull_gap=pull_gap,
        )
        httpd.sync = sync                                         # type: ignore[attr-defined]
        if start_sync:
            sync.start()
    return httpd


def main() -> None:
    port = int(os.environ.get("AETHER_REGISTRY_PORT", "8080"))
    peers = _peers_from_env()
    httpd = serve(port=port, peers=peers, start_sync=bool(peers))
    peer_note = f"  peers={peers}" if peers else "  (single registry)"
    print(f"AETHER registry listening on http://0.0.0.0:{port}  (db={_DEFAULT_DB}){peer_note}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        if getattr(httpd, "sync", None):
            httpd.sync.stop()  # type: ignore[attr-defined]
        httpd.shutdown()


if __name__ == "__main__":
    main()
