from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class RoundupConfig:
    multiplier: float = 1.0          # 2.0 = double each round-up
    whole_dollar_adds_one: bool = False  # $5.00 purchase -> $1 round-up instead of $0
    min_sweep_usd: float = 25.0      # accumulate until this before moving money
    ledger_path: str = "data/roundup_ledger.json"


@dataclass
class StrategyConfig:
    fast_ema: int = 20
    slow_ema: int = 50
    allow_short: bool = True


@dataclass
class LadderConfig:
    # Take-profit levels as return on margin (ROE %), not price %.
    # At 5x leverage, +50% ROE == +10% price move.
    levels_pct: list[float] = field(default_factory=lambda: [50, 100, 150, 200])
    # Fraction of the ORIGINAL position closed at each level above.
    close_fractions: list[float] = field(default_factory=lambda: [0.25, 0.25, 0.25, 0.0])
    # After the last level, keep adding a level every N% ROE ("and on and on").
    step_after_pct: float = 50.0
    # Initial stop, in ROE %. Must sit well above liquidation.
    stop_loss_roe_pct: float = -40.0
    # When level k is hit, stop ratchets to level k-1 (level 0 == breakeven).
    ratchet_stop: bool = True


@dataclass
class RiskConfig:
    leverage: float = 5.0
    max_leverage: float = 10.0
    risk_per_trade_pct: float = 1.0           # equity lost if the initial stop is hit
    max_risk_per_trade_pct: float = 2.0       # hard ceiling on the above
    margin_fraction_per_trade: float = 0.25   # cap: max share of equity posted as margin
    maintenance_margin_rate: float = 0.005
    liq_buffer_roe_pct: float = 20.0          # stop must be >= this far above liq
    max_drawdown_pct: float = 50.0            # kill switch from equity peak
    taker_fee_rate: float = 0.0005
    min_order_notional_usd: float = 5.0


@dataclass
class Config:
    mode: str = "paper"                 # paper | live
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    exchange: str = "bybit"
    roundup: RoundupConfig = field(default_factory=RoundupConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    ladder: LadderConfig = field(default_factory=LadderConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


def _build(cls, data: dict | None):
    data = data or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        sub = {"roundup": RoundupConfig, "strategy": StrategyConfig,
               "ladder": LadderConfig, "risk": RiskConfig}.get(name) if cls is Config else None
        kwargs[name] = _build(sub, value) if sub else value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return validate(Config())
    with open(path) as fh:
        return validate(_build(Config, yaml.safe_load(fh)))


def validate(cfg: Config) -> Config:
    r, lad = cfg.risk, cfg.ladder
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    if not 1 <= r.leverage <= r.max_leverage:
        raise ValueError(f"leverage {r.leverage} outside [1, {r.max_leverage}]")
    if not 0 < r.risk_per_trade_pct <= r.max_risk_per_trade_pct:
        raise ValueError(f"risk_per_trade_pct {r.risk_per_trade_pct} outside (0, {r.max_risk_per_trade_pct}]")
    if len(lad.levels_pct) != len(lad.close_fractions):
        raise ValueError("ladder.levels_pct and ladder.close_fractions must match in length")
    if sorted(lad.levels_pct) != list(lad.levels_pct) or lad.levels_pct[0] <= 0:
        raise ValueError("ladder.levels_pct must be positive and ascending")
    if sum(lad.close_fractions) > 1.0 + 1e-9:
        raise ValueError("ladder.close_fractions sum > 1")
    if lad.stop_loss_roe_pct >= 0:
        raise ValueError("ladder.stop_loss_roe_pct must be negative (a stop is mandatory)")
    liq_roe = liquidation_roe_pct(r.leverage, r.maintenance_margin_rate)
    if lad.stop_loss_roe_pct < liq_roe + r.liq_buffer_roe_pct:
        raise ValueError(
            f"stop {lad.stop_loss_roe_pct}% ROE is within {r.liq_buffer_roe_pct}% of "
            f"liquidation (~{liq_roe:.1f}% ROE at {r.leverage}x). Lower leverage or tighten stop."
        )
    return cfg


def liquidation_roe_pct(leverage: float, mmr: float) -> float:
    """Approx isolated-margin liquidation point in ROE %. Price move = -(1/lev - mmr)."""
    return -(1.0 / leverage - mmr) * leverage * 100.0
