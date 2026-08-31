"""
AETHER end-to-end example: research task delegation & settlement
================================================================

Two agents:
  * Requester "Orion" needs a market-research brief.
  * Provider  "Vega"  advertises research capability.

We walk the full protocol:
    discovery → capability response → signed offer → signed acceptance →
    task envelope → delivery → verification → settlement (all 3 models).

Run it:  python -m examples.research_flow      (from the repo root)
"""

import time

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
    ImmediatePayment,
    EscrowSettlement,
    PhasedSettlement,
)

LINE = "─" * 68


def step(n: int, title: str) -> None:
    print(f"\n{LINE}\nSTEP {n}: {title}\n{LINE}")


def run_flow(settlement_model: str) -> None:
    print("\n" + "=" * 68)
    print(f"  AETHER PROTOCOL DEMO  —  settlement model: {settlement_model.upper()}")
    print("=" * 68)

    # --- keys / identities -------------------------------------------------
    orion_priv, orion_id = crypto.generate_keypair()   # requester
    vega_priv, vega_id = crypto.generate_keypair()     # provider

    # --- STEP 1: provider publishes a signed capability manifest ----------
    step(1, "Provider 'Vega' publishes a signed Capability Manifest")
    manifest = CapabilityManifest(
        agent_id=vega_id,
        display_name="Vega Research Agent",
        task_types=["market_research", "summarization"],
        pricing=[
            PriceSchedule("market_research", amount=25.0, unit="per_task"),
            PriceSchedule("summarization", amount=5.0, unit="per_task"),
        ],
        reputation=0.82,
    ).sign(vega_priv)
    print(f"  agent_id     : {manifest.agent_id[:24]}…")
    print(f"  task_types   : {manifest.task_types}")
    print(f"  reputation   : {manifest.reputation}")
    print(f"  signature ok : {manifest.verify()}")

    # --- STEP 2: requester broadcasts a discovery query -------------------
    step(2, "Requester 'Orion' broadcasts a Discovery Query")
    query = DiscoveryQuery(
        task_type="market_research",
        requester_id=orion_id,
        max_price=40.0,
        details={"topic": "AI agent payment protocols", "depth": "brief"},
    )
    print(f"  need         : {query.task_type}")
    print(f"  max_price    : ${query.max_price}")
    print(f"  query_id     : {query.query_id}")

    # --- STEP 3: provider responds with matching manifest subset ----------
    step(3, "Provider replies with a Capability Response")
    response = respond_to_query(query, manifest)
    assert response is not None, "provider could not serve the query"
    print(f"  quoted_price : ${response.quoted_price}")
    print(f"  manifest ok  : {response.is_valid()}")

    # --- STEP 4: requester makes a signed settlement offer ----------------
    step(4, "Requester sends a signed Settlement Offer")
    offer = SettlementOffer(
        query_id=query.query_id,
        requester_id=orion_id,
        provider_id=vega_id,
        task_type=query.task_type,
        price=response.quoted_price,
        currency="USD",
        deadline=time.time() + 3600,
        settlement_type=settlement_model,
        acceptance_criteria="Return >=3 sourced findings as a JSON list.",
    ).sign(orion_priv)
    print(f"  price        : ${offer.price}")
    print(f"  settlement   : {offer.settlement_type}")
    print(f"  offer signed : {offer.verify()}")

    # --- STEP 5: provider signs an acceptance receipt ---------------------
    step(5, "Provider returns a signed Acceptance Receipt")
    receipt = AcceptanceReceipt(
        offer_id=offer.offer_id,
        provider_id=vega_id,
        requester_id=orion_id,
    ).sign(vega_priv)
    print(f"  txn_id       : {receipt.transaction_id}")
    print(f"  receipt ok   : {receipt.verify()}")

    # --- STEP 6: build the task envelope from the agreed deal -------------
    step(6, "Task Envelope created from the signed deal")
    env = envelope_from_deal(offer, receipt, payload=query.details)
    # requester attaches a verification hook (>=3 findings required)
    env.set_verifier(lambda r: isinstance(r, list) and len(r) >= 3)
    print(f"  txn_id       : {env.transaction_id}")
    print(f"  status       : {env.status}")

    # --- fund the ledger --------------------------------------------------
    ledger = Ledger({orion_id: 100.0, vega_id: 0.0})

    # escrow locks funds up front; phased releases an up-front portion now
    if settlement_model == "escrow":
        escrow = EscrowSettlement(ledger)
        escrow.lock(env)
    elif settlement_model == "phased":
        phased = PhasedSettlement(ledger, reputation=manifest.reputation)
        up = phased.release_upfront(env)
        print(f"  upfront paid : ${up} ({phased.upfront_fraction:.0%} of price)")

    # --- STEP 7: provider delivers the work -------------------------------
    step(7, "Provider delivers the result")
    findings = [
        {"finding": "Agent payment volume growing", "source": "example.com/a"},
        {"finding": "Escrow reduces counterparty risk", "source": "example.com/b"},
        {"finding": "Signed manifests enable trustless discovery", "source": "example.com/c"},
    ]
    env.deliver(findings)
    print(f"  status       : {env.status}  ({len(findings)} findings)")

    # --- STEP 8: requester verifies delivery ------------------------------
    step(8, "Requester verifies the delivery")
    ok = env.verify_delivery()
    print(f"  verified     : {ok}  (status={env.status})")

    # --- STEP 9: settle ---------------------------------------------------
    step(9, f"Settlement via {settlement_model}")
    if settlement_model == "immediate":
        result = ImmediatePayment(ledger).settle(env)
    elif settlement_model == "escrow":
        result = escrow.settle(env)
    else:
        result = phased.settle(env)
    print(f"  released     : ${result.released}")
    print(f"  refunded     : ${result.refunded}")
    print(f"  success      : {result.success}")
    print(f"  notes        : {result.notes}")
    print(f"  balances     : Orion=${ledger.balance(orion_id):.2f}  "
          f"Vega=${ledger.balance(vega_id):.2f}")

    # --- audit trail ------------------------------------------------------
    step(10, "Tamper-evident audit trail")
    for entry in env.audit_trail:
        print(f"  • {entry.event}")
    print(f"  audit_digest : {env.audit_digest()[:32]}…")


if __name__ == "__main__":
    for model in ("immediate", "escrow", "phased"):
        run_flow(model)
    print("\nAll three settlement flows completed successfully.\n")
