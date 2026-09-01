"""
AETHER Registry Flow — end-to-end demo
======================================

Shows the *network* version of AETHER: instead of two agents that already know
each other, providers publish to a shared Discovery Registry and a requester
finds them dynamically, then runs the normal handshake + settlement flow.

This script is self-contained: it spins up an in-process registry server on a
random port, so you can just run it directly:

    python examples/registry_flow.py

To run against a standalone registry instead, start the server in one terminal:

    python -m registry.server            # http://localhost:8080

...and set REGISTRY_URL below to that address.
"""

import os
import sys
import threading
import time

# Allow running directly (python examples/registry_flow.py) by putting the
# repo root on the path so both `aether` and `registry` import cleanly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aether import (
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
    ManifestStore,
    RegistryClient,
)


def banner(text):
    print(f"\n{'=' * 66}\n  {text}\n{'=' * 66}")


def start_local_registry():
    """Start an in-process registry on a random port; return its base URL."""
    from registry import server
    server.STORE = ManifestStore()  # fresh in-memory store
    httpd = server.serve(host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return httpd, f"http://127.0.0.1:{port}"


def main():
    httpd, registry_url = start_local_registry()
    reg = RegistryClient(registry_url)
    banner(f"AETHER Discovery Registry live at {registry_url}")

    # -- 1. Providers publish their signed manifests -----------------------
    vega_priv, vega_pub = crypto.generate_keypair()
    vega = CapabilityManifest(
        agent_id=vega_pub,
        display_name="Vega Research",
        task_types=["market_research"],
        pricing=[PriceSchedule("market_research", 4.0)],
        reputation=0.92,
    ).sign(vega_priv)

    lyra_priv, lyra_pub = crypto.generate_keypair()
    lyra = CapabilityManifest(
        agent_id=lyra_pub,
        display_name="Lyra Analytics",
        task_types=["market_research"],
        pricing=[PriceSchedule("market_research", 2.5)],
        reputation=0.61,
    ).sign(lyra_priv)

    reg.publish(vega)
    reg.publish(lyra)
    print("Published 2 provider manifests to the registry.")
    print("Registry stats:", reg.stats())

    # -- 2. Requester discovers providers via the registry -----------------
    orion_priv, orion_pub = crypto.generate_keypair()
    banner("Orion discovers providers for 'market_research' (max $5, rep >= 0.6)")
    providers = reg.discover("market_research", max_price=5.0, min_reputation=0.6)
    for m in providers:
        price = m.price_for("market_research").amount
        print(f"  - {m.display_name:18} rep={m.reputation:.2f}  ${price:.2f}")

    best = providers[0]  # registry already sorted by reputation, then price
    print(f"\nOrion selects: {best.display_name}")

    # -- 3. Standard handshake with the chosen provider --------------------
    provider_priv = vega_priv if best.agent_id == vega_pub else lyra_priv
    query = DiscoveryQuery(
        task_type="market_research",
        requester_id=orion_pub,
        max_price=5.0,
    )
    response = respond_to_query(query, best)
    offer = SettlementOffer(
        query_id=query.query_id,
        requester_id=orion_pub,
        provider_id=best.agent_id,
        task_type="market_research",
        price=response.quoted_price,
        currency="USD",
        deadline=time.time() + 3600,
        settlement_type="escrow",
        acceptance_criteria="Deliver a 5-point competitor summary.",
    ).sign(orion_priv)
    receipt = AcceptanceReceipt(
        offer_id=offer.offer_id,
        provider_id=best.agent_id,
        requester_id=orion_pub,
    ).sign(provider_priv)
    print(f"Deal signed. Transaction id: {receipt.transaction_id}")

    # -- 4. Execute + settle via escrow ------------------------------------
    banner("Task execution + escrow settlement")
    envelope = envelope_from_deal(offer, receipt, payload={"topic": "EV chargers"})
    envelope.set_verifier(lambda result: "summary" in result)

    ledger = Ledger({orion_pub: 100.0})
    settlement = EscrowSettlement(ledger)
    settlement.lock(envelope)  # funds move requester -> escrow before work starts
    print(f"Escrow locked. Orion balance now: ${ledger.balance(orion_pub):.2f}")

    envelope.deliver({"summary": "5 competitors mapped; pricing gaps identified."})
    verified = envelope.verify_delivery()
    result = settlement.settle(envelope)

    print(f"Delivery verified: {verified}")
    print(f"Settlement success: {result.success}  (released ${result.released:.2f})")
    print(f"Orion balance:  ${ledger.balance(orion_pub):.2f}")
    print(f"{best.display_name} balance: ${ledger.balance(best.agent_id):.2f}")

    httpd.shutdown()
    banner("Registry flow complete")


if __name__ == "__main__":
    main()
