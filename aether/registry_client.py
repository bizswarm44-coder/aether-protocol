"""
AETHER Registry Client
======================

A thin, standard-library-only client for talking to an AETHER Discovery
Registry. Kept dependency-free (urllib) so adding registry support never drags
extra packages into an agent's runtime.

Example
-------
    from aether import crypto, CapabilityManifest, RegistryClient

    priv, pub = crypto.generate_keypair()
    manifest = CapabilityManifest(pub, "My Agent", ["summarize"]).sign(priv)

    reg = RegistryClient("http://localhost:8080")
    reg.publish(manifest)
    providers = reg.discover("summarize", max_price=1.0, min_reputation=0.5)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import crypto
from .manifest import CapabilityManifest


class RegistryError(RuntimeError):
    """Raised when the registry returns a non-2xx response."""


class RegistryClient:
    """HTTP client for an AETHER Discovery Registry."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- internal ----------------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RegistryError(f"{exc.code} {exc.reason}: {detail}") from exc

    # -- API ---------------------------------------------------------------

    def publish(self, manifest: CapabilityManifest) -> CapabilityManifest:
        """Publish a signed manifest; returns the stored manifest."""
        result = self._request("POST", "/publish", manifest.to_dict())
        return CapabilityManifest.from_dict(result["manifest"])

    def discover(
        self,
        task_type: str,
        max_price: Optional[float] = None,
        min_reputation: Optional[float] = None,
    ) -> List[CapabilityManifest]:
        """Find providers for a task type, best (reputation/price) first."""
        params: Dict[str, Any] = {"task_type": task_type}
        if max_price is not None:
            params["max_price"] = max_price
        if min_reputation is not None:
            params["min_reputation"] = min_reputation
        query = urllib.parse.urlencode(params)
        result = self._request("GET", f"/discover?{query}")
        return [CapabilityManifest.from_dict(m) for m in result["results"]]

    def get_agent(self, agent_id: str) -> Optional[CapabilityManifest]:
        """Fetch a single agent's manifest, or None if not registered."""
        try:
            result = self._request("GET", f"/agents/{agent_id}")
        except RegistryError as exc:
            if "404" in str(exc):
                return None
            raise
        return CapabilityManifest.from_dict(result)

    def remove(self, agent_id: str, private_hex: str) -> bool:
        """Remove your own manifest by proving ownership with a signature."""
        issued_at = time.time()
        payload = {"action": "delete", "agent_id": agent_id, "issued_at": issued_at}
        signature = crypto.sign(private_hex, payload)
        result = self._request(
            "DELETE",
            f"/agents/{agent_id}",
            {"issued_at": issued_at, "signature": signature},
        )
        return bool(result.get("removed"))

    def stats(self) -> Dict[str, Any]:
        """Return registry network statistics."""
        return self._request("GET", "/stats")
