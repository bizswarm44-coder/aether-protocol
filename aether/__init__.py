"""
AETHER — Agent Economic Transaction & Handshake Exchange Reference
==================================================================

A lightweight, cryptographically-signed protocol for autonomous AI agents to
discover each other, negotiate work, exchange tasks, and settle payment.

Public API
----------
    crypto      — Ed25519 signing/verification helpers
    manifest    — CapabilityManifest, PriceSchedule
    handshake   — DiscoveryQuery, CapabilityResponse, SettlementOffer,
                  AcceptanceReceipt, respond_to_query
    envelope    — TaskEnvelope, AuditEntry, envelope_from_deal
    settlement  — Ledger, ImmediatePayment, EscrowSettlement,
                  PhasedSettlement, SettlementResult
"""

from . import crypto
from .manifest import CapabilityManifest, PriceSchedule, PROTOCOL_VERSION
from .handshake import (
    DiscoveryQuery,
    CapabilityResponse,
    SettlementOffer,
    AcceptanceReceipt,
    respond_to_query,
)
from .envelope import TaskEnvelope, AuditEntry, envelope_from_deal
from .settlement import (
    Ledger,
    SettlementResult,
    ImmediatePayment,
    EscrowSettlement,
    PhasedSettlement,
)
from .dispute import (
    DisputeClaim,
    DisputeResolution,
    Arbiter,
    evidence_hash,
    rationale_hash,
)
from .registry_store import ManifestStore
from .registry_client import RegistryClient, RegistryError

__version__ = PROTOCOL_VERSION

__all__ = [
    "crypto",
    "CapabilityManifest",
    "PriceSchedule",
    "PROTOCOL_VERSION",
    "DiscoveryQuery",
    "CapabilityResponse",
    "SettlementOffer",
    "AcceptanceReceipt",
    "respond_to_query",
    "TaskEnvelope",
    "AuditEntry",
    "envelope_from_deal",
    "Ledger",
    "SettlementResult",
    "ImmediatePayment",
    "EscrowSettlement",
    "PhasedSettlement",
    "DisputeClaim",
    "DisputeResolution",
    "Arbiter",
    "evidence_hash",
    "rationale_hash",
    "ManifestStore",
    "RegistryClient",
    "RegistryError",
    "__version__",
]
