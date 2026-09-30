"""Trend-following entry signal: EMA crossover on closed bars."""
from __future__ import annotations

from .config import StrategyConfig


def ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out: list[float] = []
    for v in values:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def signal(closes: list[float], cfg: StrategyConfig) -> int:
    """+1 on bullish cross, -1 on bearish cross (if shorts allowed), else 0."""
    if len(closes) < cfg.slow_ema + 2:
        return 0
    fast, slow = ema(closes, cfg.fast_ema), ema(closes, cfg.slow_ema)
    prev, now = fast[-2] - slow[-2], fast[-1] - slow[-1]
    if prev <= 0 < now:
        return 1
    if prev >= 0 > now and cfg.allow_short:
        return -1
    return 0
