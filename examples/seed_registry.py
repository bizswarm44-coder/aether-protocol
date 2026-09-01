"""
AETHER Registry Seeder
======================

Publishes a small set of realistic, diverse agent capability manifests to a
LIVE AETHER Discovery Registry, so first-time visitors see a working
marketplace instead of an empty page.

It uses only the existing AETHER library APIs — the same ``CapabilityManifest``
/ ``PriceSchedule`` / ``RegistryClient`` patterns shown in
``examples/registry_flow.py`` — and introduces no new protocol surface.

Usage
-----
    # seed the live registry (default)
    python examples/seed_registry.py

    # seed a different registry
    AETHER_REGISTRY_URL=http://localhost:8090 python examples/seed_registry.py

Notes
-----
* **Idempotent-friendly.** Re-running is safe. Each seed agent uses a
  *deterministic* Ed25519 identity derived from a fixed seed phrase, so re-runs
  republish the *same* agent_ids (an update in place) rather than piling up
  duplicates. Any per-agent publish error is caught and reported without
  aborting the rest of the run.
* **No private keys are committed.** Identities are derived in-memory at
  runtime from the seed phrases below. If you pass ``--save-keys`` the private
  keys are written to ``examples/.seed_keys.json``, which is gitignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

# Allow running directly (python examples/seed_registry.py) by putting the repo
# root on the path so the ``aether`` package imports cleanly without install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aether import (  # noqa: E402  (path bootstrap must precede import)
    crypto,
    CapabilityManifest,
    PriceSchedule,
    RegistryClient,
)
from aether.registry_client import RegistryError  # noqa: E402

# The live registry, overridable via env var for local/staging targets.
DEFAULT_REGISTRY_URL = "https://bb3c19ff4.abacusai.cloud/api"


# ---------------------------------------------------------------------------
# Deterministic identities
# ---------------------------------------------------------------------------
def keypair_from_seed(seed_phrase: str) -> tuple[str, str]:
    """Derive a *stable* Ed25519 keypair from a human-readable seed phrase.

    ``crypto.generate_keypair()`` is random; for an idempotent seeder we want
    the same agent_id every run. Ed25519 private keys are 32 bytes, so we use a
    SHA-256 digest of the seed phrase as deterministic key material and load it
    through the same ``cryptography`` primitives the core library uses.

    Returns ``(private_hex, public_hex)`` — identical in shape to
    ``crypto.generate_keypair()``.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization

    key_material = hashlib.sha256(seed_phrase.encode("utf-8")).digest()  # 32 bytes
    private = Ed25519PrivateKey.from_private_bytes(key_material)
    private_hex = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    # Derive the public key hex the same way the core library represents ids.
    public_hex = crypto.public_key_from_private(private_hex)
    return private_hex, public_hex


# ---------------------------------------------------------------------------
# Seed catalogue
# ---------------------------------------------------------------------------
@dataclass
class SeedAgent:
    """A declarative description of one agent to publish."""

    seed_phrase: str          # stable identity source (never leaves this file)
    display_name: str
    task_types: List[str]
    pricing: List[PriceSchedule]
    reputation: float
    # Documentation only: the settlement model this agent is designed for.
    # (Settlement type is negotiated per-deal in the SettlementOffer, not stored
    # on the manifest, so this is informational for the printed summary.)
    settlement_model: str

    def build_manifest(self) -> tuple[CapabilityManifest, str]:
        """Return a signed manifest and its private key (kept in-memory)."""
        private_hex, public_hex = keypair_from_seed(self.seed_phrase)
        manifest = CapabilityManifest(
            agent_id=public_hex,
            display_name=self.display_name,
            task_types=self.task_types,
            pricing=self.pricing,
            reputation=self.reputation,
        ).sign(private_hex)
        return manifest, private_hex


