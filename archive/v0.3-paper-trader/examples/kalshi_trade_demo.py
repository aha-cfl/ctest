"""Watch one paper trade end to end: wait for the final 2 minutes, decide, buy, settle.

Uses the real Trader + PaperExecutor against the simulated exchange from kalshi_demo
(stale, noisy pricing so the model finds an edge). Run: python -m examples.kalshi_trade_demo [--favorite]
"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from examples.kalshi_demo import SimExchange
from kalshi.client import Coinbase, Kalshi
from kalshi.execution import PaperExecutor
from kalshi.recorder import read_jsonl
from kalshi.trader import Rule, Trader

BASE = 1_790_000_000


def main():
    ex = SimExchange(efficient=False, seed=int(next((a for a in sys.argv[1:] if a.isdigit()), 21)))
    ex.advance(3600)
    clock = lambda: datetime.fromtimestamp(BASE + ex.now, timezone.utc).strftime("%H:%M:%S")
    out = Path(tempfile.mkdtemp())
    rule = Rule(mode="favorite" if "--favorite" in sys.argv else "model")
    trader = Trader(Kalshi(ex), Coinbase(ex), PaperExecutor(), out, rule, max_trades=1,
                    log=lambda m: print(f"[{clock()}] {m}"))
    print(f"[{clock()}] [trader] rule={rule.mode} band {rule.lo:.2f}-{rule.hi:.2f} "
          f"min_edge {rule.min_edge:.2f} | paper executor | output {out}")
    while not trader.done:
        start = ex.now
        ex.open_market(start + 900)
        while ex.now < start + 901 and not trader.done:
            trader.tick(BASE + ex.now)
            ex.advance(ex.now + (2 if trader.in_window or trader.open_trades else 30))
        trader.tick(BASE + ex.now)
    print("\ntrades.jsonl:")
    for r in read_jsonl(out / "trades.jsonl"):
        keep = ("status", "ticker", "side", "count", "price", "fee", "fair", "edge", "result", "pnl")
        print("  ", {k: r[k] for k in keep if k in r})


if __name__ == "__main__":
    main()
