"""Test suite for the AETHER protocol reference implementation."""

import time

from aether import (
    crypto,
    CapabilityManifest,
    PriceSchedule,
    DiscoveryQuery,
    CapabilityResponse,
    respond_to_query,
    SettlementOffer,
    AcceptanceReceipt,
    envelope_from_deal,
    Ledger,
    ImmediatePayment,
    EscrowSettlement,
    PhasedSettlement,
)


# -- crypto ---------------------------------------------------------------

def test_keypair_and_signature_roundtrip():
    priv, pub = crypto.generate_keypair()
    assert crypto.public_key_from_private(priv) == pub
    payload = {"b": 2, "a": 1}
    sig = crypto.sign(priv, payload)
    assert crypto.verify(pub, payload, sig)


def test_verify_rejects_tampered_payload():
    priv, pub = crypto.generate_keypair()
    sig = crypto.sign(priv, {"amount": 10})
    assert not crypto.verify(pub, {"amount": 11}, sig)


def test_verify_never_raises_on_garbage():
    assert crypto.verify("zz", {"x": 1}, "notasig") is False


def test_canonical_bytes_is_order_independent():
    assert crypto.canonical_bytes({"a": 1, "b": 2}) == crypto.canonical_bytes({"b": 2, "a": 1})


# -- manifest -------------------------------------------------------------

def _manifest(priv, pub):
    return CapabilityManifest(
        agent_id=pub,
        display_name="Provider",
        task_types=["market_research"],
        pricing=[PriceSchedule("market_research", 25.0)],
        reputation=0.8,
    ).sign(priv)


def test_manifest_sign_verify_and_roundtrip():
    priv, pub = crypto.generate_keypair()
    m = _manifest(priv, pub)
    assert m.verify()
    m2 = CapabilityManifest.from_dict(m.to_dict())
    assert m2.verify()
    assert m2.handles("market_research")
    assert m2.price_for("market_research").amount == 25.0
    assert m2.price_for("nope") is None


def test_manifest_sign_wrong_key_rejected():
    priv, pub = crypto.generate_keypair()
    other_priv, _ = crypto.generate_keypair()
    m = CapabilityManifest(agent_id=pub, display_name="x", task_types=["t"])
    try:
        m.sign(other_priv)
        assert False, "should have raised"
    except ValueError:
        pass


def test_manifest_tamper_detected():
    priv, pub = crypto.generate_keypair()
    m = _manifest(priv, pub)
    m.reputation = 0.99  # tamper after signing
    assert not m.verify()


# -- handshake ------------------------------------------------------------

def test_respond_to_query_matches_and_filters():
    priv, pub = crypto.generate_keypair()
    m = _manifest(priv, pub)
    _, req_id = crypto.generate_keypair()

    ok = respond_to_query(DiscoveryQuery("market_research", req_id, max_price=40), m)
    assert ok is not None and ok.is_valid() and ok.quoted_price == 25.0

    too_cheap = respond_to_query(DiscoveryQuery("market_research", req_id, max_price=10), m)
    assert too_cheap is None

    wrong = respond_to_query(DiscoveryQuery("translation", req_id), m)
    assert wrong is None


def test_offer_and_receipt_sign_verify_roundtrip():
    req_priv, req_id = crypto.generate_keypair()
    prov_priv, prov_id = crypto.generate_keypair()
    offer = SettlementOffer(
        query_id="q", requester_id=req_id, provider_id=prov_id,
        task_type="market_research", price=25.0, currency="USD",
        deadline=time.time() + 60, settlement_type="immediate",
        acceptance_criteria="x",
    ).sign(req_priv)
    assert offer.verify()
    assert SettlementOffer.from_dict(offer.to_dict()).verify()

    receipt = AcceptanceReceipt(offer.offer_id, prov_id, req_id).sign(prov_priv)
    assert receipt.verify()
    assert AcceptanceReceipt.from_dict(receipt.to_dict()).verify()


# -- envelope + settlement helpers ---------------------------------------

def _full_deal(settlement_type):
    req_priv, req_id = crypto.generate_keypair()
    prov_priv, prov_id = crypto.generate_keypair()
    offer = SettlementOffer(
        query_id="q", requester_id=req_id, provider_id=prov_id,
        task_type="market_research", price=20.0, currency="USD",
        deadline=time.time() + 60, settlement_type=settlement_type,
        acceptance_criteria="ok",
    ).sign(req_priv)
    receipt = AcceptanceReceipt(offer.offer_id, prov_id, req_id).sign(prov_priv)
    env = envelope_from_deal(offer, receipt, payload={"topic": "t"})
    env.set_verifier(lambda r: r == "good")
    return req_id, prov_id, env


def test_envelope_lifecycle_and_audit():
    _, _, env = _full_deal("immediate")
    assert env.status == "created"
    env.deliver("good")
    assert env.status == "delivered"
    assert env.verify_delivery() is True
    assert env.status == "verified"
    events = [e.event for e in env.audit_trail]
    assert events[:3] == ["created", "delivered", "verified"]
    assert len(env.audit_digest()) == 64


def test_envelope_rejection():
    _, _, env = _full_deal("immediate")
    env.deliver("bad")
    assert env.verify_delivery() is False
    assert env.status == "rejected"


def test_immediate_payment():
    req, prov, env = _full_deal("immediate")
    ledger = Ledger({req: 100.0})
    env.deliver("good"); env.verify_delivery()
    r = ImmediatePayment(ledger).settle(env)
    assert r.success and r.released == 20.0
    assert ledger.balance(prov) == 20.0 and ledger.balance(req) == 80.0


def test_escrow_release_and_refund():
    # release on success
    req, prov, env = _full_deal("escrow")
    ledger = Ledger({req: 100.0})
    esc = EscrowSettlement(ledger)
    esc.lock(env)
    assert ledger.balance(req) == 80.0
    env.deliver("good"); env.verify_delivery()
    r = esc.settle(env)
    assert r.success and ledger.balance(prov) == 20.0

    # refund on rejection
    req2, prov2, env2 = _full_deal("escrow")
    ledger2 = Ledger({req2: 100.0})
    esc2 = EscrowSettlement(ledger2)
    esc2.lock(env2)
    env2.deliver("bad"); env2.verify_delivery()
    r2 = esc2.settle(env2)
    assert not r2.success and r2.refunded == 20.0
    assert ledger2.balance(req2) == 100.0 and ledger2.balance(prov2) == 0.0


def test_phased_reputation_weighted():
    req, prov, env = _full_deal("phased")
    ledger = Ledger({req: 100.0})
    phased = PhasedSettlement(ledger, reputation=1.0)  # max upfront
    up = phased.release_upfront(env)
    assert up == 20.0 * phased.upfront_fraction
    env.deliver("good"); env.verify_delivery()
    r = phased.settle(env)
    assert r.success and round(r.released, 6) == 20.0
    assert round(ledger.balance(prov), 6) == 20.0


def test_ledger_insufficient_funds():
    ledger = Ledger({"a": 5.0})
    try:
        ledger.transfer("a", "b", 10.0)
        assert False
    except ValueError:
        pass


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed.")
    sys.exit(0)
