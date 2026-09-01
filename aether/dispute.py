"""
AETHER Dispute Resolution (v0.2, Extension 1)
=============================================

Escrow answers the happy path; disputes answer the unhappy one — a provider
delivers garbage, never delivers, or a requester refuses to pay for good work.

The protocol stays **dumb about who is right**: it defines the messages and the
state machine, but the judgment call (arbitration) is pluggable. Two new signed
messages carry a dispute end to end, following the exact pattern of
``SettlementOffer`` / ``AcceptanceReceipt`` (a ``_signable()`` dict, ``sign()``,
``verify()``):

    DisputeClaim      → raised by either party against a locked/delivered escrow
    DisputeResolution → issued by the agreed arbiter, releasing funds per verdict

Large evidence (logs, transcripts, output) stays off the wire: only its SHA-256
hash travels, consistent with the envelope audit-trail approach.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict

try:  # Protocol is stdlib on 3.8+; typing_extensions never required.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - defensive for very old runtimes
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from . import crypto

# Allowed enum values (kept as simple frozensets so the core stays tiny).
CLAIM_REASONS = frozenset(
    {"non_delivery", "quality", "scope", "non_payment", "other"}
)
REQUESTED_OUTCOMES = frozenset({"refund", "release", "split"})
VERDICTS = frozenset({"release_to_provider", "refund_to_requester", "split"})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def evidence_hash(blob: bytes) -> str:
    """SHA-256 hex of an off-protocol evidence/rationale blob."""
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    return crypto.digest({"blob": blob.hex()})


# Rationale hashing is the same operation; expose a clearly named alias.
rationale_hash = evidence_hash


@dataclass
class DisputeClaim:
    """Raised by either party against a locked/delivered escrow envelope."""

    envelope_id: str
    claimant: str                       # agent id raising the claim (signer)
    respondent: str
    reason: str                         # one of CLAIM_REASONS
    evidence_hash: str                  # SHA-256 hex of off-protocol evidence
    requested_outcome: str              # one of REQUESTED_OUTCOMES
    dispute_id: str = field(default_factory=lambda: _new_id("dsp"))
    opened_at: float = field(default_factory=lambda: time.time())
    signature: str = ""

    def _signable(self) -> Dict[str, Any]:
        return {
            "dispute_id": self.dispute_id,
            "envelope_id": self.envelope_id,
            "claimant": self.claimant,
            "respondent": self.respondent,
            "reason": self.reason,
            "evidence_hash": self.evidence_hash,
            "requested_outcome": self.requested_outcome,
            "opened_at": round(self.opened_at, 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._signable()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DisputeClaim":
        d = dict(data)
        sig = d.pop("signature", "")
        claim = cls(**d)
        claim.signature = sig
        return claim

    def sign(self, private_hex: str) -> "DisputeClaim":
        if crypto.public_key_from_private(private_hex) != self.claimant:
            raise ValueError("private key does not match claimant")
        if self.reason not in CLAIM_REASONS:
            raise ValueError(f"invalid reason: {self.reason}")
        if self.requested_outcome not in REQUESTED_OUTCOMES:
            raise ValueError(f"invalid requested_outcome: {self.requested_outcome}")
        self.signature = crypto.sign(private_hex, self._signable())
        return self

    def verify(self) -> bool:
        if not self.signature:
            return False
        return crypto.verify(self.claimant, self._signable(), self.signature)


@dataclass
class DisputeResolution:
    """Issued by the agreed arbiter, releasing funds per its verdict."""

    dispute_id: str
    envelope_id: str
    arbiter: str                        # agent id (signer); must match the offer
    verdict: str                        # one of VERDICTS
    rationale_hash: str                 # SHA-256 hex of the arbiter's reasoning
    split_bps: int = 0                  # basis points to provider when split
    resolved_at: float = field(default_factory=lambda: time.time())
    signature: str = ""

    def _signable(self) -> Dict[str, Any]:
        return {
            "dispute_id": self.dispute_id,
            "envelope_id": self.envelope_id,
            "arbiter": self.arbiter,
            "verdict": self.verdict,
            "split_bps": self.split_bps,
            "rationale_hash": self.rationale_hash,
            "resolved_at": round(self.resolved_at, 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._signable()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DisputeResolution":
        d = dict(data)
        sig = d.pop("signature", "")
        resolution = cls(**d)
        resolution.signature = sig
        return resolution

    def sign(self, private_hex: str) -> "DisputeResolution":
        if crypto.public_key_from_private(private_hex) != self.arbiter:
            raise ValueError("private key does not match arbiter")
        if self.verdict not in VERDICTS:
            raise ValueError(f"invalid verdict: {self.verdict}")
        if self.verdict == "split" and not (0 <= self.split_bps <= 10000):
            raise ValueError("split_bps must be within [0, 10000]")
        self.signature = crypto.sign(private_hex, self._signable())
        return self

    def verify(self) -> bool:
        if not self.signature:
            return False
        return crypto.verify(self.arbiter, self._signable(), self.signature)


@runtime_checkable
class Arbiter(Protocol):
    """Pluggable arbitration interface.

    An arbiter examines a signed ``DisputeClaim`` (and any off-protocol evidence
    it can fetch via ``evidence_hash``) and returns a *signed*
    ``DisputeResolution``. Implementations may be an automated check, a human in
    the loop, or a third-party agent discovered through AETHER itself — the
    protocol neither knows nor cares which.
    """

    def resolve(self, claim: DisputeClaim) -> DisputeResolution:
        ...
