"""Position sizing and loss limits: quarter-Kelly, $ cap per trade, daily loss stop, kill switch.

State lives in <data>/risk.json so limits survive restarts. The same RiskBook is what any future
live executor must consult before every order.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .model import taker_fee_per_contract


@dataclass
class RiskConfig:
    bankroll: float = 100.0          # starting paper bankroll, dollars
    kelly_fraction: float = 0.25
    per_trade_cap: float = 5.0       # max dollars at risk per trade (price + fee) x count
    daily_loss_cap: float = 25.0     # stop opening trades once today's realized P&L <= -this


def kelly_fraction(p_win: float, price: float, fee: float) -> float:
    """Full-Kelly bankroll fraction for a $1 binary bought at price+fee. <= 0 means no bet."""
    cost = price + fee
    if not 0 < cost < 1:
        return 0.0
    b = (1 - cost) / cost                  # net odds received on a win
    return (p_win * b - (1 - p_win)) / b


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class RiskBook:
    def __init__(self, data_dir: Path, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()
        self.path = Path(data_dir) / "risk.json"
        self.kill_path = Path(data_dir) / "KILL"
        st = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.bankroll = st.get("bankroll", self.cfg.bankroll)
        self.day = st.get("day", "")
        self.day_pnl = st.get("day_pnl", 0.0)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"bankroll": round(self.bankroll, 4), "day": self.day,
                                   "day_pnl": round(self.day_pnl, 4), "config": asdict(self.cfg)}))
        os.replace(tmp, self.path)

    def _roll(self, ts: float) -> None:
        if _day(ts) != self.day:
            self.day, self.day_pnl = _day(ts), 0.0

    def blocked(self, ts: float) -> str | None:
        """Reason no new trade may open right now, or None."""
        if self.kill_path.exists():
            return f"kill switch ({self.kill_path.name} file present)"
        self._roll(ts)
        if self.day_pnl <= -self.cfg.daily_loss_cap:
            return f"daily loss stop: ${self.day_pnl:+.2f} <= -${self.cfg.daily_loss_cap:.0f}"
        return None

    def size(self, p_win: float, price: float, fixed: int | None = None) -> tuple[int, str]:
        """(contracts, explanation). fixed=N bypasses Kelly (blind rule) but still obeys the $ cap."""
        if fixed is not None:
            n, why = fixed, f"fixed {fixed}"
        else:
            f = kelly_fraction(p_win, price, taker_fee_per_contract(price, 10))
            if f <= 0:
                return 0, f"kelly {f:+.3f} <= 0"
            stake = self.bankroll * f * self.cfg.kelly_fraction
            n = int(stake // (price + taker_fee_per_contract(price, 10)))
            why = f"kelly {f:.3f} x{self.cfg.kelly_fraction} on ${self.bankroll:.2f} -> ${stake:.2f}"
        # obey the per-trade dollar cap using the real (rounded) fee at that count
        while n > 0 and n * (price + taker_fee_per_contract(price, n)) > self.cfg.per_trade_cap + 1e-9:
            n -= 1
        return n, why

    def record(self, ts: float, pnl: float) -> None:
        self._roll(ts)
        self.day_pnl += pnl
        self.bankroll += pnl
        self._save()
