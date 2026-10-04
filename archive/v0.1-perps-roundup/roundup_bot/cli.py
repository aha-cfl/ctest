"""CLI: `roundup` (bank -> ledger -> sweep), `backtest` (paper on CSV bars), `live` (ccxt loop)."""
from __future__ import annotations

import argparse
import csv
import time

from .broker import CcxtBroker, PaperBroker
from .config import load_config
from .engine import Engine
from .position import Bar
from .roundup import Ledger, ManualSink, fetch_plaid, load_csv, sweep


def load_bars(path: str) -> list[Bar]:
    """CSV columns: ts,open,high,low,close"""
    with open(path, newline="") as fh:
        return [Bar(r["ts"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                for r in csv.DictReader(fh)]


def cmd_roundup(args, cfg) -> None:
    ledger = Ledger(cfg.roundup.ledger_path)
    txns = load_csv(args.csv) if args.csv else fetch_plaid(ledger)
    added = ledger.add(txns, cfg.roundup)
    moved = sweep(ledger, ManualSink(), cfg.roundup)
    ledger.save()
    print(f"added ${added}  pending ${ledger.pending}  swept-now ${moved}  swept-total ${ledger.swept_total}")


def cmd_backtest(args, cfg) -> None:
    broker = PaperBroker(cash=args.cash, fee_rate=cfg.risk.taker_fee_rate)
    eng = Engine(cfg, broker)
    bars = load_bars(args.prices)
    for i, bar in enumerate(bars):
        if args.monthly_deposit and i and i % args.bars_per_month == 0:
            eng.deposit(args.monthly_deposit)
        eng.on_bar(bar)
    if eng.pos:
        eng._apply(bars[-1].ts, eng.pos.exit_all(bars[-1].close, "end-of-data"))
    for t in eng.log:
        print(f"{t.ts:>20} {t.action:<14} side={t.side:+d} qty={t.qty:.6f} px={t.price:,.2f} eq={t.equity:,.2f}")
    deposited = args.cash + args.monthly_deposit * ((len(bars) - 1) // args.bars_per_month)
    print(f"\ndeposited ${deposited:,.2f}  final equity ${broker.equity():,.2f}  "
          f"fees ${broker.fees_paid:,.2f}  halted={eng.halted}")


def cmd_live(args, cfg) -> None:
    broker = CcxtBroker(cfg.exchange, cfg.symbol)
    eng = Engine(cfg, broker)
    last_ts = None
    while not eng.halted:
        rows = broker.ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=cfg.strategy.slow_ema * 3)
        closed = rows[:-1]  # last candle is still forming
        if not eng.closes:
            eng.closes = [r[4] for r in closed[:-1]]
        ts, o, h, l, c, _ = closed[-1]
        if ts != last_ts:
            last_ts = ts
            eng.on_bar(Bar(str(ts), o, h, l, c))
            if eng.log:
                print(eng.log[-1])
        time.sleep(args.poll_seconds)
    print("halted by kill switch")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="roundup_bot")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("roundup")
    r.add_argument("--csv", help="transactions CSV; omit to pull from Plaid")

    b = sub.add_parser("backtest")
    b.add_argument("--prices", required=True)
    b.add_argument("--cash", type=float, default=25.0)
    b.add_argument("--monthly-deposit", type=float, default=0.0)
    b.add_argument("--bars-per-month", type=int, default=720)

    lv = sub.add_parser("live")
    lv.add_argument("--poll-seconds", type=int, default=30)

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "live" and cfg.mode != "live":
        p.error("config mode must be 'live' to run the live command")
    {"roundup": cmd_roundup, "backtest": cmd_backtest, "live": cmd_live}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
