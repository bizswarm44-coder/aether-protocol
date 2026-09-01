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

Federated discovery
-------------------
Pass several registry URLs and ``discover()`` queries them all, then merges and
deduplicates by ``agent_id`` (keeping the newest signed ``issued_at``), so a
client is never dependent on any single registry being reachable::

    reg = RegistryClient(["https://a.example/api", "https://b.example/api"])
    providers = reg.discover("summarize")     # union across A and B

Single-URL usage is unchanged: writes and single-registry discovery behave
exactly as in v0.1.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Union

from . import crypto
from .manifest import CapabilityManifest


class RegistryError(RuntimeError):
    """Raised when the registry returns a non-2xx response."""


class RegistryClient:
    """HTTP client for an AETHER Discovery Registry (optionally federated)."""

    def __init__(
        self,
        base_url: Union[str, List[str]],
        timeout: float = 10.0,
    ) -> None:
        # Accept a single URL (v0.1) or a list of URLs (federated discovery).
        if isinstance(base_url, str):
            urls = [base_url]
        else:
            urls = list(base_url)
        if not urls:
            raise ValueError("at least one registry URL is required")
        self._urls: List[str] = [u.rstrip("/") for u in urls]
        # The primary URL backs all single-target operations (publish, remove,
        # get_agent, stats), preserving exact v0.1 behavior.
        self.base_url = self._urls[0]
        self.timeout = timeout

    # -- internal ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        base: Optional[str] = None,
    ) -> Any:
        url = f"{base or self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        # A named User-Agent identifies AETHER traffic and, importantly, avoids
        # the default "Python-urllib/x.y" signature that many CDNs/WAFs block
        # outright — so this client works against CDN-fronted registries too.
        req.add_header("User-Agent", "aether-registry-client/1.0")
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
        """Find providers for a task type, best (reputation/price) first.

        With a single configured registry this behaves exactly as in v0.1: the
        server's ordered results are returned as-is. With several registries the
        results are queried across all of them, then merged and deduplicated by
        ``agent_id`` (keeping the newest signed ``issued_at``) and re-sorted by
        reputation (desc) then advertised price (asc).
        """
        params: Dict[str, Any] = {"task_type": task_type}
        if max_price is not None:
            params["max_price"] = max_price
        if min_reputation is not None:
            params["min_reputation"] = min_reputation
        query = urllib.parse.urlencode(params)
        path = f"/discover?{query}"

        # Single-URL fast path: byte-for-byte identical to v0.1.
        if len(self._urls) == 1:
            result = self._request("GET", path)
            return [CapabilityManifest.from_dict(m) for m in result["results"]]

        # Federated: union across all registries, tolerating unreachable ones.
        best: Dict[str, CapabilityManifest] = {}
        for base in self._urls:
            try:
                result = self._request("GET", path, base=base)
            except (RegistryError, urllib.error.URLError, OSError):
                continue  # a down registry must not break discovery
            for raw in result.get("results", []):
                m = CapabilityManifest.from_dict(raw)
                current = best.get(m.agent_id)
                if current is None or m.issued_at > current.issued_at:
                    best[m.agent_id] = m

        def sort_key(m: CapabilityManifest):
            schedule = m.price_for(task_type)
            price = schedule.amount if schedule else 0.0
            return (-m.reputation, price)

        return sorted(best.values(), key=sort_key)

    def peer_manifests(self, since: float = 0.0) -> List[CapabilityManifest]:
        """Pull manifests updated since ``ts`` from the primary registry.

        Used by the federation sync loop. Signatures are NOT verified here — the
        caller (a registry ingesting from an untrusted peer) must verify every
        manifest before storing it.
        """
        query = urllib.parse.urlencode({"since": since})
        result = self._request("GET", f"/peer/manifests?{query}")
        return [CapabilityManifest.from_dict(m) for m in result.get("manifests", [])]

    def peer_info(self) -> Dict[str, Any]:
        """Return the primary registry's id and configured peer list."""
        return self._request("GET", "/peer/info")

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
