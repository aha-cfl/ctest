"""Score recorded snapshots against settled outcomes.

Each market counts ONCE per strategy (first qualifying snapshot). Snapshots 2s apart in the
same market are not independent trials; counting them all would fake statistical significance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .model import taker_fee_per_contract
from .recorder import read_jsonl


@dataclass
class StratResult:
    name: str
    n: int
    wins: int
    avg_price: float
    avg_fee: float
    pnl_per_contract: float
    ci_low: float
    ci_high: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def breakeven(self) -> float:
        return self.avg_price + self.avg_fee

    @property
    def verdict(self) -> str:
        if self.n < 100:
            return "insufficient data (<100 markets)"
        if self.ci_low > self.breakeven:
            return "EDGE: CI lower bound above breakeven"
        if self.ci_high < self.breakeven:
            return "NEGATIVE: CI upper bound below breakeven"
        return "inconclusive: CI straddles breakeven"


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def load(data_dir: Path) -> tuple[dict[str, list[dict]], dict[str, str]]:
    outcomes = {o["ticker"]: o["result"] for o in read_jsonl(data_dir / "outcomes.jsonl")}
    by_ticker: dict[str, list[dict]] = {}
    for s in read_jsonl(data_dir / "snapshots.jsonl"):
        if s["ticker"] in outcomes:
            by_ticker.setdefault(s["ticker"], []).append(s)
    for rows in by_ticker.values():
        rows.sort(key=lambda r: -r["t_rem"])  # earliest (most time left) first
    return by_ticker, outcomes


def _candidates(s: dict) -> list[tuple[str, float, float, float]]:
    """(side, ask, fair, depth) for each side that has an ask."""
    out = []
    if s["yes_ask"] is not None:
        out.append(("yes", s["yes_ask"], s["fair_yes"], s["no_bid_size"]))
    if s["no_ask"] is not None:
        out.append(("no", s["no_ask"], 1 - s["fair_yes"], s["yes_bid_size"]))
    return out


def run_strategy(name, by_ticker, outcomes, lo, hi, min_edge, contracts, max_t_rem, use_model) -> StratResult:
    wins = n = 0
    prices, fees, pnl = [], [], 0.0
    for ticker, snaps in by_ticker.items():
        for s in snaps:
            if s["t_rem"] > max_t_rem:
                continue
            pick = None
            for side, ask, fair, depth in _candidates(s):
                if not lo <= ask <= hi or depth < 1:
                    continue
                fee = taker_fee_per_contract(ask, contracts)
                if use_model and fair - ask - fee < min_edge:
                    continue
                pick = (side, ask, fee)
                break
            if pick:
                side, ask, fee = pick
                won = outcomes[ticker] == side
                n += 1
                wins += won
                prices.append(ask)
                fees.append(fee)
                pnl += (1.0 if won else 0.0) - ask - fee
                break  # one trade per market
    lo_ci, hi_ci = wilson(wins, n)
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return StratResult(name, n, wins, avg(prices), avg(fees), pnl / n if n else 0.0, lo_ci, hi_ci)


def calibration(by_ticker, outcomes, max_t_rem: float) -> dict:
    """First snapshot per market at or under max_t_rem. Brier: lower is better."""
    pts = []
    for ticker, snaps in by_ticker.items():
        s = next((x for x in snaps if x["t_rem"] <= max_t_rem
                  and x["yes_bid"] is not None and x["yes_ask"] is not None), None)
        if s:
            mid = (s["yes_bid"] + s["yes_ask"]) / 2
            pts.append((s["fair_yes"], mid, 1.0 if outcomes[ticker] == "yes" else 0.0))
    if not pts:
        return {"n": 0}
    brier = lambda i: sum((p[i] - p[2]) ** 2 for p in pts) / len(pts)
    buckets = {}
    for f, _, y in pts:
        b = min(int(f * 10), 9)
        c = buckets.setdefault(b, [0, 0.0, 0.0])
        c[0] += 1; c[1] += f; c[2] += y
    table = [(f"{b/10:.1f}-{(b+1)/10:.1f}", c[0], c[1] / c[0], c[2] / c[0]) for b, c in sorted(buckets.items())]
    return {"n": len(pts), "brier_model": brier(0), "brier_market": brier(1), "table": table}


def report(data_dir: Path, lo=0.78, hi=0.85, min_edge=0.02, contracts=10, max_t_rem=120.0) -> str:
    by_ticker, outcomes = load(data_dir)
    lines = [f"markets with snapshots+outcome: {len(by_ticker)}  "
             f"(band {lo:.2f}-{hi:.2f}, entry t<= {max_t_rem:g}s, fee @ {contracts} contracts)"]
    for name, use_model in (("favorite (blind)", False), (f"model edge>={min_edge:.2f}", True)):
        r = run_strategy(name, by_ticker, outcomes, lo, hi, min_edge, contracts, max_t_rem, use_model)
        lines.append(f"\n[{r.name}] n={r.n} win {r.win_rate:.1%} (95% CI {r.ci_low:.1%}-{r.ci_high:.1%}) | "
                     f"avg ask {r.avg_price:.3f} + fee {r.avg_fee:.4f} = breakeven {r.breakeven:.1%} | "
                     f"PnL/contract {r.pnl_per_contract:+.4f}\n  -> {r.verdict}")
    cal = calibration(by_ticker, outcomes, max_t_rem)
    if cal["n"]:
        lines.append(f"\ncalibration (n={cal['n']}): Brier model {cal['brier_model']:.4f} vs "
                     f"market mid {cal['brier_market']:.4f} ({'model better' if cal['brier_model'] < cal['brier_market'] else 'market better'})")
        lines.append("  fair bucket |   n | avg fair | realized YES")
        lines += [f"  {b:>11} | {n:3d} | {f:8.3f} | {y:8.3f}" for b, n, f, y in cal["table"]]
    return "\n".join(lines)
