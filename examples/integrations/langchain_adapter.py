"""
AETHER × LangChain — reference adapter
======================================

Shows how an existing **LangChain** agent/tool can plug into the AETHER economy:

  * A *provider* wraps any LangChain ``Tool`` (or ``Runnable``) so it advertises
    a signed :class:`CapabilityManifest` to the discovery registry and fulfils
    paid, signed task envelopes.
  * A *consumer* discovers such a provider through the registry and pays for a
    task using the standard four-message handshake + escrow settlement.

This file is a *reference integration*, NOT part of the zero-dependency core.
LangChain is imported lazily and guarded, so:

    * importing this module never fails, and
    * running it without LangChain prints a friendly "pip install" hint instead
      of a traceback.

Run
---
    pip install langchain            # only needed to exercise the live demo
    python examples/integrations/langchain_adapter.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, List, Optional

# Make the repo root importable when run directly (python examples/.../x.py).
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from aether import (  # noqa: E402  (path bootstrap must precede import)
    crypto,
    CapabilityManifest,
    PriceSchedule,
    DiscoveryQuery,
    respond_to_query,
    SettlementOffer,
    AcceptanceReceipt,
    envelope_from_deal,
    Ledger,
    EscrowSettlement,
    RegistryClient,
)


# ---------------------------------------------------------------------------
# Guarded LangChain import
# ---------------------------------------------------------------------------
def _require_langchain():
    """Return a LangChain ``Tool`` class, or raise a friendly error.

    We only need a callable-with-metadata abstraction from LangChain. We try the
    modern ``langchain_core`` location first, then the legacy one, so this works
    across LangChain versions.
    """
    try:
        from langchain_core.tools import Tool  # type: ignore
        return Tool
    except Exception:
        pass
    try:
        from langchain.tools import Tool  # type: ignore
        return Tool
    except Exception as exc:  # pragma: no cover - depends on user's env
        raise SystemExit(
            "This reference integration needs LangChain.\n"
            "    pip install langchain\n"
            f"(import error: {exc})"
        )


# ---------------------------------------------------------------------------
# Provider side: expose a LangChain Tool as a paid AETHER capability
# ---------------------------------------------------------------------------
class LangChainProvider:
    """Wrap a LangChain ``Tool`` so it can be sold over AETHER.

    Parameters
    ----------
    tool:
        A LangChain ``Tool`` (anything with ``.name`` and a callable ``.func``/
        ``.invoke``). Its output is what buyers pay for.
    task_type:
        The AETHER capability name buyers discover (e.g. ``"summarize"``).
    price:
        Price per invocation, in ``currency``.
    display_name / reputation / currency:
        Manifest metadata.
    """

    def __init__(
        self,
        tool,
        task_type: str,
        price: float,
        display_name: str,
        reputation: float = 0.75,
        currency: str = "USD",
    ) -> None:
        self.tool = tool
        self.task_type = task_type
        self.price = price
        self.currency = currency
        self.private_hex, self.agent_id = crypto.generate_keypair()
        self.manifest = CapabilityManifest(
            agent_id=self.agent_id,
            display_name=display_name,
            task_types=[task_type],
            pricing=[PriceSchedule(task_type, price, currency)],
            reputation=reputation,
        ).sign(self.private_hex)

    # -- registry ----------------------------------------------------------
    def publish(self, registry: RegistryClient) -> None:
        """Advertise this provider's signed manifest to the registry."""
        registry.publish(self.manifest)

    # -- execution ---------------------------------------------------------
    def _run_tool(self, payload) -> str:
        """Invoke the wrapped LangChain tool across LangChain versions."""
        if hasattr(self.tool, "invoke"):
            return self.tool.invoke(payload)
        return self.tool.func(payload)  # legacy Tool

    def fulfil(self, offer: SettlementOffer, payload) -> AcceptanceReceipt:
        """Sign an acceptance receipt committing to the offered deal."""
        return AcceptanceReceipt(
            offer_id=offer.offer_id,
            provider_id=self.agent_id,
            requester_id=offer.requester_id,
        ).sign(self.private_hex)


