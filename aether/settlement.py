"""
AETHER Settlement Primitives
============================

Three interchangeable ways to move funds once a Task Envelope reaches a
terminal state. Each primitive implements the same ``settle(envelope)``
contract and returns a ``SettlementResult``, so callers can swap models
without changing their flow.

    * ImmediatePayment      — pay in full the moment delivery verifies.
    * EscrowSettlement      — funds are locked up front, released on verify,
                              refunded on rejection.
    * PhasedSettlement      — reputation-weighted staged release: a trusted
                              provider gets more up front, the rest on verify.

These are ledger-agnostic: the "balances" are an in-memory dict so the
reference implementation runs anywhere. Swap ``Ledger`` for a real payment
rail (chain, bank API, stablecoin) without touching the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .envelope import TaskEnvelope
from .dispute import DisputeClaim, DisputeResolution


class Ledger:
    """A trivial in-memory balance sheet used by the settlement primitives."""

    def __init__(self, balances: Dict[str, float] | None = None) -> None:
        self.balances: Dict[str, float] = dict(balances or {})

    def balance(self, account: str) -> float:
        return self.balances.get(account, 0.0)

    def transfer(self, src: str, dst: str, amount: float) -> None:
        if amount < 0:
            raise ValueError("amount must be non-negative")
        if self.balances.get(src, 0.0) < amount:
            raise ValueError(f"insufficient funds in {src}")
        self.balances[src] = self.balances.get(src, 0.0) - amount
        self.balances[dst] = self.balances.get(dst, 0.0) + amount


@dataclass
class SettlementResult:
    """Outcome of a settlement attempt."""

    transaction_id: str
    model: str
    released: float
    refunded: float
    success: bool
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "model": self.model,
            "released": self.released,
            "refunded": self.refunded,
            "success": self.success,
            "notes": self.notes,
        }


class ImmediatePayment:
    """Micro-payment released in full on successful verification."""

    model = "immediate"

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def settle(self, env: TaskEnvelope) -> SettlementResult:
        ok = env.status == "verified"
        if ok:
            self.ledger.transfer(env.requester_id, env.provider_id, env.price)
            env._log("settled_immediate", {"amount": env.price})
        return SettlementResult(
            transaction_id=env.transaction_id,
            model=self.model,
            released=env.price if ok else 0.0,
            refunded=0.0,
            success=ok,
            notes=["paid in full" if ok else f"not verified (status={env.status})"],
        )


# Explicit escrow states (v0.2 dispute state machine):
#   LOCKED --deliver--> DELIVERED --(window, no claim)--> RELEASED
#      |                    |
#      |                    +--DisputeClaim--> DISPUTED --DisputeResolution--> RESOLVED
#      +--DisputeClaim (non_delivery)--> DISPUTED
LOCKED = "LOCKED"
DELIVERED = "DELIVERED"
DISPUTED = "DISPUTED"
RESOLVED = "RESOLVED"
RELEASED = "RELEASED"

# States from which a dispute may still be opened.
_DISPUTABLE = frozenset({LOCKED, DELIVERED})


@dataclass
class _EscrowRecord:
    """Per-escrow bookkeeping so the settlement can enforce the state machine."""

    transaction_id: str
    requester_id: str
    provider_id: str
    amount: float
    state: str = LOCKED
    arbiter: str = ""              # agreed in the signed offer; "" == no-arbitration
    dispute_window_secs: float = 0.0
    claim: Optional[DisputeClaim] = None


class EscrowSettlement:
    """Funds locked at open, released on verify, refunded on rejection.

    v0.2 adds explicit per-escrow state tracking plus a pluggable dispute flow
    (:meth:`open_dispute` / :meth:`resolve_dispute`). The v0.1 happy path
    (``lock()`` then ``settle()``) is unchanged and fully backward compatible.
    """

    model = "escrow"

    def __init__(self, ledger: Ledger, escrow_account: str = "escrow") -> None:
        self.ledger = ledger
        self.escrow_account = escrow_account
        # Keyed by envelope transaction_id (== dispute envelope_id).
        self._records: Dict[str, _EscrowRecord] = {}

    # -- state helpers -----------------------------------------------------

    def state(self, envelope_id: str) -> Optional[str]:
        """Return the current escrow state for an envelope, or None if unknown."""
        rec = self._records.get(envelope_id)
        return rec.state if rec else None

    # -- happy path (v0.1 compatible) --------------------------------------

    def lock(self, env: TaskEnvelope, offer=None) -> None:
        """Move the price from requester into escrow before work begins.

        Pass the signed ``offer`` to enable disputes: it carries the agreed
        ``arbiter`` and ``dispute_window_secs``. Omitting it preserves exact
        v0.1 behaviour (a no-arbitration escrow).
        """
        self.ledger.transfer(env.requester_id, self.escrow_account, env.price)
        env._log("escrow_locked", {"amount": env.price})
        self._records[env.transaction_id] = _EscrowRecord(
            transaction_id=env.transaction_id,
            requester_id=env.requester_id,
            provider_id=env.provider_id,
            amount=env.price,
            state=LOCKED,
            arbiter=getattr(offer, "arbiter", "") or "",
            dispute_window_secs=float(getattr(offer, "dispute_window_secs", 0.0) or 0.0),
        )

    def settle(self, env: TaskEnvelope) -> SettlementResult:
        rec = self._records.get(env.transaction_id)
        # Funds cannot leave escrow while a dispute is open or resolved here.
        if rec and rec.state in (DISPUTED, RESOLVED):
            raise ValueError(
                f"cannot settle: escrow is {rec.state}; use resolve_dispute()"
            )
        if env.status == "verified":
            self.ledger.transfer(self.escrow_account, env.provider_id, env.price)
            env._log("escrow_released", {"amount": env.price})
            if rec:
                rec.state = RELEASED
            return SettlementResult(
                env.transaction_id, self.model, env.price, 0.0, True,
                ["escrow released to provider"],
            )
        # rejected / not delivered → refund requester
        self.ledger.transfer(self.escrow_account, env.requester_id, env.price)
        env._log("escrow_refunded", {"amount": env.price})
        if rec:
            rec.state = RELEASED
        return SettlementResult(
            env.transaction_id, self.model, 0.0, env.price, False,
            [f"escrow refunded (status={env.status})"],
        )

    # -- dispute flow (v0.2) -----------------------------------------------

    def open_dispute(self, claim: DisputeClaim, env: Optional[TaskEnvelope] = None) -> _EscrowRecord:
        """Register a signed dispute, moving the escrow into DISPUTED.

        Validates the claim signature, that the escrow exists and is in a
        disputable state, that the job actually agreed an arbiter, and that the
        claimant is one of the two counterparties. Prevents fund release while
        disputed (see :meth:`settle`).
        """
        if not claim.verify():
            raise ValueError("dispute claim signature does not verify")
        rec = self._records.get(claim.envelope_id)
        if rec is None:
            raise ValueError(f"unknown escrow envelope: {claim.envelope_id}")
        if not rec.arbiter:
            raise ValueError("no arbiter agreed in the offer; disputes unavailable")
        if rec.state not in _DISPUTABLE:
            raise ValueError(f"escrow not disputable in state {rec.state}")
        parties = {rec.requester_id, rec.provider_id}
        if claim.claimant not in parties:
            raise ValueError("claimant is not a party to this escrow")
        rec.state = DISPUTED
        rec.claim = claim
        if env is not None:
            env._log("dispute_opened", {
                "dispute_id": claim.dispute_id,
                "reason": claim.reason,
                "claimant": claim.claimant,
            })
        return rec

    def resolve_dispute(
        self, resolution: DisputeResolution, env: Optional[TaskEnvelope] = None
    ) -> SettlementResult:
        """Apply the arbiter's signed verdict, splitting locked funds.

        Only the ``arbiter`` named in the signed offer may resolve. Moves
        DISPUTED → RESOLVED and moves funds out of escrow via ``Ledger.transfer``.
        """
        if not resolution.verify():
            raise ValueError("resolution signature does not verify")
        rec = self._records.get(resolution.envelope_id)
        if rec is None:
            raise ValueError(f"unknown escrow envelope: {resolution.envelope_id}")
        if rec.state != DISPUTED:
            raise ValueError(f"escrow not in DISPUTED state (state={rec.state})")
        # Authorization: the signer MUST be the arbiter agreed in the offer.
        if resolution.arbiter != rec.arbiter:
            raise PermissionError(
                "resolution not signed by the agreed arbiter"
            )

        amount = rec.amount
        if resolution.verdict == "release_to_provider":
            released, refunded = amount, 0.0
            self.ledger.transfer(self.escrow_account, rec.provider_id, amount)
        elif resolution.verdict == "refund_to_requester":
            released, refunded = 0.0, amount
            self.ledger.transfer(self.escrow_account, rec.requester_id, amount)
        elif resolution.verdict == "split":
            if not (0 <= resolution.split_bps <= 10000):
                raise ValueError("split_bps must be within [0, 10000]")
            released = round(amount * resolution.split_bps / 10000, 6)
            refunded = round(amount - released, 6)
            if released:
                self.ledger.transfer(self.escrow_account, rec.provider_id, released)
            if refunded:
                self.ledger.transfer(self.escrow_account, rec.requester_id, refunded)
        else:  # pragma: no cover - guarded by DisputeResolution.sign()
            raise ValueError(f"invalid verdict: {resolution.verdict}")

        rec.state = RESOLVED
        if env is not None:
            env._log("dispute_resolved", {
                "dispute_id": resolution.dispute_id,
                "verdict": resolution.verdict,
                "released": released,
                "refunded": refunded,
            })
        return SettlementResult(
            rec.transaction_id, self.model, released, refunded, True,
            [f"dispute resolved: {resolution.verdict}"],
        )


class PhasedSettlement:
    """Reputation-weighted phased release.

    An up-front fraction proportional to provider reputation is released when
    work begins; the remainder is released on verification. Higher reputation
    ⇒ more paid up front (less counterparty risk for a trusted provider).
    """

    model = "phased"

    def __init__(
        self, ledger: Ledger, reputation: float, min_upfront: float = 0.1,
        max_upfront: float = 0.6,
    ) -> None:
        self.ledger = ledger
        # clamp reputation into [0,1] then map onto the up-front band.
        rep = max(0.0, min(1.0, reputation))
        self.upfront_fraction = min_upfront + (max_upfront - min_upfront) * rep
        self._released = 0.0

    def release_upfront(self, env: TaskEnvelope) -> float:
        """Release the reputation-weighted up-front phase; returns amount."""
        amount = round(env.price * self.upfront_fraction, 6)
        self.ledger.transfer(env.requester_id, env.provider_id, amount)
        self._released += amount
        env._log("phase_upfront", {"amount": amount, "fraction": self.upfront_fraction})
        return amount

    def settle(self, env: TaskEnvelope) -> SettlementResult:
        remainder = round(env.price - self._released, 6)
        if env.status == "verified":
            self.ledger.transfer(env.requester_id, env.provider_id, remainder)
            env._log("phase_final", {"amount": remainder})
            return SettlementResult(
                env.transaction_id, self.model, round(self._released + remainder, 6),
                0.0, True, [f"upfront {self.upfront_fraction:.0%}, remainder on verify"],
            )
        return SettlementResult(
            env.transaction_id, self.model, self._released, 0.0, False,
            [f"only upfront released (status={env.status})"],
        )
