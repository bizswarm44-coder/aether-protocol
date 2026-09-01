"""
Tests for AETHER v0.2 Extension 1 — Escrow Dispute Resolution.

Covers the four scenarios called out in SPEC_v0.2.md plus the state-machine
guarantees:

    * non-delivery  -> refund_to_requester
    * quality       -> split
    * bad-faith non_payment -> release_to_provider
    * a resolution signed by anyone other than the agreed arbiter is REJECTED
    * funds cannot be released while DISPUTED
"""

import time

import pytest

from aether import (
    crypto,
    SettlementOffer,
    AcceptanceReceipt,
    envelope_from_deal,
    Ledger,
    EscrowSettlement,
    DisputeClaim,
    DisputeResolution,
    evidence_hash,
    rationale_hash,
)
from aether.settlement import LOCKED, DISPUTED, RESOLVED, RELEASED


def _escrow_deal(price=20.0, dispute_window_secs=3600.0):
    """Build a signed escrow deal that agrees on an arbiter, returning the
    keys, envelope, offer, and a locked EscrowSettlement ready to dispute."""
    req_priv, req_id = crypto.generate_keypair()
    prov_priv, prov_id = crypto.generate_keypair()
    arb_priv, arb_id = crypto.generate_keypair()

    offer = SettlementOffer(
        query_id="q", requester_id=req_id, provider_id=prov_id,
        task_type="market_research", price=price, currency="USD",
        deadline=time.time() + 60, settlement_type="escrow",
        acceptance_criteria="deliver a 5-point summary",
        arbiter=arb_id, dispute_window_secs=dispute_window_secs,
    ).sign(req_priv)
    receipt = AcceptanceReceipt(offer.offer_id, prov_id, req_id).sign(prov_priv)
    env = envelope_from_deal(offer, receipt, payload={"topic": "t"})

    ledger = Ledger({req_id: 100.0})
    esc = EscrowSettlement(ledger)
    esc.lock(env, offer)
    return {
        "req_priv": req_priv, "req_id": req_id,
        "prov_priv": prov_priv, "prov_id": prov_id,
        "arb_priv": arb_priv, "arb_id": arb_id,
        "offer": offer, "env": env, "ledger": ledger, "esc": esc,
    }


# -- message round-trips ---------------------------------------------------

def test_dispute_messages_sign_verify_and_roundtrip():
    d = _escrow_deal()
    claim = DisputeClaim(
        envelope_id=d["env"].transaction_id,
        claimant=d["req_id"], respondent=d["prov_id"],
        reason="quality", evidence_hash=evidence_hash(b"logs"),
        requested_outcome="split",
    ).sign(d["req_priv"])
    assert claim.verify()
    assert DisputeClaim.from_dict(claim.to_dict()).verify()

    res = DisputeResolution(
        dispute_id=claim.dispute_id, envelope_id=claim.envelope_id,
        arbiter=d["arb_id"], verdict="split", split_bps=5000,
        rationale_hash=rationale_hash("both partially right"),
    ).sign(d["arb_priv"])
    assert res.verify()
    assert DisputeResolution.from_dict(res.to_dict()).verify()


def test_offer_arbiter_fields_are_signed_and_roundtrip():
    """The arbiter/window live inside the signed offer (both parties agree)."""
    d = _escrow_deal(dispute_window_secs=1234.0)
    offer = d["offer"]
    assert offer.verify()
    ro = SettlementOffer.from_dict(offer.to_dict())
    assert ro.arbiter == d["arb_id"]
    assert ro.dispute_window_secs == 1234.0
    assert ro.verify()
    # Tampering with the agreed arbiter must break the signature.
    offer.arbiter = crypto.generate_keypair()[1]
    assert not offer.verify()


# -- the four spec scenarios ----------------------------------------------

def test_non_delivery_refund():
    d = _escrow_deal()
    esc, env, ledger = d["esc"], d["env"], d["esc"].ledger
    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=d["req_id"],
        respondent=d["prov_id"], reason="non_delivery",
        evidence_hash=evidence_hash(b"no output received"),
        requested_outcome="refund",
    ).sign(d["req_priv"])
    esc.open_dispute(claim, env)
    assert esc.state(env.transaction_id) == DISPUTED

    res = DisputeResolution(
        dispute_id=claim.dispute_id, envelope_id=env.transaction_id,
        arbiter=d["arb_id"], verdict="refund_to_requester",
        rationale_hash=rationale_hash("provider never delivered"),
    ).sign(d["arb_priv"])
    result = esc.resolve_dispute(res, env)

    assert result.success and result.refunded == 20.0 and result.released == 0.0
    assert esc.state(env.transaction_id) == RESOLVED
    assert ledger.balance(d["req_id"]) == 100.0
    assert ledger.balance(d["prov_id"]) == 0.0
    assert ledger.balance(esc.escrow_account) == 0.0


def test_quality_split():
    d = _escrow_deal(price=20.0)
    esc, env, ledger = d["esc"], d["env"], d["esc"].ledger
    env.set_verifier(lambda r: True)
    env.deliver("partial work")  # DELIVERED-ish; still disputable
    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=d["req_id"],
        respondent=d["prov_id"], reason="quality",
        evidence_hash=evidence_hash(b"only 3 of 5 points"),
        requested_outcome="split",
    ).sign(d["req_priv"])
    esc.open_dispute(claim, env)

    res = DisputeResolution(
        dispute_id=claim.dispute_id, envelope_id=env.transaction_id,
        arbiter=d["arb_id"], verdict="split", split_bps=6000,  # 60% to provider
        rationale_hash=rationale_hash("mostly delivered"),
    ).sign(d["arb_priv"])
    result = esc.resolve_dispute(res, env)

    assert result.success
    assert result.released == 12.0 and result.refunded == 8.0
    assert ledger.balance(d["prov_id"]) == 12.0
    assert ledger.balance(d["req_id"]) == 80.0 + 8.0
    assert ledger.balance(esc.escrow_account) == 0.0


