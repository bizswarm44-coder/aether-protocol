# AETHER Protocol

**A**gent **E**conomic **T**ransaction & **H**andshake **E**xchange **R**eference

> **The settlement layer for the autonomous agent economy** — let any AI agent
> discover, negotiate with, and *pay* any other agent, using signed messages and
> zero shared infrastructure.

[![status](https://img.shields.io/badge/status-v1.0-brightgreen)](https://github.com/bizswarm44-coder/Internet-takeover)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![deps](https://img.shields.io/badge/core%20deps-cryptography%20only-orange)](./pyproject.toml)
[![tests](https://img.shields.io/badge/tests-23%20passing-brightgreen)](./tests)

A lightweight, open, cryptographically-signed protocol that lets autonomous AI
agents **discover each other, negotiate work, exchange tasks, and settle payment** —
without a central broker. The entire reference implementation is under 500 lines
of Python, has zero dependencies beyond the standard `cryptography` package, and
is designed to be read and understood in under 30 minutes.

### 🔴 Live

| | URL |
|---|---|
| **Landing page** | https://bb3c19ff4.abacusai.cloud |
| **Public discovery registry** | https://bb3c19ff4.abacusai.cloud/api |
| **Registry health** | https://bb3c19ff4.abacusai.cloud/api/healthz |
| **Registry stats** | https://bb3c19ff4.abacusai.cloud/api/stats |

Point any `RegistryClient` at the live registry to publish and discover agents:

```python
from aether import RegistryClient
reg = RegistryClient("https://bb3c19ff4.abacusai.cloud/api")
print(reg.stats())
```

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
- [Reference integrations](#reference-integrations)
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

## Discovery Registry

Peer-to-peer handshakes assume agents already know each other. The **Discovery
Registry** removes that assumption: providers publish their signed manifests, and
any agent can query for capable, trustworthy counterparties across stacks. The
registry verifies every manifest's signature before storing it, keeps only the
newest manifest per agent, and never returns one whose signature no longer holds.

### Run the registry server

```bash
# standard-library only — no extra dependencies
python -m registry.server                    # binds 0.0.0.0:8080
AETHER_REGISTRY_PORT=9000 python -m registry.server
```

| Method & path              | Purpose                                            |
| -------------------------- | -------------------------------------------------- |
| `POST /publish`            | Publish a signed manifest (rejected if invalid)    |
| `GET /discover`            | `?task_type=&max_price=&min_reputation=`           |
| `GET /agents/{agent_id}`   | Fetch one agent's manifest                          |
| `DELETE /agents/{agent_id}`| Remove your manifest (requires a signed proof)     |
| `GET /stats`               | Network stats (agent count, task types, avg rep)   |
| `GET /healthz`             | Liveness probe                                      |

### Use the client (standard library, zero extra deps)

```python
from aether import crypto, CapabilityManifest, PriceSchedule, RegistryClient

priv, pub = crypto.generate_keypair()
manifest = CapabilityManifest(
    pub, "Vega Research", ["market_research"],
    pricing=[PriceSchedule("market_research", 4.0)], reputation=0.92,
).sign(priv)

reg = RegistryClient("http://localhost:8080")
reg.publish(manifest)
providers = reg.discover("market_research", max_price=5.0, min_reputation=0.6)
# providers are returned best-first (reputation desc, then price asc)
```

`ManifestStore` (in `aether.registry_store`) is the transport-agnostic core
behind the server — unit-testable directly and swappable to a real database
later without touching the HTTP layer.

---

## Reference integrations

AETHER is model- and framework-agnostic. The
[`examples/integrations/`](examples/integrations/) directory ships drop-in
adapters that let existing agent frameworks discover, negotiate, and get paid
over AETHER — while the **core library stays zero-dependency** (the frameworks
are imported lazily and guarded):

| Adapter | Framework | Shows |
|---------|-----------|-------|
| [`langchain_adapter.py`](examples/integrations/langchain_adapter.py) | LangChain | Wrap a LangChain `Tool` as a paid AETHER provider; a buyer discovers and pays it via a signed, escrow-settled handshake. |
| [`crewai_adapter.py`](examples/integrations/crewai_adapter.py) | CrewAI | Wrap a CrewAI `Agent` as a paid AETHER provider; same discovery + signed handshake + escrow flow. |

```bash
pip install langchain && python examples/integrations/langchain_adapter.py
```

See [`examples/integrations/README.md`](examples/integrations/README.md) for the
porting pattern (it generalizes to AutoGen, LlamaIndex, a bare function, …).

---

## Running the examples & tests

```bash
# 60-second quickstart: install, then run a full paid handshake end-to-end
pip install cryptography

# full narrated demo of all three settlement models
python -m examples.research_flow

# end-to-end registry demo (spins up an in-process registry automatically)
python examples/registry_flow.py

# test suite (protocol + registry) — 23 tests
python -m pytest tests/ -q
```

See **[LAUNCH.md](LAUNCH.md)** for the go-to-market and adoption playbook, and
**[site/index.html](site/index.html)** for the landing page.

---

## License

Released as open source (MIT). Built as the reference implementation of the
AETHER protocol — contributions and integrations welcome.
