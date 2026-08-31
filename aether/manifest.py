"""
AETHER Capability Manifest
==========================

A Capability Manifest is an agent's signed, self-describing advertisement:
who it is, what task types it can perform, how it prices them, its current
reputation, and when the manifest was issued. Manifests are the unit of
discovery — agents publish them and counterparties verify them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

from . import crypto

PROTOCOL_VERSION = "1.0"


@dataclass
class PriceSchedule:
    """Pricing for a single task type.

    ``unit`` is a free-form string (e.g. "per_task", "per_1k_tokens") so the
    protocol stays model-agnostic; ``amount`` and ``currency`` describe cost.
    """

    task_type: str
    amount: float
    currency: str = "USD"
    unit: str = "per_task"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityManifest:
    """A signed advertisement of an agent's identity and services."""

    agent_id: str                       # public key hex — the agent's identity
    display_name: str
    task_types: List[str]
    pricing: List[PriceSchedule] = field(default_factory=list)
    reputation: float = 0.0             # 0.0–1.0 rolling reputation score
    version: str = PROTOCOL_VERSION
    issued_at: float = field(default_factory=lambda: time.time())
    signature: str = ""                 # populated by sign()

    # -- serialization -----------------------------------------------------

    def _signable(self) -> Dict[str, Any]:
        """The canonical payload that gets signed (everything but the sig)."""
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "task_types": sorted(self.task_types),
            "pricing": [p.to_dict() for p in self.pricing],
            "reputation": round(self.reputation, 6),
            "version": self.version,
            "issued_at": round(self.issued_at, 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._signable()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CapabilityManifest":
        return cls(
            agent_id=data["agent_id"],
            display_name=data["display_name"],
            task_types=list(data["task_types"]),
            pricing=[PriceSchedule(**p) for p in data.get("pricing", [])],
            reputation=data.get("reputation", 0.0),
            version=data.get("version", PROTOCOL_VERSION),
            issued_at=data.get("issued_at", 0.0),
            signature=data.get("signature", ""),
        )

    # -- crypto ------------------------------------------------------------

    def sign(self, private_hex: str) -> "CapabilityManifest":
        """Sign the manifest in place; the signer must own ``agent_id``."""
        if crypto.public_key_from_private(private_hex) != self.agent_id:
            raise ValueError("private key does not match agent_id")
        self.signature = crypto.sign(private_hex, self._signable())
        return self

    def verify(self) -> bool:
        """Return True if the signature is valid for this manifest's agent_id."""
        if not self.signature:
            return False
        return crypto.verify(self.agent_id, self._signable(), self.signature)

    # -- queries -----------------------------------------------------------

    def handles(self, task_type: str) -> bool:
        """Whether this agent advertises support for ``task_type``."""
        return task_type in self.task_types

    def price_for(self, task_type: str) -> PriceSchedule | None:
        """Return the PriceSchedule for a task type, or None if unpriced."""
        for schedule in self.pricing:
            if schedule.task_type == task_type:
                return schedule
        return None
