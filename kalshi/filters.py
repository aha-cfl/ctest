"""Pre-trade filters. Each returns a skip reason (str) or None when the trade may proceed."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .client import Book


@dataclass
class FilterConfig:
    max_spread: float = 0.03            # yes_ask - yes_bid, dollars
    vol_spike_ratio: float = 2.0        # short-window realized vol / 60-min realized vol
    vol_spike_minutes: int = 5
    # Scheduled US macro releases (UTC "HH:MM"). 12:30/14:00/18:00 are 8:30am, 10am and 2pm ET
    # in daylight time; 13:30/15:00/19:00 cover standard time. Weekdays only.
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
    """(reason, short/long vol ratio). Needs > vol_spike_minutes + 5 closes, else no opinion."""
    rets = [math.log(b / a) for a, b in zip(closes_1m, closes_1m[1:]) if a > 0 and b > 0]
    n = cfg.vol_spike_minutes
    if len(rets) < n + 5:
        return None, None
    long_sd, short_sd = _sd(rets), _sd(rets[-n:])
    if long_sd <= 0:
        return None, None
    ratio = short_sd / long_sd
    reason = f"vol spike {ratio:.1f}x last {n}m" if ratio > cfg.vol_spike_ratio else None
    return reason, ratio


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
