"""Execution backends. PaperBroker is the default; CcxtBroker places real orders."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

LIVE_ACK_ENV = "ROUNDUP_BOT_LIVE"
LIVE_ACK_VALUE = "I_ACCEPT_TOTAL_LOSS"


class Broker(ABC):
    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def deposit(self, usd: float) -> None: ...

    @abstractmethod
    def open(self, side: int, qty: float, price: float, leverage: float) -> float:
        """Open at market. Returns fill price."""

    @abstractmethod
    def close(self, side: int, qty: float, price: float) -> float:
        """Reduce-only close. Returns fill price."""


class PaperBroker(Broker):
    """Isolated-margin simulator: tracks cash + margin, charges taker fees."""

    def __init__(self, cash: float = 0.0, fee_rate: float = 0.0005):
        self.cash = cash
        self.fee_rate = fee_rate
        self.fees_paid = 0.0
        self._entry = 0.0
        self._qty = 0.0
        self._side = 0
        self._margin = 0.0

    def equity(self) -> float:
        return self.cash + self._margin

    def deposit(self, usd: float) -> None:
        self.cash += usd

    def _fee(self, notional: float) -> None:
        fee = notional * self.fee_rate
        self.cash -= fee
        self.fees_paid += fee

    def open(self, side, qty, price, leverage):
        if self._qty:
            raise RuntimeError("paper broker supports one position at a time")
        margin = qty * price / leverage
        if margin > self.cash:
            raise RuntimeError(f"insufficient cash {self.cash:.2f} for margin {margin:.2f}")
        self.cash -= margin
        self._margin, self._entry, self._qty, self._side = margin, price, qty, side
        self._fee(qty * price)
        return price

    def close(self, side, qty, price):
        qty = min(qty, self._qty)
        frac = qty / self._qty
        released = self._margin * frac
        pnl = (price - self._entry) * qty * self._side
        # Isolated margin: loss on this slice can't exceed the margin backing it.
        self.cash += max(released + pnl, 0.0)
        self._margin -= released
        self._qty -= qty
        if self._qty < 1e-12:
            self._qty, self._margin, self._side = 0.0, 0.0, 0
        self._fee(qty * price)
        return price


class CcxtBroker(Broker):
    """Live perpetual-futures execution via ccxt. Requires explicit env acknowledgement."""

    def __init__(self, exchange: str, symbol: str):
        if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
            raise RuntimeError(f"Live trading blocked. Set {LIVE_ACK_ENV}={LIVE_ACK_VALUE} to enable.")
        import ccxt  # lazy: only needed live

        self.symbol = symbol
        self.ex = getattr(ccxt, exchange)({
            "apiKey": os.environ["EXCHANGE_API_KEY"],
            "secret": os.environ["EXCHANGE_API_SECRET"],
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        self.ex.load_markets()

    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        quote = self.ex.market(self.symbol)["settle"]
        return float(bal["total"].get(quote, 0.0))

    def deposit(self, usd: float) -> None:
        print(f"[live] funding is external; expecting ${usd:.2f} to arrive on the exchange")

    def open(self, side, qty, price, leverage):
        self.ex.set_margin_mode("isolated", self.symbol)
        self.ex.set_leverage(int(leverage), self.symbol)
        qty = float(self.ex.amount_to_precision(self.symbol, qty))
        o = self.ex.create_order(self.symbol, "market", "buy" if side > 0 else "sell", qty)
        return float(o.get("average") or price)

    def close(self, side, qty, price):
        qty = float(self.ex.amount_to_precision(self.symbol, qty))
        o = self.ex.create_order(self.symbol, "market", "sell" if side > 0 else "buy", qty,
                                 params={"reduceOnly": True})
        return float(o.get("average") or price)
