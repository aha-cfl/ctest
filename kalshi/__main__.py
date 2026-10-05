"""python -m kalshi [--data DIR] run | report | backtest | dashboard | export | demo"""
from __future__ import annotations

import argparse
import tempfile
import threading
from pathlib import Path


def build_desk(args):
    from .desk import Desk, PaperBook
    from .feeds import Coinbase, CompositeSpot, Kalshi
    from .rules import FilterConfig, RiskBook, RiskConfig, Rule

    rule = Rule(mode=args.rule, min_edge=args.min_edge, min_gap_usd=args.min_gap, min_vol_ratio=args.min_vol_ratio)
    books = [PaperBook(name, rule, execution=name, max_trades=args.max_trades,
                       risk=RiskBook(args.data / name, RiskConfig(args.bankroll, 0.25, args.per_trade_cap,
                                                                   args.daily_loss_cap), kill_path=args.data / "KILL"))
             for name in args.books.split(",")]
    spot = CompositeSpot() if args.spot == "composite" else Coinbase()
    return Desk(Kalshi(), spot, args.data, books, FilterConfig(news_blackout=not args.no_news_filter))


def main(argv=None):
    p = argparse.ArgumentParser(prog="kalshi", description="Kalshi KXBTC15M paper desk (no real orders)")
    p.add_argument("--data", type=Path, default=Path("data/live"))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the desk forever: record, decide, paper-fill, settle, export dashboard")
    r.add_argument("--rule", choices=["model", "favorite"], default="model")
    r.add_argument("--books", default="taker,maker", help="comma list of taker,maker")
    r.add_argument("--max-trades", type=int, default=10**9)
    r.add_argument("--min-edge", type=float, default=0.02)
    r.add_argument("--min-gap", type=float, default=5.0, help="$ basis guard around the strike")
    r.add_argument("--min-vol-ratio", type=float, default=0.0, help="market/realized vol filter, 0=off")
    r.add_argument("--bankroll", type=float, default=100.0)
    r.add_argument("--per-trade-cap", type=float, default=5.0)
    r.add_argument("--daily-loss-cap", type=float, default=25.0)
    r.add_argument("--no-news-filter", action="store_true")
    r.add_argument("--spot", choices=["composite", "coinbase"], default="composite")
    r.add_argument("--serve", type=int, metavar="PORT", help="also serve the live dashboard on 127.0.0.1:PORT")

    sub.add_parser("report", help="per-book win rate vs breakeven, breakdowns, real-money gate")
    b = sub.add_parser("backtest", help="replay recorded snapshots: blind favorite vs model rule")
    b.add_argument("--lo", type=float, default=0.78)
    b.add_argument("--hi", type=float, default=0.85)
    b.add_argument("--min-edge", type=float, default=0.02)
    b.add_argument("--contracts", type=int, default=10)
    b.add_argument("--max-t-rem", type=float, default=120)
    d = sub.add_parser("dashboard", help="serve the live dashboard (read-only, no login: keep 127.0.0.1)")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8080)
    e = sub.add_parser("export", help="write a self-contained HTML snapshot of the dashboard")
    e.add_argument("out", type=Path)
    e.add_argument("--label")
    e.add_argument("--fragment", action="store_true", help="omit <!doctype>/<html> wrapper")
    m = sub.add_parser("demo", help="run the desk against the simulated exchange (no network)")
    m.add_argument("--rule", choices=["model", "favorite"], default="model")
    m.add_argument("--max-trades", type=int, default=25)
    m.add_argument("--seed", type=int, default=21)
    m.add_argument("--efficient", action="store_true", help="fairly priced market: no edge to find")
    m.add_argument("--quiet", action="store_true", help="print only buys and settlements")
    m.add_argument("--out", type=Path, help="demo data dir (default: a new temp dir, never data/live)")
    args = p.parse_args(argv)

    if args.cmd == "run":
        from .dashboard import export, serve
        desk = build_desk(args).resume()
        if args.serve:
            threading.Thread(target=serve, args=(args.data, "127.0.0.1", args.serve), daemon=True).start()
        desk.run(every=lambda: export(args.data, args.data / "desk.html", full_document=False,
                                      label="Live Kalshi data · paper fills"))
    elif args.cmd == "report":
        from .report import trades_report
        print(trades_report(args.data))
    elif args.cmd == "backtest":
        from .report import backtest_report
        print(backtest_report(args.data, args.lo, args.hi, args.min_edge, args.contracts, args.max_t_rem))
    elif args.cmd == "dashboard":
        from .dashboard import serve
        serve(args.data, args.host, args.port)
    elif args.cmd == "export":
        from .dashboard import export
        print(export(args.data, args.out, not args.fragment, args.label))
    elif args.cmd == "demo":
        from .report import trades_report
        from .sim import run_demo
        out = args.out or Path(tempfile.mkdtemp(prefix="kalshi-demo-"))
        log = (lambda s: s.startswith(("[taker] >>>", "[taker] <<<", "[maker] >>>", "[maker] <<<")) and print(s)) \
            if args.quiet else print
        run_demo(out, mode=args.rule, max_trades=args.max_trades, seed=args.seed, efficient=args.efficient, log=log)
        print(f"\n{trades_report(out)}\n\ndata: {out}")


if __name__ == "__main__":
    main()
