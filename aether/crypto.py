"""
AETHER Crypto Utilities
=======================

Thin, opinionated wrappers around Ed25519 (from the standard `cryptography`
package) for signing and verifying protocol messages.

Ed25519 is chosen because it is fast, produces small (64-byte) signatures,
needs no parameter choices, and is misuse-resistant. Keys are exchanged as
hex strings so they drop cleanly into JSON manifests and handshake messages.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def generate_keypair() -> Tuple[str, str]:
    """Generate a fresh Ed25519 keypair.

    Returns a ``(private_key_hex, public_key_hex)`` tuple. The public key hex
    doubles as an agent's cryptographic identity throughout the protocol.
    Suitable for demos and tests; in production, protect the private key.
    """
    private_key = Ed25519PrivateKey.generate()
    priv_hex = private_key.private_bytes_raw().hex()
    pub_hex = private_key.public_key().public_bytes_raw().hex()
    return priv_hex, pub_hex


def public_key_from_private(private_hex: str) -> str:
    """Derive the hex-encoded public key from a hex-encoded private key."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return private_key.public_key().public_bytes_raw().hex()


def canonical_bytes(payload: Dict[str, Any]) -> bytes:
    """Deterministically serialize a dict so signatures are reproducible.

    Keys are sorted and whitespace is stripped, guaranteeing that two parties
    signing/verifying the same logical message produce identical bytes.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(payload: Dict[str, Any]) -> str:
    """Return a hex SHA-256 digest of a payload (used for audit trails/IDs)."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sign(private_hex: str, payload: Dict[str, Any]) -> str:
    """Sign a payload dict with a hex private key; returns a hex signature."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return private_key.sign(canonical_bytes(payload)).hex()


def verify(public_hex: str, payload: Dict[str, Any], signature_hex: str) -> bool:
    """Verify a hex signature over a payload against a hex public key.

    Returns ``True`` on a valid signature and ``False`` on any failure
    (bad signature, malformed key, or corrupt input) — it never raises.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        public_key.verify(bytes.fromhex(signature_hex), canonical_bytes(payload))
        return True
    except (InvalidSignature, ValueError):
        return False
