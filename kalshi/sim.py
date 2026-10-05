"""Simulated Kalshi + BTC feed for demos, tests and parity checks. No network.

BTC is a 50%-vol random walk sampled each second; each 15-minute market settles on the true
60s average. The simulated book prices YES from BTC 5 seconds ago plus +/-3c noise, i.e. it is
deliberately stale: the kind of edge the live desk is looking for. efficient=True removes both.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .feeds import Coinbase, Kalshi
from .model import fair_yes

SIG = 0.5 / math.sqrt(365 * 24 * 3600)
BASE = 1_790_000_000            # sim second 0 == this unix time


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(BASE + ts, timezone.utc).isoformat().replace("+00:00", "Z")


class SimExchange:
    """Callable like http_get(url, params): serves Kalshi and Coinbase endpoints from the simulation."""

    def __init__(self, efficient: bool = False, seed: int = 21):
        self.rng = random.Random(seed)
        self.efficient = efficient
        self.now, self.spot, self.hist = 0.0, 60000.0, []   # hist: (ts, spot)
        self.markets: dict[str, dict] = {}

    def open_market(self, close_ts: float) -> None:
        t = f"KXBTC15M-{int(close_ts)}"
        self.markets[t] = {"ticker": t, "close_time": _iso(close_ts), "close_ts": close_ts,
                           "floor_strike": self.spot, "status": "open", "result": "", "prints": []}

    def advance(self, to_ts: float) -> None:
        while self.now < to_ts:
            self.now += 1
            self.spot *= math.exp(self.rng.gauss(0, SIG))
            self.hist.append((self.now, self.spot))
            for m in self.markets.values():
                if m["status"] == "open" and 0 <= m["close_ts"] - self.now < 60:
                    m["prints"].append(self.spot)
                if m["status"] == "open" and self.now >= m["close_ts"]:
                    avg = sum(m["prints"]) / len(m["prints"])
                    m["status"], m["result"] = "settled", "yes" if avg > m["floor_strike"] else "no"

    def _mkt_price(self, m: dict) -> tuple[float, float]:
        lag = 0 if self.efficient else 5
        ts, s = self.hist[max(0, len(self.hist) - 1 - lag)]
        pr = m["prints"][: max(0, len(m["prints"]) - lag)]
        fy = fair_yes(s, m["floor_strike"], m["close_ts"] - ts, SIG, sum(pr) / len(pr) if pr else None)
        noise = 0 if self.efficient else self.rng.gauss(0, 0.03)
        yes_bid = min(max(round(fy + noise - 0.01, 2), 0.01), 0.97)
        return yes_bid, round(1 - yes_bid - 0.02, 2)        # 2c wide spread

    def __call__(self, url: str, params=None):
        if url.endswith("/ticker"):
            return {"price": str(self.spot)}
        if url.endswith("/candles"):
            closes = [s for _, s in self.hist[-3600::60]] or [self.spot] * 60
            closes = (closes * 60)[:60] if len(closes) < 60 else closes[-60:]
            return [[0, 0, 0, 0, c, 0] for c in reversed(closes)]
        if url.endswith("/markets"):
            return {"markets": [_pub(m) for m in self.markets.values() if m["status"] == "open"]}
        m = self.markets[url.split("/markets/")[1].split("/")[0]]
        if url.endswith("/orderbook"):
            yb, nb = self._mkt_price(m)
            return {"orderbook_fp": {"yes_dollars": [[f"{yb:.4f}", "50"]], "no_dollars": [[f"{nb:.4f}", "50"]]}}
        return {"market": _pub(m)}

    def feeds(self) -> tuple[Kalshi, Coinbase]:
        return Kalshi(self, markets_ttl=0), Coinbase(self)   # no cache: the sim clock runs fast


def _pub(m: dict) -> dict:
    return {k: v for k, v in m.items() if k not in ("prints", "close_ts")}


def drive(desk, ex: SimExchange, max_markets: int = 5000) -> None:
    """Run `desk` against `ex` market by market until every book is done (or max_markets)."""
    for _ in range(max_markets):
        if desk.done:
            return
        start = ex.now
        ex.open_market(start + 900)
        while ex.now < start + 901 and not desk.done:
            desk.tick(BASE + ex.now)
            busy = desk.in_window or any(b.open_trades for b in desk.books)
            ex.advance(ex.now + (2 if busy else 30))
        desk.tick(BASE + ex.now)


def run_demo(out_dir: Path, mode: str = "model", executions=("taker", "maker"), max_trades: int = 25,
             seed: int = 21, kelly: bool = True, efficient: bool = False,
             log: Callable[[str], None] = print):
    from .desk import Desk, PaperBook
    from .rules import RiskBook, RiskConfig, Rule

    ex = SimExchange(efficient=efficient, seed=seed)
    ex.advance(3600)                                     # warm-up history for realized vol
    kalshi, coinbase = ex.feeds()
    out_dir = Path(out_dir)
    books = [PaperBook(name, Rule(mode=mode), execution=name, max_trades=max_trades,
                       risk=RiskBook(out_dir / name, RiskConfig(), kill_path=out_dir / "KILL") if kelly else None)
             for name in executions]
    desk = Desk(kalshi, coinbase, out_dir, books, log=log)
    drive(desk, ex)
    return desk
