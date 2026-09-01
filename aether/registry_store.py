"""
AETHER Discovery Registry — Store
=================================

The registry is what turns AETHER from a peer-to-peer library into a *network*:
agents publish their signed CapabilityManifests, and any other agent can query
for capable, trustworthy counterparties. Value compounds with every new agent
(Metcalfe's law), which is the strategic foundation for a settlement/clearing
layer on top.

``ManifestStore`` is deliberately transport-agnostic — it holds no HTTP logic —
so it can be unit-tested directly and later swapped from JSON-on-disk to a real
database without touching the service layer.

Key invariants
--------------
* A manifest is only stored if its signature verifies (authenticity).
* Only the newest manifest per ``agent_id`` is kept (by ``issued_at``).
* Discovery never returns a manifest whose signature no longer verifies.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

from .manifest import CapabilityManifest


class ManifestStore:
    """Thread-safe store of signed capability manifests, keyed by agent_id."""

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._manifests: Dict[str, CapabilityManifest] = {}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        if persist_path and os.path.exists(persist_path):
            self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for raw in data.get("manifests", []):
            manifest = CapabilityManifest.from_dict(raw)
            if manifest.verify():
                self._manifests[manifest.agent_id] = manifest

    def _save(self) -> None:
        if not self._persist_path:
            return
        tmp = f"{self._persist_path}.tmp"
        payload = {"manifests": [m.to_dict() for m in self._manifests.values()]}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self._persist_path)  # atomic swap

    # -- writes ------------------------------------------------------------

    def publish(self, manifest: CapabilityManifest) -> CapabilityManifest:
        """Store a manifest after verifying its signature.

        Raises ``ValueError`` on an invalid signature. If an existing manifest
        for the same agent is newer, it is kept and returned unchanged.
        """
        if not manifest.verify():
            raise ValueError("manifest signature is invalid")
        with self._lock:
            existing = self._manifests.get(manifest.agent_id)
            if existing and existing.issued_at >= manifest.issued_at:
                return existing  # newer/equal manifest already on file
            self._manifests[manifest.agent_id] = manifest
            self._save()
            return manifest

    def remove(self, agent_id: str) -> bool:
        """Delete an agent's manifest. Returns True if something was removed."""
        with self._lock:
            existed = self._manifests.pop(agent_id, None) is not None
            if existed:
                self._save()
            return existed

    # -- reads -------------------------------------------------------------

    def get(self, agent_id: str) -> Optional[CapabilityManifest]:
        with self._lock:
            return self._manifests.get(agent_id)

    def discover(
        self,
        task_type: str,
        max_price: Optional[float] = None,
        min_reputation: Optional[float] = None,
    ) -> List[CapabilityManifest]:
        """Return signature-valid manifests matching the filters.

        Sorted by reputation (desc), then by advertised price (asc) so the
        most trustworthy, cheapest providers surface first.
        """
        with self._lock:
            candidates = list(self._manifests.values())

        results: List[CapabilityManifest] = []
        for m in candidates:
            if not m.handles(task_type):
                continue
            if not m.verify():
                continue
            if min_reputation is not None and m.reputation < min_reputation:
                continue
            schedule = m.price_for(task_type)
            price = schedule.amount if schedule else 0.0
            if max_price is not None and price > max_price:
                continue
            results.append(m)

        def sort_key(m: CapabilityManifest):
            schedule = m.price_for(task_type)
            price = schedule.amount if schedule else 0.0
            return (-m.reputation, price)

        results.sort(key=sort_key)
        return results

    def stats(self) -> Dict[str, Any]:
        """Network-effects dashboard data: size and breadth of the registry."""
        with self._lock:
            manifests = list(self._manifests.values())
        task_counts: Dict[str, int] = {}
        for m in manifests:
            for t in m.task_types:
                task_counts[t] = task_counts.get(t, 0) + 1
        avg_rep = (
            round(sum(m.reputation for m in manifests) / len(manifests), 4)
            if manifests
            else 0.0
        )
        return {
            "total_agents": len(manifests),
            "task_types": dict(sorted(task_counts.items(), key=lambda kv: -kv[1])),
            "distinct_task_types": len(task_counts),
            "avg_reputation": avg_rep,
        }
