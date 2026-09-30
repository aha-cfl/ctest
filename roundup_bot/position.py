"""Take-profit ladder (+50/100/150/200% ROE, then every +step) with a ratcheting stop."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import LadderConfig


@dataclass
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Fill:
    qty: float        # base units closed (positive)
    price: float
    reason: str       # "tp@50%", "stop@-40%", "signal-exit", ...


@dataclass
class Position:
    side: int                     # +1 long, -1 short
    entry: float
    qty: float                    # remaining base units
    leverage: float
    cfg: LadderConfig
    initial_qty: float = 0.0
    next_level: int = 0           # index into the (infinite) level sequence
    stop_roe: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self):
        self.initial_qty = self.initial_qty or self.qty
        self.stop_roe = self.cfg.stop_loss_roe_pct

    def level_roe(self, i: int) -> float:
        lv = self.cfg.levels_pct
        return lv[i] if i < len(lv) else lv[-1] + (i - len(lv) + 1) * self.cfg.step_after_pct

    def level_fraction(self, i: int) -> float:
        return self.cfg.close_fractions[i] if i < len(self.cfg.close_fractions) else 0.0

    def price_at_roe(self, roe_pct: float) -> float:
        return self.entry * (1 + self.side * roe_pct / 100.0 / self.leverage)

    def roe_at(self, price: float) -> float:
        return (price / self.entry - 1) * self.side * self.leverage * 100.0

    @property
    def stop_price(self) -> float:
        return self.price_at_roe(self.stop_roe)

    def _close(self, qty: float, price: float, reason: str) -> Fill:
        qty = min(qty, self.qty)
        self.qty -= qty
        if self.qty < 1e-12:
            self.qty = 0.0
        f = Fill(qty, price, reason)
        self.fills.append(f)
        return f

    def on_bar(self, bar: Bar) -> list[Fill]:
        """Process one OHLC bar. Stop is checked first (pessimistic intrabar ordering)."""
        out: list[Fill] = []
        if self.qty == 0:
            return out

        adverse = bar.low if self.side > 0 else bar.high
        favorable = bar.high if self.side > 0 else bar.low
        stop = self.stop_price
        if (adverse - stop) * self.side <= 0:
            # Gap through the stop fills at the open, not the stop.
            gapped = (bar.open - stop) * self.side < 0
            px = bar.open if gapped else stop
            out.append(self._close(self.qty, px, f"stop@{self.stop_roe:g}%"))
            return out

        while self.qty > 0:
            target_roe = self.level_roe(self.next_level)
            target_px = self.price_at_roe(target_roe)
            if (favorable - target_px) * self.side < 0:
                break
            frac = self.level_fraction(self.next_level)
            if frac > 0:
                out.append(self._close(self.initial_qty * frac, target_px, f"tp@{target_roe:g}%"))
            if self.cfg.ratchet_stop:
                self.stop_roe = self.level_roe(self.next_level - 1) if self.next_level > 0 else 0.0
            self.next_level += 1
        return out

    def exit_all(self, price: float, reason: str) -> list[Fill]:
        return [self._close(self.qty, price, reason)] if self.qty > 0 else []
