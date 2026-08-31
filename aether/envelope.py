"""
AETHER Task Envelope
====================

Once a deal is struck, the actual work is packaged in a Task Envelope: the
payload to be delivered, the settlement terms it is bound to, a verification
hook that decides whether delivery is acceptable, and an append-only audit
trail that records every state transition for later dispute resolution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import crypto

# A verification hook receives the delivered result and returns True/False.
VerificationHook = Callable[[Any], bool]


@dataclass
class AuditEntry:
    """A single immutable step in an envelope's lifecycle."""

    event: str
    at: float
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.event, "at": self.at, "detail": self.detail}


@dataclass
class TaskEnvelope:
    """Standardized container binding a task to its settlement terms."""

    transaction_id: str
    task_type: str
    requester_id: str
    provider_id: str
    payload: Dict[str, Any]
    price: float
    currency: str
    deadline: float
    settlement_type: str
    acceptance_criteria: str
    status: str = "created"             # created→delivered→verified/rejected
    result: Optional[Any] = None
    audit_trail: List[AuditEntry] = field(default_factory=list)
    _verifier: Optional[VerificationHook] = None

    def __post_init__(self) -> None:
        if not self.audit_trail:
            self._log("created", {"task_type": self.task_type})

    # -- audit -------------------------------------------------------------

    def _log(self, event: str, detail: Dict[str, Any] | None = None) -> None:
        self.audit_trail.append(
            AuditEntry(event=event, at=time.time(), detail=detail or {})
        )

    def audit_digest(self) -> str:
        """Tamper-evident SHA-256 digest over the full audit trail."""
        return crypto.digest({"trail": [e.to_dict() for e in self.audit_trail]})

    # -- lifecycle ---------------------------------------------------------

    def set_verifier(self, hook: VerificationHook) -> "TaskEnvelope":
        """Attach the delivery-verification hook (set by the requester)."""
        self._verifier = hook
        return self

    def deliver(self, result: Any) -> "TaskEnvelope":
        """Provider submits the result, moving the envelope to 'delivered'."""
        self.result = result
        self.status = "delivered"
        self._log("delivered", {"result_digest": crypto.digest({"r": repr(result)})})
        return self

    def verify_delivery(self) -> bool:
        """Run the verification hook; transition to verified or rejected.

        With no hook attached, any non-None delivery is accepted by default.
        """
        if self.status != "delivered":
            self._log("verify_skipped", {"reason": f"status={self.status}"})
            return False
        ok = self._verifier(self.result) if self._verifier else self.result is not None
        self.status = "verified" if ok else "rejected"
        self._log(self.status, {})
        return ok

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "task_type": self.task_type,
            "requester_id": self.requester_id,
            "provider_id": self.provider_id,
            "payload": self.payload,
            "price": self.price,
            "currency": self.currency,
            "deadline": self.deadline,
            "settlement_type": self.settlement_type,
            "acceptance_criteria": self.acceptance_criteria,
            "status": self.status,
            "result": self.result,
            "audit_trail": [e.to_dict() for e in self.audit_trail],
            "audit_digest": self.audit_digest(),
        }


def envelope_from_deal(offer, receipt, payload: Dict[str, Any]) -> TaskEnvelope:
    """Build a TaskEnvelope from a signed offer + acceptance receipt.

    This is the bridge from the handshake layer to the execution layer: the
    agreed terms in the offer become the immutable terms of the envelope.
    """
    return TaskEnvelope(
        transaction_id=receipt.transaction_id,
        task_type=offer.task_type,
        requester_id=offer.requester_id,
        provider_id=offer.provider_id,
        payload=payload,
        price=offer.price,
        currency=offer.currency,
        deadline=offer.deadline,
        settlement_type=offer.settlement_type,
        acceptance_criteria=offer.acceptance_criteria,
    )
