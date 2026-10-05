"""Should we trade this side, and how big: entry rule, market filters, risk limits, sizing.

Checks run in this order and the first failure is the skip reason:
  price band -> model edge -> basis guard -> vol-ratio filter -> market filters
  (news, vol spike, spread) -> kill switch / daily loss stop -> Kelly size
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .feeds import Book
from .model import kelly_fraction, taker_fee_per_contract


@dataclass
class Rule:
    mode: str = "model"             # model: needs edge >= min_edge | favorite: blind, demo only
    lo: float = 0.78
    hi: float = 0.85
    min_edge: float = 0.02
    max_t_rem: float = 120.0
    contracts: int = 1              # used when no RiskBook is attached, and as the fee basis for edge
    min_gap_usd: float = 5.0        # skip when expected settlement is this close to strike (venue basis)
    min_vol_ratio: float = 0.0      # 0=off; else buy only if market-implied vol / realized vol >= this


def side_blocker(rule: Rule, side: str, ask: float, fair: float, gap: float,
                 vol_ratio: float | None) -> str | None:
    """Per-side checks. Returns the skip note, or None if this side passes."""
    edge = fair - ask - taker_fee_per_contract(ask, rule.contracts)
    if not rule.lo <= ask <= rule.hi:
        return f"{side} ask {ask:.2f} outside band"
    if rule.mode == "model" and edge < rule.min_edge:
        return f"{side} ask {ask:.2f} fair {fair:.3f} edge {edge:+.3f} < {rule.min_edge:.2f}"
    if abs(gap) < rule.min_gap_usd:
        return f"expected settle ${gap:+.0f} from strike < ${rule.min_gap_usd:.0f} basis guard"
    if rule.min_vol_ratio and (vol_ratio is None or vol_ratio < rule.min_vol_ratio):
        return f"vol ratio {vol_ratio if vol_ratio is None else round(vol_ratio, 2)} < {rule.min_vol_ratio}"
    return None


# ---- market filters --------------------------------------------------------------------------
@dataclass
class FilterConfig:
    max_spread: float = 0.03            # yes_ask - yes_bid, dollars
    vol_spike_ratio: float = 2.0        # short-window realized vol / 60-min realized vol
    vol_spike_minutes: int = 5
    # Scheduled US macro releases, UTC. 12:30/14:00/18:00 = 8:30am/10am/2pm ET in daylight time;
    # 13:30/15:00/19:00 cover standard time. Weekdays only.
    news_times_utc: list[str] = field(default_factory=lambda: ["12:30", "13:30", "14:00", "15:00",
                                                                "18:00", "19:00"])
    news_pad_s: float = 120.0
    news_blackout: bool = True


def spread(book: Book, cfg: FilterConfig) -> str | None:
    if book.yes_bid is None or book.yes_ask is None:
        return "one-sided book"
    width = book.yes_ask - book.yes_bid
    return f"spread {width * 100:.0f}c > {cfg.max_spread * 100:.0f}c" if width > cfg.max_spread + 1e-9 else None


def _sd(rets: list[float]) -> float:
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / max(len(rets) - 1, 1))


def vol_spike(closes_1m: list[float], cfg: FilterConfig) -> tuple[str | None, float | None]:
    """(reason, short/long vol ratio); no opinion without enough history."""
    rets = [math.log(b / a) for a, b in zip(closes_1m, closes_1m[1:]) if a > 0 and b > 0]
    n = cfg.vol_spike_minutes
    if len(rets) < n + 5:
        return None, None
    long_sd, short_sd = _sd(rets), _sd(rets[-n:])
    if long_sd <= 0:
        return None, None
    ratio = short_sd / long_sd
    return (f"vol spike {ratio:.1f}x last {n}m" if ratio > cfg.vol_spike_ratio else None), ratio


def news(now_ts: float, close_ts: float, cfg: FilterConfig) -> str | None:
    """Skip if a scheduled release lands between now and settlement (padded)."""
    if not cfg.news_blackout:
        return None
    start = datetime.fromtimestamp(now_ts - cfg.news_pad_s, timezone.utc)
    end = datetime.fromtimestamp(close_ts + cfg.news_pad_s, timezone.utc)
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5:
            for hhmm in cfg.news_times_utc:
                h, m = map(int, hhmm.split(":"))
                t = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
                if start <= t <= end:
                    return f"news blackout {hhmm} UTC"
        day += timedelta(days=1)
    return None


# ---- risk ------------------------------------------------------------------------------------
@dataclass
class RiskConfig:
    bankroll: float = 100.0          # starting paper bankroll, dollars
    kelly_fraction: float = 0.25
    per_trade_cap: float = 5.0       # max dollars per trade, (price + fee) x count
    daily_loss_cap: float = 25.0     # stop opening trades once today's realized P&L <= -this


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class RiskBook:
    """Quarter-Kelly sizing, $ cap per trade, daily loss stop, kill switch. State in risk.json."""

    def __init__(self, data_dir: Path, cfg: RiskConfig | None = None, kill_path: Path | None = None):
        self.cfg = cfg or RiskConfig()
        self.path = Path(data_dir) / "risk.json"
        self.kill_path = Path(kill_path) if kill_path else Path(data_dir) / "KILL"
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
        if self.kill_path.exists():
            return f"kill switch ({self.kill_path.name} file present)"
        self._roll(ts)
        if self.day_pnl <= -self.cfg.daily_loss_cap:
            return f"daily loss stop: ${self.day_pnl:+.2f} <= -${self.cfg.daily_loss_cap:.0f}"
        return None

    def size(self, p_win: float, price: float, fixed: int | None = None) -> tuple[int, str]:
        """(contracts, why). fixed=N bypasses Kelly (blind rule) but still obeys the $ cap."""
        if fixed is not None:
            n, why = fixed, f"fixed {fixed}"
        else:
            fee10 = taker_fee_per_contract(price, 10)
            f = kelly_fraction(p_win, price, fee10)
            if f <= 0:
                return 0, f"kelly {f:+.3f} <= 0"
            stake = self.bankroll * f * self.cfg.kelly_fraction
            n = int(stake // (price + fee10))
            why = f"kelly {f:.3f} x{self.cfg.kelly_fraction} on ${self.bankroll:.2f} -> ${stake:.2f}"
        while n > 0 and n * (price + taker_fee_per_contract(price, n)) > self.cfg.per_trade_cap + 1e-9:
            n -= 1
        return n, why

    def record(self, ts: float, pnl: float) -> None:
        self._roll(ts)
        self.day_pnl += pnl
        self.bankroll += pnl
        self._save()
