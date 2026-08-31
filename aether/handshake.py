"""
AETHER Settlement Handshake
===========================

The four-message negotiation that takes two agents from "I need something"
to "we have a signed deal":

    1. DiscoveryQuery    (requester broadcasts a need)
    2. CapabilityResponse (provider replies with the relevant manifest subset)
    3. SettlementOffer   (requester makes a formal, signed proposal)
    4. AcceptanceReceipt (provider signs a binding commitment + transaction id)

Every message after discovery is signed, so either party can later prove
exactly what was agreed. All messages serialize to plain dicts (JSON-ready).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

from . import crypto
from .manifest import CapabilityManifest


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class DiscoveryQuery:
    """Message 1 — broadcast the task type and constraints you need filled."""

    task_type: str
    requester_id: str
    max_price: float = 0.0              # 0 == no stated ceiling
    currency: str = "USD"
    details: Dict[str, Any] = field(default_factory=dict)
    query_id: str = field(default_factory=lambda: _new_id("qry"))
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveryQuery":
        return cls(**data)


@dataclass
class CapabilityResponse:
    """Message 2 — a provider answers a query with its matching manifest."""

    query_id: str
    manifest: CapabilityManifest
    quoted_price: float
    currency: str = "USD"
    response_id: str = field(default_factory=lambda: _new_id("rsp"))
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_id": self.query_id,
            "manifest": self.manifest.to_dict(),
            "quoted_price": self.quoted_price,
            "currency": self.currency,
            "response_id": self.response_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CapabilityResponse":
        return cls(
            query_id=data["query_id"],
            manifest=CapabilityManifest.from_dict(data["manifest"]),
            quoted_price=data["quoted_price"],
            currency=data.get("currency", "USD"),
            response_id=data["response_id"],
            created_at=data["created_at"],
        )

    def is_valid(self) -> bool:
        """The response is only trustworthy if the manifest signature holds."""
        return self.manifest.verify()


@dataclass
class SettlementOffer:
    """Message 3 — the requester's signed, formal proposal to one provider."""

    query_id: str
    requester_id: str
    provider_id: str
    task_type: str
    price: float
    currency: str
    deadline: float                     # unix timestamp the work is due by
    settlement_type: str                # "immediate" | "escrow" | "phased"
    acceptance_criteria: str
    offer_id: str = field(default_factory=lambda: _new_id("ofr"))
    created_at: float = field(default_factory=lambda: time.time())
    signature: str = ""

    def _signable(self) -> Dict[str, Any]:
        return {
            "query_id": self.query_id,
            "requester_id": self.requester_id,
            "provider_id": self.provider_id,
            "task_type": self.task_type,
            "price": self.price,
            "currency": self.currency,
            "deadline": round(self.deadline, 3),
            "settlement_type": self.settlement_type,
            "acceptance_criteria": self.acceptance_criteria,
            "offer_id": self.offer_id,
            "created_at": round(self.created_at, 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._signable()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SettlementOffer":
        d = dict(data)
        sig = d.pop("signature", "")
        offer = cls(**d)
        offer.signature = sig
        return offer

    def sign(self, private_hex: str) -> "SettlementOffer":
        if crypto.public_key_from_private(private_hex) != self.requester_id:
            raise ValueError("private key does not match requester_id")
        self.signature = crypto.sign(private_hex, self._signable())
        return self

    def verify(self) -> bool:
        if not self.signature:
            return False
        return crypto.verify(self.requester_id, self._signable(), self.signature)


@dataclass
class AcceptanceReceipt:
    """Message 4 — the provider's signed commitment, binding the deal."""

    offer_id: str
    provider_id: str
    requester_id: str
    transaction_id: str = field(default_factory=lambda: _new_id("txn"))
    accepted_at: float = field(default_factory=lambda: time.time())
    signature: str = ""

    def _signable(self) -> Dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "provider_id": self.provider_id,
            "requester_id": self.requester_id,
            "transaction_id": self.transaction_id,
            "accepted_at": round(self.accepted_at, 3),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._signable()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AcceptanceReceipt":
        d = dict(data)
        sig = d.pop("signature", "")
        receipt = cls(**d)
        receipt.signature = sig
        return receipt

    def sign(self, private_hex: str) -> "AcceptanceReceipt":
        if crypto.public_key_from_private(private_hex) != self.provider_id:
            raise ValueError("private key does not match provider_id")
        self.signature = crypto.sign(private_hex, self._signable())
        return self

    def verify(self) -> bool:
        if not self.signature:
            return False
        return crypto.verify(self.provider_id, self._signable(), self.signature)


# -- convenience helpers ---------------------------------------------------

def respond_to_query(
    query: DiscoveryQuery, manifest: CapabilityManifest
) -> CapabilityResponse | None:
    """Build a CapabilityResponse if ``manifest`` can serve ``query``.

    Returns ``None`` when the provider does not handle the task type or its
    quote exceeds the requester's stated ceiling.
    """
    if not manifest.handles(query.task_type):
        return None
    schedule = manifest.price_for(query.task_type)
    quote = schedule.amount if schedule else 0.0
    if query.max_price and quote > query.max_price:
        return None
    return CapabilityResponse(
        query_id=query.query_id,
        manifest=manifest,
        quoted_price=quote,
        currency=query.currency,
    )
