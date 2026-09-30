"""python -m kalshi record | analyze | resolve"""
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
    args = p.parse_args(argv)
    data = Path(args.data)

    if args.cmd == "record":
        Recorder(Kalshi(), Coinbase(), data, window_s=args.window).run(poll_s=args.poll, idle_poll_s=5.0)
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
