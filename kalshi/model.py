"""Fair value of a KXBTC15M YES contract and Kalshi fees.

Settlement: YES pays $1 if the 60s average of CF Benchmarks BRTI (1 print/sec) over the
final minute is above the market's floor_strike. Model: driftless Brownian price with
per-second vol sigma; arithmetic approximation (fine over minutes).

Let t = seconds to close, W = 60 (averaging window).
  t > W : A - S ~ N(0, (S*sig)^2 * ((t - W) + W/3))
  t <= W: (W - t) prints are already locked with mean m_obs;
          A = ((W-t)*m_obs + t*S)/W + (t/W) * S * N(0, sig^2 * t/3)
"""
from __future__ import annotations

import math
from statistics import NormalDist

WINDOW_S = 60
_N = NormalDist()


def fair_yes(spot: float, strike: float, t_rem: float, sigma_s: float,
             locked_mean: float | None = None, window: int = WINDOW_S) -> float:
    if t_rem <= 0:
        final = locked_mean if locked_mean is not None else spot
        return 1.0 if final > strike else 0.0
    if t_rem > window:
        mean = spot
        sd = spot * sigma_s * math.sqrt((t_rem - window) + window / 3)
    else:
        locked = locked_mean if locked_mean is not None else spot
        k = window - t_rem
        mean = (k * locked + t_rem * spot) / window
        sd = (t_rem / window) * spot * sigma_s * math.sqrt(t_rem / 3)
    if sd <= 0:
        return 1.0 if mean > strike else 0.0
    return _N.cdf((mean - strike) / sd)


def sigma_per_second(closes_1m: list[float]) -> float:
    """Realized vol from 1-minute closes, scaled to per-second."""
    rets = [math.log(b / a) for a, b in zip(closes_1m, closes_1m[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        raise ValueError("need >= 6 one-minute closes")
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var / 60.0)


def taker_fee_per_contract(price: float, contracts: int = 10, rate: float = 0.07) -> float:
    """Kalshi taker fee: ceil_to_cent(rate * C * P * (1-P)), spread over C contracts."""
    total = math.ceil(round(rate * contracts * price * (1 - price) * 100, 9)) / 100
    return total / contracts


SECONDS_PER_YEAR = 365 * 24 * 3600


def annualize(sigma_s: float) -> float:
    return sigma_s * math.sqrt(SECONDS_PER_YEAR)


def implied_sigma(price_yes: float, spot: float, strike: float, t_rem: float,
                  locked_mean: float | None = None, window: int = WINDOW_S) -> float | None:
    """Per-second vol at which fair_yes == price_yes (bisection).

    None when the price carries no vol information: at the 1c/99c rails, at the money
    (any vol gives 50%), or when the price points the opposite way from spot vs strike.
    """
    if not 0.02 <= price_yes <= 0.98 or t_rem <= 0:
        return None
    f = lambda s: fair_yes(spot, strike, t_rem, s, locked_mean, window)
    lo, hi = 1e-9, 1e-2                       # ~0.0006% .. 560% annualized
    f_lo, f_hi = f(lo), f(hi)
    if abs(f_hi - f_lo) < 1e-6 or (f_lo - price_yes) * (f_hi - price_yes) > 0:
        return None
    for _ in range(80):
        mid = math.sqrt(lo * hi)              # bisect in log space
        if (f(mid) - price_yes) * (f_lo - price_yes) > 0:
            lo, f_lo = mid, f(mid)
        else:
            hi = mid
    return math.sqrt(lo * hi)
