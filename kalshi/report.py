"""Everything read-only over the data dir: per-book trade stats, the real-money gate, the
snapshot backtest, and the dashboard state.

Each market counts ONCE per strategy. Snapshots 2s apart in one market are not independent
trials; counting them all would fake statistical significance.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .desk import read_jsonl
from .model import taker_fee_per_contract

GATE_MIN_TRADES = 200


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def books(data_dir: Path) -> list[str]:
    """Book names: from the running desk's status.json, plus any folder holding trades (taker first)."""
    d = Path(data_dir)
    names: set[str] = set()
    if (d / "status.json").exists():
        names |= set(json.loads((d / "status.json").read_text()).get("books", {}))
    if d.exists():
        names |= {p.name for p in d.iterdir() if p.is_dir() and (p / "trades.jsonl").exists()}
    return sorted(names, key=lambda n: (n != "taker", n))


def latest_trades(book_dir: Path) -> list[dict]:
    """One row per market: the settled row supersedes the open row."""
    latest = {r["ticker"]: r for r in read_jsonl(Path(book_dir) / "trades.jsonl")}
    return sorted(latest.values(), key=lambda r: r["ts"])


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = sum(r["won"] for r in rows)
    contracts = sum(r["count"] for r in rows)
    staked = sum(r["count"] * r["price"] + r["fee"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    lo, hi = wilson(wins, n)
    return {"n": n, "wins": wins, "win_rate": wins / n, "ci_low": lo, "ci_high": hi,
            "breakeven": staked / contracts,          # cost per contract = win rate needed
            "pnl": pnl, "staked": staked, "roi": pnl / staked if staked else 0.0,
            "pred_edge": sum(r["edge"] for r in rows) / n, "real_edge": pnl / contracts}


# ---- snapshots vs outcomes -----------------------------------------------------------------
def load_snapshots(data_dir: Path) -> tuple[dict[str, list[dict]], dict[str, str]]:
    outcomes = {o["ticker"]: o["result"] for o in read_jsonl(Path(data_dir) / "outcomes.jsonl")}
    by_ticker: dict[str, list[dict]] = {}
    for s in read_jsonl(Path(data_dir) / "snapshots.jsonl"):
        if s["ticker"] in outcomes:
            by_ticker.setdefault(s["ticker"], []).append(s)
    for rows in by_ticker.values():
        rows.sort(key=lambda r: -r["t_rem"])   # most time left first
    return by_ticker, outcomes


def calibration(by_ticker, outcomes, max_t_rem: float = 120.0) -> dict:
    """First two-sided snapshot per market at or under max_t_rem. Brier score: lower is better."""
    pts = []
    for ticker, snaps in by_ticker.items():
        s = next((x for x in snaps if x["t_rem"] <= max_t_rem
                  and x["yes_bid"] is not None and x["yes_ask"] is not None), None)
        if s:
            pts.append((s["fair_yes"], (s["yes_bid"] + s["yes_ask"]) / 2, 1.0 if outcomes[ticker] == "yes" else 0.0))
    if not pts:
        return {"n": 0}
    brier = lambda i: sum((p[i] - p[2]) ** 2 for p in pts) / len(pts)
    buckets: dict[int, list] = {}
    for f, _, y in pts:
        c = buckets.setdefault(min(int(f * 10), 9), [0, 0.0, 0.0])
        c[0] += 1; c[1] += f; c[2] += y
    table = [(f"{b/10:.1f}-{(b+1)/10:.1f}", c[0], c[1] / c[0], c[2] / c[0]) for b, c in sorted(buckets.items())]
    return {"n": len(pts), "brier_model": brier(0), "brier_market": brier(1), "table": table}


def backtest(data_dir: Path, lo=0.78, hi=0.85, min_edge=0.02, contracts=10, max_t_rem=120.0,
             use_model=True) -> dict:
    """Replay snapshots: first qualifying snapshot per market, taker fill at the ask."""
    by_ticker, outcomes = load_snapshots(data_dir)
    rows = []
    for ticker, snaps in by_ticker.items():
        for s in snaps:
            if s["t_rem"] > max_t_rem:
                continue
            cands = [("yes", s["yes_ask"], s["fair_yes"], s["no_bid_size"]),
                     ("no", s["no_ask"], 1 - s["fair_yes"], s["yes_bid_size"])]
            pick = next(((side, ask, taker_fee_per_contract(ask, contracts)) for side, ask, fair, depth in cands
                         if ask is not None and lo <= ask <= hi and depth >= 1
                         and (not use_model or fair - ask - taker_fee_per_contract(ask, contracts) >= min_edge)), None)
            if pick:
                side, ask, fee = pick
                won = outcomes[ticker] == side
                rows.append({"won": won, "count": 1, "price": ask, "fee": fee, "edge": 0.0,
                             "pnl": (1.0 if won else 0.0) - ask - fee})
                break
    return stats(rows)


# ---- the real-money gate -------------------------------------------------------------------
def gate(data_dir: Path, book: str) -> dict:
    """GO only if: 200+ trades, win-rate CI above breakeven, model beats market, both halves profitable."""
    rows = [r for r in latest_trades(Path(data_dir) / book) if r["status"] == "settled"]
    s = stats(rows)
    cal = calibration(*load_snapshots(data_dir))
    half = len(rows) // 2
    h1, h2 = stats(rows[:half]), stats(rows[half:])
    checks = {
        "trades": {"ok": s["n"] >= GATE_MIN_TRADES, "value": s["n"], "need": GATE_MIN_TRADES},
        "ci_above_breakeven": {"ok": bool(s["n"]) and s["ci_low"] > s["breakeven"],
                               "value": s.get("ci_low"), "need": s.get("breakeven")},
        "model_beats_market": {"ok": cal["n"] > 0 and cal["brier_model"] < cal["brier_market"],
                               "value": cal.get("brier_model"), "need": cal.get("brier_market")},
        "both_halves_profitable": {"ok": h1["n"] > 0 and h2["n"] > 0 and h1["pnl"] > 0 and h2["pnl"] > 0,
                                   "value": [h1.get("pnl"), h2.get("pnl")], "need": "> 0 each"},
    }
    return {"go": all(c["ok"] for c in checks.values()), "checks": checks}


# ---- text reports ----------------------------------------------------------------------------
def _fv(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    if isinstance(v, list):
        return "[" + ", ".join(_fv(x) for x in v) + "]"
    return "n/a" if v is None else str(v)


def _line(name: str, s: dict) -> str:
    if not s["n"]:
        return f"  {name:<16} n=   0"
    return (f"  {name:<16} n={s['n']:>4}  win {s['win_rate']:.1%} (CI {s['ci_low']:.1%}-{s['ci_high']:.1%})  "
            f"breakeven {s['breakeven']:.1%}  P&L ${s['pnl']:+.2f}  "
            f"edge pred {s['pred_edge'] * 100:+.1f}c / real {s['real_edge'] * 100:+.1f}c")


def _bucket(rows, key, edges, fmt) -> list[str]:
    out = [_line(fmt(a, b), stats(sel)) for a, b in zip(edges, edges[1:])
           if (sel := [r for r in rows if r.get(key) is not None and a <= r[key] < b])]
    missing = [r for r in rows if r.get(key) is None]
    return out + ([_line("n/a", stats(missing))] if missing else [])


def trades_report(data_dir: Path) -> str:
    out = []
    for book in books(data_dir) or ["taker"]:
        rows = [r for r in latest_trades(Path(data_dir) / book) if r["status"] == "settled"]
        out.append(f"=== {book.upper()}")
        if not rows:
            out.append("  no settled trades yet")
        else:
            out.append(_line("all", stats(rows)))
            out.append("BY VOL RATIO (market / realized)")
            out += _bucket(rows, "vol_ratio", [0, 0.8, 1.2, 99], lambda a, b: f"{a}-{b}" if b < 99 else f">= {a}")
            out.append("BY SECONDS LEFT AT ENTRY")
            out += _bucket(rows, "t_rem", [0, 30, 60, 90, 121], lambda a, b: f"{a}-{b}s")
            out.append("BY ENTRY PRICE")
            out += _bucket(rows, "price", [0.0, 0.78, 0.80, 0.82, 0.86], lambda a, b: f"{a:.2f}-{b:.2f}")
            out.append("BY SIDE")
            out += [_line(f"side={v}", stats([r for r in rows if r["side"] == v])) for v in ("yes", "no")]
        g = gate(data_dir, book)
        out.append(f"GATE: {'GO' if g['go'] else 'NO-GO'}")
        out += [f"  [{'x' if c['ok'] else ' '}] {k}: {_fv(c['value'])} (need {_fv(c['need'])})" for k, c in g["checks"].items()]
        out.append("")
    return "\n".join(out).rstrip()


def backtest_report(data_dir: Path, lo=0.78, hi=0.85, min_edge=0.02, contracts=10, max_t_rem=120.0) -> str:
    by_ticker, _ = load_snapshots(data_dir)
    lines = [f"markets with snapshots+outcome: {len(by_ticker)} (band {lo:.2f}-{hi:.2f}, entry t<={max_t_rem:g}s, "
             f"fee @ {contracts} contracts)"]
    for name, use_model in (("favorite (blind)", False), (f"model edge>={min_edge:.2f}", True)):
        s = backtest(data_dir, lo, hi, min_edge, contracts, max_t_rem, use_model)
        verdict = ("insufficient data (<100 markets)" if s["n"] < 100 else
                   "EDGE: CI lower bound above breakeven" if s["ci_low"] > s["breakeven"] else
                   "NEGATIVE: CI upper bound below breakeven" if s["ci_high"] < s["breakeven"] else
                   "inconclusive: CI straddles breakeven")
        lines += [_line(name, s), f"    -> {verdict}"]
    cal = calibration(*load_snapshots(data_dir), max_t_rem)
    if cal["n"]:
        better = "model better" if cal["brier_model"] < cal["brier_market"] else "market better"
        lines.append(f"calibration (n={cal['n']}): Brier model {cal['brier_model']:.4f} vs market {cal['brier_market']:.4f} ({better})")
        lines += [f"  {b:>9} n={n:3d} fair {f:.3f} realized {y:.3f}" for b, n, f, y in cal["table"]]
    return "\n".join(lines)


# ---- dashboard state -------------------------------------------------------------------------
# Most specific first; "outside band" last so a side that was in band reports its real blocker.
SKIP_KINDS = [("edge", "no model edge"), ("basis guard", "near strike"), ("vol ratio", "vol filter"),
              ("spread", "wide spread"), ("vol spike", "vol spike"), ("news blackout", "news blackout"),
              ("daily loss stop", "daily loss stop"), ("kill switch", "kill switch"), ("size 0", "Kelly size 0"),
              ("one-sided", "one-sided book"), ("empty book", "empty book"), ("outside band", "price outside band")]


def skip_tally(events: list[dict], book: str | None = None) -> list[list]:
    counts: dict[str, int] = {}
    for e in events:
        msg = e.get("msg", "")
        if e.get("kind") != "skip" or "skip:" not in msg or (book and not msg.startswith(f"[{book}]")):
            continue
        reasons = msg.split("skip:", 1)[1]
        label = next((lbl for needle, lbl in SKIP_KINDS if needle in reasons), None)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return sorted(([k, v] for k, v in counts.items()), key=lambda kv: -kv[1])


def book_state(data_dir: Path, book: str, events: list[dict]) -> dict:
    trades = latest_trades(Path(data_dir) / book)
    settled = [t for t in trades if t["status"] == "settled"]
    s = stats(settled)
    cum, curve = 0.0, []
    for t in settled:
        cum += t["pnl"]
        curve.append({"ts": t["close_ts"], "pnl": round(cum, 4), "won": t["won"]})
    n = s["n"]
    return {
        "summary": {"settled": n, "open": len(trades) - n, "wins": s.get("wins", 0), "losses": n - s.get("wins", 0),
                    "win_rate": s.get("win_rate"), "breakeven": s.get("breakeven"),
                    "avg_price": sum(t["price"] for t in settled) / n if n else None,
                    "pnl": round(cum, 4), "staked": round(s.get("staked", 0.0), 4), "roi": s.get("roi"),
                    "avg_edge": s.get("pred_edge")},
        "curve": curve, "trades": list(reversed(trades)), "gate": gate(data_dir, book),
        "skips": skip_tally(events, book),
    }


def build_state(data_dir: Path, events_tail: int = 150, label: str | None = None) -> dict:
    d = Path(data_dir)
    events = read_jsonl(d / "events.jsonl")
    status_path = d / "status.json"
    return {"generated": time.time(), "label": label,
            "status": json.loads(status_path.read_text()) if status_path.exists() else None,
            "books": {b: book_state(d, b, events) for b in books(d) or ["taker"]},
            "events": list(reversed(events[-events_tail:]))}