# ---------------------------------------------------------------------------
# Consumer side: discover + pay a provider for one task
# ---------------------------------------------------------------------------
class AetherConsumer:
    """A minimal buyer identity that can discover and pay providers."""

    def __init__(self, funding: float = 100.0) -> None:
        self.private_hex, self.agent_id = crypto.generate_keypair()
        self.ledger = Ledger({self.agent_id: funding})

    def buy(
        self,
        registry: RegistryClient,
        task_type: str,
        payload,
        provider: "LangChainProvider",
        max_price: Optional[float] = None,
    ):
        """Run the full AETHER purchase flow against one known provider.

        Returns ``(result, settlement_result)``.
        """
        # 1. Discover candidates through the registry (network effect).
        candidates = registry.discover(task_type, max_price=max_price)
        if not candidates:
            raise RuntimeError(f"no providers advertise '{task_type}'")
        best = candidates[0]

        # 2. Negotiate: query -> capability response -> signed offer.
        query = DiscoveryQuery(
            task_type=task_type, requester_id=self.agent_id, max_price=max_price
        )
        response = respond_to_query(query, best)
        offer = SettlementOffer(
            query_id=query.query_id,
            requester_id=self.agent_id,
            provider_id=best.agent_id,
            task_type=task_type,
            price=response.quoted_price,
            currency=best.price_for(task_type).currency,
            deadline=time.time() + 3600,
            settlement_type="escrow",
            acceptance_criteria=f"Fulfil one '{task_type}' task.",
        ).sign(self.private_hex)

        # 3. Provider commits with a signed receipt.
        receipt = provider.fulfil(offer, payload)

        # 4. Escrow: lock funds, run the real LangChain tool, settle.
        envelope = envelope_from_deal(offer, receipt, payload=payload)
        envelope.set_verifier(lambda r: r is not None and r != "")
        settlement = EscrowSettlement(self.ledger)
        settlement.lock(envelope)

        result = provider._run_tool(payload)
        envelope.deliver(result)
        envelope.verify_delivery()
        outcome = settlement.settle(envelope)
        return result, outcome


# ---------------------------------------------------------------------------
# Live demo (only runs if LangChain is installed)
# ---------------------------------------------------------------------------
def _start_local_registry():
    from aether import ManifestStore
    from registry import server

    server.STORE = ManifestStore()
    httpd = server.serve(host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> None:
    Tool = _require_langchain()

    # A trivial LangChain tool. In real life this could be an LLM chain,
    # a retriever, or any Runnable.
    def summarize(text: str) -> str:
        words = text.split()
        head = " ".join(words[:12])
        return f"SUMMARY ({len(words)} words): {head}..."

    lc_tool = Tool(
        name="summarizer",
        func=summarize,
        description="Summarize a block of text.",
    )

    httpd, registry_url = _start_local_registry()
    registry = RegistryClient(registry_url)
    print(f"Registry live at {registry_url}\n")

    provider = LangChainProvider(
        tool=lc_tool,
        task_type="summarize",
        price=0.50,
        display_name="LangChain Summarizer Co.",
        reputation=0.88,
    )
    provider.publish(registry)
    print(f"Provider published: {provider.manifest.display_name} "
          f"(${provider.price:.2f}/call)")

    consumer = AetherConsumer(funding=10.0)
    text = ("AETHER lets autonomous agents discover each other, negotiate a "
            "price, and settle payment with cryptographically signed messages.")
    result, outcome = consumer.buy(
        registry, "summarize", text, provider, max_price=1.0
    )

    print("\n--- Purchase complete ---")
    print("Tool output :", result)
    print("Settled     :", outcome.success, f"(released ${outcome.released:.2f})")
    print(f"Buyer balance   : ${consumer.ledger.balance(consumer.agent_id):.2f}")
    print(f"Provider balance: ${consumer.ledger.balance(provider.agent_id):.2f}")
    httpd.shutdown()


if __name__ == "__main__":
    main()
