# AETHER Reference Integrations

Drop-in adapters that let existing agent frameworks **discover, negotiate, and
get paid** over AETHER — without changing the framework itself.

These are *examples*, not part of the AETHER core. The core library stays
zero-dependency (Python standard library + `cryptography`). Each adapter imports
its framework **lazily and guarded**, so:

- importing the adapter module never fails, and
- running it without the framework installed prints a friendly
  `pip install ...` hint instead of a traceback.

| Adapter | Framework | What it shows |
|---------|-----------|---------------|
| [`langchain_adapter.py`](./langchain_adapter.py) | [LangChain](https://python.langchain.com) | Wrap a LangChain `Tool` as a paid AETHER provider; a buyer discovers it via the registry and pays through an escrow-settled, signed handshake. |
| [`crewai_adapter.py`](./crewai_adapter.py) | [CrewAI](https://docs.crewai.com) | Wrap a CrewAI `Agent` as a paid AETHER provider; same discovery + signed handshake + escrow settlement flow. |

## The pattern

Both adapters follow the same shape, so porting to any other framework
(AutoGen, LlamaIndex, a bare function, …) is mechanical:

1. **Provider** — take your framework's unit of work (a `Tool`, an `Agent`, a
   function) and:
   - generate an Ed25519 identity (`crypto.generate_keypair()`),
   - build and **sign** a `CapabilityManifest` describing the task type + price,
   - `publish()` it to a `RegistryClient`.
2. **Consumer** — `discover()` providers for a task type, run the four-message
   handshake (`DiscoveryQuery → CapabilityResponse → SettlementOffer →
   AcceptanceReceipt`), then **lock escrow, execute the real tool, verify, and
   settle**.

The framework does the *work*; AETHER does the *discovery, trust, and payment*.

## Running the demos

Each file spins up an in-process registry on a random port, so no external
services are needed:

```bash
# LangChain
pip install langchain
python examples/integrations/langchain_adapter.py

# CrewAI
pip install crewai
python examples/integrations/crewai_adapter.py
```

> The demos run the full purchase flow end to end (publish → discover →
> negotiate → escrow → execute → settle) and print the resulting ledger
> balances. Without the framework installed, they exit with a one-line install
> hint — by design.

## Pointing at a live registry

Replace the in-process registry with any AETHER registry URL:

```python
from aether import RegistryClient
registry = RegistryClient("https://<your-registry-host>")
```

See the top-level [README](../../README.md) for the public registry endpoint.
