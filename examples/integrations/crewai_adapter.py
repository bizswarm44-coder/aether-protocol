"""
AETHER × CrewAI — reference adapter
===================================

Shows how a **CrewAI** agent can participate in the AETHER economy:

  * A CrewAI ``Agent`` is wrapped as an AETHER *provider* that advertises a
    signed :class:`CapabilityManifest` and fulfils paid task envelopes.
  * A buyer discovers the provider through the registry and pays for a task via
    the standard four-message handshake + escrow settlement.

Like the LangChain adapter, this is a *reference integration*, NOT part of the
zero-dependency core. CrewAI is imported lazily and guarded, so importing this
module never fails and running it without CrewAI prints a friendly hint.

Run
---
    pip install crewai               # only needed to exercise the live demo
    python examples/integrations/crewai_adapter.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

# Make the repo root importable when run directly.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from aether import (  # noqa: E402
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
# Guarded CrewAI import
# ---------------------------------------------------------------------------
def _require_crewai():
    """Return the CrewAI ``Agent`` class, or raise a friendly error."""
    try:
        from crewai import Agent  # type: ignore
        return Agent
    except Exception as exc:  # pragma: no cover - depends on user's env
        raise SystemExit(
            "This reference integration needs CrewAI.\n"
            "    pip install crewai\n"
            f"(import error: {exc})"
        )


# ---------------------------------------------------------------------------
# Provider side: expose a CrewAI Agent as a paid AETHER capability
# ---------------------------------------------------------------------------
class CrewAIProvider:
    """Wrap a CrewAI ``Agent`` (or any object with a callable executor) so it
    can be sold over AETHER.

    Parameters
    ----------
    agent:
        A CrewAI ``Agent``. Its role/goal define what it does; we only need a
        way to execute a task string against it.
    task_type:
        The AETHER capability name buyers discover.
    price:
        Price per invocation.
    executor:
        Optional callable ``(agent, payload) -> str`` used to run the CrewAI
        agent. Supplied because CrewAI's execution API varies by version and by
        whether you wrap the agent in a ``Task``/``Crew``. Defaults to a simple
        ``agent.execute_task``/``agent.kickoff`` probe.
    """

    def __init__(
        self,
        agent,
        task_type: str,
        price: float,
        display_name: str,
        reputation: float = 0.75,
        currency: str = "USD",
        executor=None,
    ) -> None:
        self.agent = agent
        self.task_type = task_type
        self.price = price
        self.currency = currency
        self.executor = executor or self._default_executor
        self.private_hex, self.agent_id = crypto.generate_keypair()
        self.manifest = CapabilityManifest(
            agent_id=self.agent_id,
            display_name=display_name,
            task_types=[task_type],
            pricing=[PriceSchedule(task_type, price, currency)],
            reputation=reputation,
        ).sign(self.private_hex)

    @staticmethod
    def _default_executor(agent, payload) -> str:
        """Best-effort execution across CrewAI versions."""
        for attr in ("execute_task", "kickoff", "execute"):
            fn = getattr(agent, attr, None)
            if callable(fn):
                try:
                    return str(fn(payload))
                except TypeError:
                    return str(fn())
        raise RuntimeError(
            "Could not execute the CrewAI agent; pass a custom executor="
            "lambda agent, payload: ... to CrewAIProvider."
        )

    def publish(self, registry: RegistryClient) -> None:
        registry.publish(self.manifest)

    def run(self, payload) -> str:
        return self.executor(self.agent, payload)

    def fulfil(self, offer: SettlementOffer) -> AcceptanceReceipt:
        return AcceptanceReceipt(
            offer_id=offer.offer_id,
            provider_id=self.agent_id,
            requester_id=offer.requester_id,
        ).sign(self.private_hex)


# ---------------------------------------------------------------------------
# Consumer side
# ---------------------------------------------------------------------------
class AetherConsumer:
    """A minimal buyer identity that can discover and pay CrewAI providers."""

    def __init__(self, funding: float = 100.0) -> None:
        self.private_hex, self.agent_id = crypto.generate_keypair()
        self.ledger = Ledger({self.agent_id: funding})

    def buy(
        self,
        registry: RegistryClient,
        task_type: str,
        payload,
        provider: "CrewAIProvider",
        max_price: Optional[float] = None,
    ):
        candidates = registry.discover(task_type, max_price=max_price)
        if not candidates:
            raise RuntimeError(f"no providers advertise '{task_type}'")
        best = candidates[0]

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

        receipt = provider.fulfil(offer)

        envelope = envelope_from_deal(offer, receipt, payload=payload)
        envelope.set_verifier(lambda r: r is not None and r != "")
        settlement = EscrowSettlement(self.ledger)
        settlement.lock(envelope)

        result = provider.run(payload)
        envelope.deliver(result)
        envelope.verify_delivery()
        outcome = settlement.settle(envelope)
        return result, outcome


# ---------------------------------------------------------------------------
# Live demo (only runs if CrewAI is installed)
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
    Agent = _require_crewai()

    # A CrewAI agent. Creating one may require an LLM configured in your env;
    # that is a CrewAI concern, not an AETHER one.
    researcher = Agent(
        role="Market Researcher",
        goal="Produce concise competitive summaries",
        backstory="Seasoned analyst who distills markets into 5 bullet points.",
    )

    httpd, registry_url = _start_local_registry()
    registry = RegistryClient(registry_url)
    print(f"Registry live at {registry_url}\n")

    provider = CrewAIProvider(
        agent=researcher,
        task_type="market_research",
        price=4.0,
        display_name="CrewAI Research Guild",
        reputation=0.90,
        # Provide an explicit executor to avoid version-specific surprises:
        executor=lambda agent, payload: f"[{agent.role}] analysed: {payload}",
    )
    provider.publish(registry)
    print(f"Provider published: {provider.manifest.display_name} "
          f"(${provider.price:.2f}/call)")

    consumer = AetherConsumer(funding=50.0)
    result, outcome = consumer.buy(
        registry, "market_research", "EV charging market", provider, max_price=5.0
    )

    print("\n--- Purchase complete ---")
    print("Agent output:", result)
    print("Settled     :", outcome.success, f"(released ${outcome.released:.2f})")
    print(f"Buyer balance   : ${consumer.ledger.balance(consumer.agent_id):.2f}")
    print(f"Provider balance: ${consumer.ledger.balance(provider.agent_id):.2f}")
    httpd.shutdown()


if __name__ == "__main__":
    main()
