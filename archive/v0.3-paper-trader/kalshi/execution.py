"""Order execution. Paper only: fills are simulated at the quoted ask, no orders reach Kalshi."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from .model import taker_fee_per_contract


@dataclass
class Fill:
    order_id: str
    side: str          # "yes" | "no"
    count: int
    price: float       # per contract, dollars
    fee: float         # total dollars

    @property
    def cost(self) -> float:
        return self.count * self.price + self.fee


class PaperExecutor:
    """Fills at the quoted ask, capped by displayed depth. No network calls."""

    def buy(self, ticker: str, side: str, price: float, count: int, depth: float) -> Fill | None:
        n = int(min(count, depth))
        if n < 1:
            return None
        return Fill(f"paper-{uuid.uuid4().hex[:10]}", side, n, price, taker_fee_per_contract(price, n) * n)
