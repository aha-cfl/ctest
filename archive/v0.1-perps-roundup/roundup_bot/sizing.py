"""Fixed-fractional sizing: a full stop-out costs `risk_per_trade_pct` of equity, fees included."""
from __future__ import annotations

from dataclasses import dataclass

from .config import LadderConfig, RiskConfig


@dataclass
class Size:
    qty: float
    notional: float
    margin: float
    risk_usd: float
    capped_by_margin: bool


def stop_distance_frac(ladder: LadderConfig, risk: RiskConfig) -> float:
    """Price move to the initial stop, as a fraction of entry. -40% ROE at 5x -> 0.08."""
    return abs(ladder.stop_loss_roe_pct) / 100.0 / risk.leverage


def size_position(equity: float, price: float, ladder: LadderConfig, risk: RiskConfig) -> Size | None:
    """None when the risk budget can't fund the exchange's minimum order."""
    if equity <= 0 or price <= 0:
        return None
    risk_usd = equity * risk.risk_per_trade_pct / 100.0
    # Loss per $1 notional if stopped: price move + entry fee + exit fee.
    loss_per_notional = stop_distance_frac(ladder, risk) + 2 * risk.taker_fee_rate
    notional = risk_usd / loss_per_notional

    max_notional = equity * risk.margin_fraction_per_trade * risk.leverage
    capped = notional > max_notional
    if capped:
        notional = max_notional
        risk_usd = notional * loss_per_notional
    if notional < risk.min_order_notional_usd:
        return None
    return Size(notional / price, notional, notional / risk.leverage, risk_usd, capped)
