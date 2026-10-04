"""python -m kalshi record | analyze | resolve | paper-trade | dashboard | export"""
import argparse
from pathlib import Path

from .analyze import report
from .client import Coinbase, Kalshi
from .recorder import Recorder, append_jsonl, read_jsonl


def main(argv=None):
    p = argparse.ArgumentParser(prog="kalshi")
    p.add_argument("--data", default="data/kalshi")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="log last-N-seconds books + fair value (no trading)")
    r.add_argument("--window", type=float, default=120)
    r.add_argument("--poll", type=float, default=2.0)
    a = sub.add_parser("analyze")
    a.add_argument("--lo", type=float, default=0.78)
    a.add_argument("--hi", type=float, default=0.85)
    a.add_argument("--min-edge", type=float, default=0.02)
    a.add_argument("--contracts", type=int, default=10)
    a.add_argument("--max-t-rem", type=float, default=120)
    sub.add_parser("resolve", help="fetch results for recorded markets missing an outcome")
    sub.add_parser("report", help="live paper trades: win rate vs breakeven, breakdowns, go/no-go gate")
    d = sub.add_parser("dashboard", help="serve the live dashboard (read-only)")
    d.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 and use an SSH tunnel; there is no login")
    d.add_argument("--port", type=int, default=8080)
    e = sub.add_parser("export", help="write a self-contained HTML snapshot of the dashboard")
    e.add_argument("out")
    e.add_argument("--label", help="banner shown on the snapshot, e.g. 'Simulated exchange'")
    e.add_argument("--fragment", action="store_true", help="omit <!doctype>/<html> wrapper")
    t = sub.add_parser("paper-trade", help="paper: decide, simulate buy at ask, wait for real settlement")
    t.add_argument("--rule", choices=["model", "favorite"], default="model")
    t.add_argument("--max-trades", type=int, default=1)
    t.add_argument("--contracts", type=int, default=1)
    t.add_argument("--lo", type=float, default=0.78)
    t.add_argument("--hi", type=float, default=0.85)
    t.add_argument("--min-edge", type=float, default=0.02)
    t.add_argument("--max-t-rem", type=float, default=120)
    t.add_argument("--min-gap", type=float, default=5.0, help="$ basis guard around the strike")
    t.add_argument("--min-vol-ratio", type=float, default=0.0, help="market/realized vol filter, 0=off")
    t.add_argument("--spot", choices=["composite", "coinbase"], default="composite")
    t.add_argument("--execution", choices=["taker", "maker"], default="taker")
    t.add_argument("--bankroll", type=float, default=100.0, help="paper bankroll for Kelly sizing")
    t.add_argument("--per-trade-cap", type=float, default=5.0)
    t.add_argument("--daily-loss-cap", type=float, default=25.0)
    t.add_argument("--no-news-filter", action="store_true")
    t.add_argument("--ntfy-topic", help="push alerts to https://ntfy.sh/<topic> (needs ntfy.sh allowed)")
    args = p.parse_args(argv)
    data = Path(args.data)

    if args.cmd == "record":
        from .prices import CompositeSpot
        Recorder(Kalshi(), CompositeSpot(), data, window_s=args.window).run(poll_s=args.poll, idle_poll_s=5.0)
    elif args.cmd == "dashboard":
        from .dashboard import serve
        serve(data, args.host, args.port)
    elif args.cmd == "export":
        from .dashboard import export
        print(export(data, Path(args.out), not args.fragment, args.label))
    elif args.cmd == "paper-trade":
        from .execution import PaperExecutor
        from .trader import Rule, Trader
        from .prices import CompositeSpot
        from .filters import FilterConfig
        from .risk import RiskBook, RiskConfig
        rule = Rule(args.rule, args.lo, args.hi, args.min_edge, args.max_t_rem, args.contracts,
                    args.min_gap, args.min_vol_ratio, args.execution)
        spot = CompositeSpot() if args.spot == "composite" else Coinbase()
        risk = RiskBook(data, RiskConfig(args.bankroll, 0.25, args.per_trade_cap, args.daily_loss_cap))
        notify = None
        if args.ntfy_topic:
            import requests
            notify = lambda msg: requests.post(f"https://ntfy.sh/{args.ntfy_topic}", data=msg.encode(),
                                               headers={"Title": "Kalshi BTC signal", "Priority": "high"}, timeout=5)
        Trader(Kalshi(), spot, PaperExecutor(), data, rule, max_trades=args.max_trades, risk=risk,
               filters=FilterConfig(news_blackout=not args.no_news_filter), notify=notify).run()
    elif args.cmd == "report":
        from .analyze import trades_report
        print(trades_report(data))
    elif args.cmd == "analyze":
        print(report(data, args.lo, args.hi, args.min_edge, args.contracts, args.max_t_rem))
    else:
        k = Kalshi()
        done = {o["ticker"] for o in read_jsonl(data / "outcomes.jsonl")}
        for t in sorted({s["ticker"] for s in read_jsonl(data / "snapshots.jsonl")} - done):
            m = k.market(t)
            if m.result in ("yes", "no"):
                append_jsonl(data / "outcomes.jsonl", {"ticker": t, "result": m.result,
                                                      "strike": m.strike, "close_ts": m.close_ts})
                print(t, m.result)


if __name__ == "__main__":
    main()