def test_bad_faith_non_payment_release_to_provider():
    """Requester refuses to accept good work; arbiter releases to provider."""
    d = _escrow_deal()
    esc, env, ledger = d["esc"], d["env"], d["esc"].ledger
    # Provider raises the claim (they are owed payment).
    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=d["prov_id"],
        respondent=d["req_id"], reason="non_payment",
        evidence_hash=evidence_hash(b"delivered + verified output"),
        requested_outcome="release",
    ).sign(d["prov_priv"])
    esc.open_dispute(claim, env)

    res = DisputeResolution(
        dispute_id=claim.dispute_id, envelope_id=env.transaction_id,
        arbiter=d["arb_id"], verdict="release_to_provider",
        rationale_hash=rationale_hash("work was good; requester stalling"),
    ).sign(d["arb_priv"])
    result = esc.resolve_dispute(res, env)

    assert result.success and result.released == 20.0 and result.refunded == 0.0
    assert ledger.balance(d["prov_id"]) == 20.0
    assert ledger.balance(d["req_id"]) == 80.0
    assert ledger.balance(esc.escrow_account) == 0.0


def test_only_agreed_arbiter_can_resolve():
    d = _escrow_deal()
    esc, env = d["esc"], d["env"]
    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=d["req_id"],
        respondent=d["prov_id"], reason="quality",
        evidence_hash=evidence_hash(b"e"), requested_outcome="split",
    ).sign(d["req_priv"])
    esc.open_dispute(claim, env)

    # An impostor arbiter signs a (self-consistent, verifiable) resolution.
    imp_priv, imp_id = crypto.generate_keypair()
    rogue = DisputeResolution(
        dispute_id=claim.dispute_id, envelope_id=env.transaction_id,
        arbiter=imp_id, verdict="release_to_provider",
        rationale_hash=rationale_hash("i say so"),
    ).sign(imp_priv)
    assert rogue.verify()  # signature is valid...
    with pytest.raises(PermissionError):
        esc.resolve_dispute(rogue, env)  # ...but not the AGREED arbiter

    # Escrow stays DISPUTED and funds remain locked.
    assert esc.state(env.transaction_id) == DISPUTED
    assert esc.ledger.balance(esc.escrow_account) == 20.0


def test_funds_cannot_be_released_while_disputed():
    d = _escrow_deal()
    esc, env = d["esc"], d["env"]
    env.set_verifier(lambda r: True)
    env.deliver("good"); env.verify_delivery()  # would normally release

    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=d["req_id"],
        respondent=d["prov_id"], reason="quality",
        evidence_hash=evidence_hash(b"e"), requested_outcome="split",
    ).sign(d["req_priv"])
    esc.open_dispute(claim, env)
    assert esc.state(env.transaction_id) == DISPUTED

    # settle() must refuse while disputed; funds stay in escrow.
    with pytest.raises(ValueError):
        esc.settle(env)
    assert esc.ledger.balance(esc.escrow_account) == 20.0
    assert esc.ledger.balance(d["prov_id"]) == 0.0


# -- guards ----------------------------------------------------------------

def test_open_dispute_rejects_bad_signature():
    d = _escrow_deal()
    claim = DisputeClaim(
        envelope_id=d["env"].transaction_id, claimant=d["req_id"],
        respondent=d["prov_id"], reason="quality",
        evidence_hash=evidence_hash(b"e"), requested_outcome="split",
    ).sign(d["req_priv"])
    claim.signature = "00" * 32  # corrupt it
    with pytest.raises(ValueError):
        d["esc"].open_dispute(claim, d["env"])


def test_no_arbiter_means_disputes_unavailable():
    """A v0.1-style escrow locked without an offer cannot be disputed."""
    req_priv, req_id = crypto.generate_keypair()
    prov_priv, prov_id = crypto.generate_keypair()
    offer = SettlementOffer(
        query_id="q", requester_id=req_id, provider_id=prov_id,
        task_type="market_research", price=10.0, currency="USD",
        deadline=time.time() + 60, settlement_type="escrow",
        acceptance_criteria="x",
    ).sign(req_priv)  # no arbiter agreed
    receipt = AcceptanceReceipt(offer.offer_id, prov_id, req_id).sign(prov_priv)
    env = envelope_from_deal(offer, receipt, payload={})
    esc = EscrowSettlement(Ledger({req_id: 50.0}))
    esc.lock(env)  # v0.1 call — no offer passed
    assert esc.state(env.transaction_id) == LOCKED

    claim = DisputeClaim(
        envelope_id=env.transaction_id, claimant=req_id, respondent=prov_id,
        reason="quality", evidence_hash=evidence_hash(b"e"),
        requested_outcome="split",
    ).sign(req_priv)
    with pytest.raises(ValueError):
        esc.open_dispute(claim, env)


def test_happy_path_still_works_and_reaches_released():
    """v0.1 escrow happy path is unchanged and now records RELEASED state."""
    d = _escrow_deal()
    esc, env, ledger = d["esc"], d["env"], d["esc"].ledger
    env.set_verifier(lambda r: r == "good")
    env.deliver("good"); env.verify_delivery()
    r = esc.settle(env)
    assert r.success and r.released == 20.0
    assert ledger.balance(d["prov_id"]) == 20.0
    assert esc.state(env.transaction_id) == RELEASED
