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

Run
---
    python -m registry.server            # binds 0.0.0.0:8080
    AETHER_REGISTRY_PORT=9000 python -m registry.server

Signed deletion
---------------
``DELETE`` requires a JSON body ``{"agent_id", "issued_at", "signature"}`` where
``signature`` is the agent signing ``{"action":"delete","agent_id","issued_at"}``.
This proves the caller owns the private key behind ``agent_id``.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from aether import crypto
from aether.manifest import CapabilityManifest
from aether.registry_store import ManifestStore

# A module-level store so every request handler shares the same state.
_DEFAULT_DB = os.environ.get("AETHER_REGISTRY_DB", "registry_data.json")
STORE = ManifestStore(persist_path=_DEFAULT_DB)


class RegistryHandler(BaseHTTPRequestHandler):
    server_version = "AETHERRegistry/1.0"

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
            return self._json(200, STORE.stats())

        if path == "/discover":
            qs = parse_qs(parsed.query)
            task_type = (qs.get("task_type") or [None])[0]
            if not task_type:
                return self._json(400, {"error": "task_type is required"})
            max_price = self._opt_float(qs, "max_price")
            min_rep = self._opt_float(qs, "min_reputation")
            matches = STORE.discover(task_type, max_price, min_rep)
            return self._json(200, {"results": [m.to_dict() for m in matches]})

        if path.startswith("/agents/"):
            agent_id = path[len("/agents/"):]
            manifest = STORE.get(agent_id)
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
            stored = STORE.publish(manifest)
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
        removed = STORE.remove(agent_id)
        return self._json(200 if removed else 404, {"removed": removed})

    @staticmethod
    def _opt_float(qs, key):
        val = (qs.get(key) or [None])[0]
        return float(val) if val is not None else None


def serve(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), RegistryHandler)
    return httpd


def main() -> None:
    port = int(os.environ.get("AETHER_REGISTRY_PORT", "8080"))
    httpd = serve(port=port)
    print(f"AETHER registry listening on http://0.0.0.0:{port}  (db={_DEFAULT_DB})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
