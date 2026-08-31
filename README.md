# AETHER Protocol

**A**gent **E**conomic **T**ransaction & **H**andshake **E**xchange **R**eference

A lightweight, open, cryptographically-signed protocol that lets autonomous AI
agents **discover each other, negotiate work, exchange tasks, and settle payment** —
without a central broker. The entire reference implementation is under 500 lines
of Python, has zero dependencies beyond the standard `cryptography` package, and
is designed to be read and understood in under 30 minutes.

> **Why AETHER?** As AI agents start doing real economic work for one another,
> they need a common, trust-minimized way to advertise services, agree on terms,
> and get paid. AETHER is that thin, model-agnostic layer — the TCP/IP of
> agent-to-agent commerce.

---

## Table of contents

- [Protocol overview](#protocol-overview)
- [Install](#install)
- [Quick start](#quick-start)
- [The four-message handshake](#the-four-message-handshake)
- [Settlement models](#settlement-models)
- [API reference](#api-reference)
- [Design principles](#design-principles)
- [Running the example & tests](#running-the-example--tests)

---

## Protocol overview

AETHER is built from three core primitives and a set of settlement primitives:

| Primitive | Module | Purpose |
|-----------|--------|---------|
| **Capability Manifest** | `aether.manifest` | A signed advertisement of an agent's identity, task types, pricing, and reputation. |
| **Settlement Handshake** | `aether.handshake` | A four-message negotiation: discovery → response → offer → acceptance. |
| **Task Envelope** | `aether.envelope` | A signed container binding a task payload to settlement terms, with verification hooks and an audit trail. |
| **Settlement Primitives** | `aether.settlement` | Three payment models: immediate, escrow, and reputation-weighted phased release. |
| **Crypto Utilities** | `aether.crypto` | Ed25519 keygen, signing, verification, canonical serialization. |

Every identity in AETHER **is** an Ed25519 public key (hex-encoded). Every
message that matters is signed, so any party can later prove exactly what was
agreed.

The end-to-end lifecycle:

```
  Provider                         Requester
     │                                 │
     │  1. publish CapabilityManifest  │
     │◀────── DiscoveryQuery ──────────│   (2)
     │─── CapabilityResponse ─────────▶│   (3)
     │◀────── SettlementOffer ─────────│   (4, signed)
     │─── AcceptanceReceipt ──────────▶│   (5, signed)
     │                                 │
     │        TaskEnvelope created (6) │
     │─── deliver(result) ────────────▶│   (7)
     │                verify_delivery() │   (8)
     │◀──────── settle() ──────────────│   (9)  funds move
```

---

## Install

Requires **Python 3.8+**.

```bash
# from the repo root
pip install cryptography      # the only runtime dependency
pip install -e .              # optional: install AETHER as an editable package
```

---

## Quick start

```python
import time
from aether import (
    crypto, CapabilityManifest, PriceSchedule, DiscoveryQuery,
    respond_to_query, SettlementOffer, AcceptanceReceipt,
    envelope_from_deal, Ledger, ImmediatePayment,
)

# 1. identities are keypairs; the public key IS the agent id
requester_priv, requester_id = crypto.generate_keypair()
provider_priv, provider_id = crypto.generate_keypair()

# 2. provider advertises a signed manifest
manifest = CapabilityManifest(
    agent_id=provider_id,
    display_name="Research Bot",
    task_types=["market_research"],
    pricing=[PriceSchedule("market_research", amount=25.0)],
    reputation=0.8,
).sign(provider_priv)
assert manifest.verify()

# 3. requester discovers, provider responds
query = DiscoveryQuery("market_research", requester_id, max_price=40.0)
response = respond_to_query(query, manifest)      # None if no match / too pricey

# 4. requester makes a signed offer, provider signs acceptance
offer = SettlementOffer(
    query_id=query.query_id, requester_id=requester_id, provider_id=provider_id,
    task_type="market_research", price=response.quoted_price, currency="USD",
    deadline=time.time() + 3600, settlement_type="immediate",
    acceptance_criteria="Return >=3 findings",
).sign(requester_priv)
receipt = AcceptanceReceipt(offer.offer_id, provider_id, requester_id).sign(provider_priv)

# 5. package the task, deliver, verify, and settle
env = envelope_from_deal(offer, receipt, payload={"topic": "AI payments"})
env.set_verifier(lambda r: isinstance(r, list) and len(r) >= 3)
env.deliver([{"finding": "..."}, {"finding": "..."}, {"finding": "..."}])
env.verify_delivery()                              # -> True, status == "verified"

ledger = Ledger({requester_id: 100.0})
result = ImmediatePayment(ledger).settle(env)      # funds move on verification
print(result.to_dict())
```

For the full, narrated walkthrough (all three settlement models), see
[`examples/research_flow.py`](examples/research_flow.py).

---

## The four-message handshake

| # | Message | Sent by | Signed? | Meaning |
|---|---------|---------|---------|---------|
| 1 | `DiscoveryQuery` | Requester | no (broadcast) | "I need `task_type`, up to `max_price`." |
| 2 | `CapabilityResponse` | Provider | manifest is signed | "Here's my manifest and quote." |
| 3 | `SettlementOffer` | Requester | ✅ | "Formal proposal: price, deadline, criteria, settlement model." |
| 4 | `AcceptanceReceipt` | Provider | ✅ | "I commit. Here is the transaction id." |

Use `respond_to_query(query, manifest)` to auto-build message 2 — it returns
`None` when the provider doesn't handle the task type or its quote exceeds the
requester's ceiling.

---

## Settlement models

All three implement the same `settle(envelope) -> SettlementResult` contract, so
they are drop-in interchangeable.

| Model | Class | Behavior |
|-------|-------|----------|
| **Immediate** | `ImmediatePayment` | Pays the full price the instant delivery verifies. Ideal for low-value micro-tasks. |
| **Escrow** | `EscrowSettlement` | `lock()` moves funds into escrow up front; `settle()` releases to the provider on verify, or refunds the requester on rejection. |
| **Phased** | `PhasedSettlement` | Reputation-weighted staged release: `release_upfront()` pays a fraction proportional to provider reputation now; `settle()` releases the remainder on verify. |

The `Ledger` is a trivial in-memory balance sheet — swap it for a real payment
rail (chain, stablecoin, bank API) without touching any protocol code.

---

## API reference

### `aether.crypto`

| Function | Description |
|----------|-------------|
| `generate_keypair() -> (priv_hex, pub_hex)` | New Ed25519 keypair; the public hex is the agent id. |
| `public_key_from_private(priv_hex) -> str` | Derive public key from private. |
| `canonical_bytes(payload) -> bytes` | Deterministic JSON serialization for reproducible signing. |
| `digest(payload) -> str` | Hex SHA-256 of a payload (used for ids/audit). |
| `sign(priv_hex, payload) -> str` | Hex Ed25519 signature over a dict. |
| `verify(pub_hex, payload, sig_hex) -> bool` | Verify a signature; never raises. |

### `aether.manifest`

- **`PriceSchedule(task_type, amount, currency="USD", unit="per_task")`**
- **`CapabilityManifest(agent_id, display_name, task_types, pricing=[], reputation=0.0, ...)`**
  - `.sign(priv_hex)` → sign in place (signer must own `agent_id`)
  - `.verify() -> bool` — validate signature
  - `.handles(task_type) -> bool`
  - `.price_for(task_type) -> PriceSchedule | None`
  - `.to_dict()` / `CapabilityManifest.from_dict(d)` — JSON round-trip

### `aether.handshake`

- **`DiscoveryQuery(task_type, requester_id, max_price=0.0, currency="USD", details={})`**
- **`CapabilityResponse(query_id, manifest, quoted_price, ...)`** — `.is_valid()` checks manifest signature
- **`SettlementOffer(query_id, requester_id, provider_id, task_type, price, currency, deadline, settlement_type, acceptance_criteria)`** — `.sign(priv)` / `.verify()`
- **`AcceptanceReceipt(offer_id, provider_id, requester_id)`** — `.sign(priv)` / `.verify()`; auto-generates `transaction_id`
- **`respond_to_query(query, manifest) -> CapabilityResponse | None`**

All messages provide `.to_dict()` / `.from_dict()` for JSON transport.

### `aether.envelope`

- **`TaskEnvelope(...)`**
  - `.set_verifier(hook)` — attach a `result -> bool` verification hook
  - `.deliver(result)` — provider submits work (status → `delivered`)
  - `.verify_delivery() -> bool` — run hook (status → `verified`/`rejected`)
  - `.audit_digest() -> str` — tamper-evident digest of the audit trail
  - `.to_dict()`
- **`envelope_from_deal(offer, receipt, payload) -> TaskEnvelope`** — bridge handshake → execution

### `aether.settlement`

- **`Ledger(balances={})`** — `.balance(acct)`, `.transfer(src, dst, amount)`
- **`ImmediatePayment(ledger)`** — `.settle(env)`
- **`EscrowSettlement(ledger, escrow_account="escrow")`** — `.lock(env)`, `.settle(env)`
- **`PhasedSettlement(ledger, reputation, min_upfront=0.1, max_upfront=0.6)`** — `.release_upfront(env)`, `.settle(env)`
- **`SettlementResult`** — `transaction_id, model, released, refunded, success, notes`; `.to_dict()`

---

## Design principles

1. **Extreme simplicity.** Core implementation is < 500 lines of logical code.
2. **Model- & platform-agnostic.** Task types, pricing units, and payment rails
   are all free-form strings / pluggable — AETHER never assumes your stack.
3. **Signed by default.** Identity is a public key; every commitment is signed.
4. **JSON-native.** Every message round-trips through `to_dict()` / `from_dict()`
   so you can send it over any transport.
5. **Auditable.** Every envelope carries an append-only, hash-chained audit trail.

---

## Running the example & tests

```bash
# full narrated demo of all three settlement models
python -m examples.research_flow

# test suite
python -m pytest tests/ -q      # or: python tests/test_protocol.py
```

---

## License

Released as open source (MIT). Built as the reference implementation of the
AETHER protocol — contributions and integrations welcome.