# Four realistic, diverse providers spanning the platform's settlement models.
SEED_AGENTS: List[SeedAgent] = [
    SeedAgent(
        seed_phrase="aether.seed.orion-web-intelligence.v1",
        display_name="Orion Web Intelligence",
        task_types=["web_research", "competitive_analysis"],
        pricing=[
            PriceSchedule("web_research", 6.00, unit="per_task"),
            PriceSchedule("competitive_analysis", 12.00, unit="per_task"),
        ],
        reputation=0.91,
        settlement_model="escrow",  # higher-value research: buyer wants escrow
    ),
    SeedAgent(
        seed_phrase="aether.seed.lyra-summarizer.v1",
        display_name="Lyra Summarizer",
        task_types=["summarize", "translate_summary"],
        pricing=[
            PriceSchedule("summarize", 0.50, unit="per_task"),
            PriceSchedule("translate_summary", 0.90, unit="per_task"),
        ],
        reputation=0.84,
        settlement_model="immediate",  # cheap, fast, low-risk -> pay on delivery
    ),
    SeedAgent(
        seed_phrase="aether.seed.vega-etl-refinery.v1",
        display_name="Vega ETL Refinery",
        task_types=["data_cleaning", "schema_mapping"],
        pricing=[
            PriceSchedule("data_cleaning", 3.50, unit="per_task"),
            PriceSchedule("schema_mapping", 5.00, unit="per_task"),
        ],
        reputation=0.88,
        settlement_model="phased",  # multi-stage pipeline -> reputation-weighted
    ),
    SeedAgent(
        seed_phrase="aether.seed.atlas-code-review.v1",
        display_name="Atlas Code Review",
        task_types=["code_review", "security_audit"],
        pricing=[
            PriceSchedule("code_review", 4.00, unit="per_task"),
            PriceSchedule("security_audit", 15.00, unit="per_task"),
        ],
        reputation=0.93,
        settlement_model="escrow",  # trust-sensitive audit -> escrow
    ),
]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed(registry_url: str, save_keys: bool = False) -> int:
    """Publish all seed agents to ``registry_url``. Returns the count published.

    Errors on individual agents are caught and reported so one bad publish never
    aborts the whole run (idempotency-friendly).
    """
    reg = RegistryClient(registry_url)
    print(f"Seeding AETHER registry at: {registry_url}\n")

    published = 0
    saved_keys: dict[str, str] = {}

    for agent in SEED_AGENTS:
        manifest, private_hex = agent.build_manifest()
        short_id = manifest.agent_id[:16]
        try:
            reg.publish(manifest)
            published += 1
            prices = ", ".join(
                f"{p.task_type} ${p.amount:.2f}" for p in agent.pricing
            )
            print(f"  [OK]   {agent.display_name}")
            print(f"         id={short_id}...  rep={agent.reputation:.2f}  "
                  f"settlement={agent.settlement_model}")
            print(f"         caps: {prices}")
            saved_keys[manifest.agent_id] = private_hex
        except RegistryError as exc:
            # Duplicate/validation/transient errors: report and keep going.
            print(f"  [SKIP] {agent.display_name}: {exc}")
        except Exception as exc:  # network etc. — never abort the batch
            print(f"  [ERR]  {agent.display_name}: {exc}")

    if save_keys and saved_keys:
        # Written to a gitignored path; never commit private keys.
        keys_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".seed_keys.json")
        with open(keys_path, "w", encoding="utf-8") as fh:
            json.dump(saved_keys, fh, indent=2)
        print(f"\nPrivate keys written to {keys_path} (gitignored).")

    return published


def summarize(registry_url: str) -> None:
    """Query the registry and print what is now discoverable per task type."""
    reg = RegistryClient(registry_url)
    print("\n" + "=" * 66)
    print("  Registry summary")
    print("=" * 66)
    try:
        print("Stats:", reg.stats())
    except Exception as exc:
        print(f"Could not fetch stats: {exc}")

    task_types = sorted({t for a in SEED_AGENTS for t in a.task_types})
    total_matches = 0
    for task_type in task_types:
        try:
            matches = reg.discover(task_type)
            total_matches += len(matches)
            names = ", ".join(m.display_name for m in matches) or "(none)"
            print(f"  discover('{task_type}') -> {len(matches)}: {names}")
        except Exception as exc:
            print(f"  discover('{task_type}') -> error: {exc}")
    print(f"\nTotal discoverable manifests across seeded task types: "
          f"{total_matches}")


def main() -> None:
    registry_url = os.environ.get("AETHER_REGISTRY_URL", DEFAULT_REGISTRY_URL)
    save_keys = "--save-keys" in sys.argv[1:]

    count = seed(registry_url, save_keys=save_keys)
    print(f"\nPublished/updated {count}/{len(SEED_AGENTS)} agents.")
    summarize(registry_url)


if __name__ == "__main__":
    main()
