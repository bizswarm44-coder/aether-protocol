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
from typing import Dict, List

from .envelope import TaskEnvelope


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


class EscrowSettlement:
    """Funds locked at open, released on verify, refunded on rejection."""

    model = "escrow"

    def __init__(self, ledger: Ledger, escrow_account: str = "escrow") -> None:
        self.ledger = ledger
        self.escrow_account = escrow_account

    def lock(self, env: TaskEnvelope) -> None:
        """Move the price from requester into escrow before work begins."""
        self.ledger.transfer(env.requester_id, self.escrow_account, env.price)
        env._log("escrow_locked", {"amount": env.price})

    def settle(self, env: TaskEnvelope) -> SettlementResult:
        if env.status == "verified":
            self.ledger.transfer(self.escrow_account, env.provider_id, env.price)
            env._log("escrow_released", {"amount": env.price})
            return SettlementResult(
                env.transaction_id, self.model, env.price, 0.0, True,
                ["escrow released to provider"],
            )
        # rejected / not delivered → refund requester
        self.ledger.transfer(self.escrow_account, env.requester_id, env.price)
        env._log("escrow_refunded", {"amount": env.price})
        return SettlementResult(
            env.transaction_id, self.model, 0.0, env.price, False,
            [f"escrow refunded (status={env.status})"],
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
