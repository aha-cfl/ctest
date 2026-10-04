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
    args = p.parse_args(argv)
    data = Path(args.data)

    if args.cmd == "record":
        Recorder(Kalshi(), Coinbase(), data, window_s=args.window).run(poll_s=args.poll, idle_poll_s=5.0)
    elif args.cmd == "dashboard":
        from .dashboard import serve
        serve(data, args.host, args.port)
    elif args.cmd == "export":
        from .dashboard import export
        print(export(data, Path(args.out), not args.fragment, args.label))
    elif args.cmd == "paper-trade":
        from .execution import PaperExecutor
        from .trader import Rule, Trader
        rule = Rule(args.rule, args.lo, args.hi, args.min_edge, args.max_t_rem, args.contracts)
        Trader(Kalshi(), Coinbase(), PaperExecutor(), data, rule, max_trades=args.max_trades).run()
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
