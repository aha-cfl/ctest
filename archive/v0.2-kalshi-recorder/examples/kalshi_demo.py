"""End-to-end dry run: the real Recorder + analyzer against a SIMULATED Kalshi/Coinbase.

The simulated market prices YES as the true fair value computed from BTC 5 seconds ago plus
+/-3c noise, i.e. it is deliberately stale. That is the kind of edge the live recording checks for.
Run: python -m examples.kalshi_demo [markets] [--efficient]
"""
import math
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kalshi.analyze import report
from kalshi.client import Coinbase, Kalshi
from kalshi.model import fair_yes
from kalshi.recorder import Recorder

SIG = 0.5 / math.sqrt(365 * 24 * 3600)


class SimExchange:
    def __init__(self, efficient: bool, seed=11):
        self.rng = random.Random(seed)
        self.efficient = efficient
        self.now, self.spot, self.hist = 0.0, 60000.0, []   # hist: (ts, spot)
        self.markets = {}                                    # ticker -> dict
        self.book_cache = None

    def open_market(self, close_ts):
        t = f"KXBTC15M-{int(close_ts)}"
        self.markets[t] = {"ticker": t, "close_time": _iso(close_ts), "close_ts": close_ts,
                           "floor_strike": self.spot, "status": "open", "result": "", "prints": []}

    def advance(self, to_ts):
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

    def _mkt_price(self, m):
        lag = 0 if self.efficient else 5
        ts, s = self.hist[max(0, len(self.hist) - 1 - lag)]
        pr = m["prints"][: max(0, len(m["prints"]) - lag)]
        fy = fair_yes(s, m["floor_strike"], m["close_ts"] - ts, SIG, sum(pr) / len(pr) if pr else None)
        noise = 0 if self.efficient else self.rng.gauss(0, 0.03)
        yes_bid = min(max(round(fy + noise - 0.01, 2), 0.01), 0.97)
        return yes_bid, round(1 - yes_bid - 0.02, 2)        # 2c wide spread

    def __call__(self, url, params=None):
        if url.endswith("/ticker"):
            return {"price": str(self.spot)}
        if url.endswith("/candles"):
            closes = [s for _, s in self.hist[-3600::60]] or [self.spot] * 60
            closes = (closes * 60)[:60] if len(closes) < 60 else closes[-60:]
            return [[0, 0, 0, 0, c, 0] for c in reversed(closes)]
        if url.endswith("/markets"):
            return {"markets": [_pub(m) for m in self.markets.values() if m["status"] == "open"]}
        ticker = url.split("/markets/")[1].split("/")[0]
        m = self.markets[ticker]
        if url.endswith("/orderbook"):
            yb, nb = self._mkt_price(m)
            return {"orderbook_fp": {"yes_dollars": [[f"{yb:.4f}", "50"]], "no_dollars": [[f"{nb:.4f}", "50"]]}}
        return {"market": _pub(m)}


def _pub(m):
    return {k: v for k, v in m.items() if k not in ("prints", "close_ts")}


def _iso(ts):
    return datetime.fromtimestamp(1_790_000_000 + ts, timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    n = int(next((a for a in sys.argv[1:] if a.isdigit()), 600))
    efficient = "--efficient" in sys.argv
    ex = SimExchange(efficient)
    ex.advance(3600)                                         # warm-up for vol estimate
    out = Path(tempfile.mkdtemp())
    rec = Recorder(Kalshi(ex), Coinbase(ex), out, window_s=120)
    base = 1_790_000_000
    for i in range(n):
        start = ex.now
        ex.open_market(start + 900)
        ex.advance(start + 900 - 121)
        while ex.now < start + 900:
            _tick(rec, ex, base)
            ex.advance(ex.now + 2)
        ex.advance(start + 901)
        _tick(rec, ex, base)                                 # resolves the settled market
    print(f"simulated {n} markets ({'efficient' if efficient else 'stale +/-3c noisy'} pricing) -> {out}\n")
    print(report(out))


def _tick(rec, ex, base):
    # Recorder compares wall-clock `now` to Kalshi close_time; sim close_time is offset by `base`.
    return rec.tick(base + ex.now)


if __name__ == "__main__":
    main()
