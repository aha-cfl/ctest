"""Decide -> buy -> wait for settlement -> report. Stops after `max_trades` settled trades.

Rules:
  model    : buy the side whose ask is in [lo, hi] AND model fair - ask - fee >= min_edge
  favorite : buy the side whose ask is in [lo, hi] (the blind strategy; for demos only)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .client import Coinbase, Kalshi
from .model import WINDOW_S, fair_yes, sigma_per_second, taker_fee_per_contract
from .recorder import append_jsonl


@dataclass
class Rule:
    mode: str = "model"
    lo: float = 0.78
    hi: float = 0.85
    min_edge: float = 0.02
    max_t_rem: float = 120.0
    contracts: int = 1


@dataclass
class Trader:
    kalshi: Kalshi
    spot_src: Coinbase
    executor: object
    out_dir: Path
    rule: Rule = field(default_factory=Rule)
    max_trades: int = 1
    log: callable = print
    open_trades: dict[str, dict] = field(default_factory=dict)
    settled: list[dict] = field(default_factory=list)
    traded_tickers: set[str] = field(default_factory=set)
    spot_samples: dict[str, list[float]] = field(default_factory=dict)
    sigma: float | None = None
    sigma_ts: float = -1e18
    last_status: str = ""
    in_window: bool = False
    last_skip: tuple[str, float] = ("", -1e18)
    now: float = 0.0
    cur: dict = field(default_factory=dict)   # live view of the market being evaluated

    @property
    def trades_path(self) -> Path:
        return self.out_dir / "trades.jsonl"

    @property
    def done(self) -> bool:
        return len(self.settled) >= self.max_trades and not self.open_trades

    def _emit(self, kind: str, msg: str) -> None:
        """Console + events.jsonl (the dashboard's activity feed)."""
        self.log(msg)
        append_jsonl(self.out_dir / "events.jsonl", {"ts": self.now, "kind": kind, "msg": msg})

    def _say(self, msg: str, kind: str = "info") -> None:
        self.cur["msg"] = msg
        if msg != self.last_status:
            self._emit(kind, msg)
            self.last_status = msg

    def _write_status(self) -> None:
        status = {"ts": self.now, "mode": "paper", "rule": asdict(self.rule), "max_trades": self.max_trades,
                  "settled": len(self.settled), "open": len(self.open_trades), "done": self.done,
                  "in_window": self.in_window, **self.cur}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.out_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status))
        os.replace(tmp, self.out_dir / "status.json")

    def tick(self, now: float) -> None:
        self.now, self.cur = now, {"msg": self.cur.get("msg", "")}
        try:
            self._tick(now)
        finally:
            if self.done:
                pnl = sum(r["pnl"] for r in self.settled)
                self.cur = {"msg": f"Finished: {len(self.settled)} trade(s) settled, P&L ${pnl:+.2f}"}
            self._write_status()

    def _tick(self, now: float) -> None:
        self.in_window = False
        self._settle()
        if len(self.traded_tickers) >= self.max_trades:
            if self.open_trades:
                t = next(iter(self.open_trades.values()))
                self._say(f"waiting for settlement of {t['ticker']} ({t['side'].upper()} x{t['count']} @ {t['price']:.2f})")
            return

        markets = [m for m in self.kalshi.open_markets() if m.close_ts > now]
        if not markets:
            self._say("no open KXBTC15M market")
            return
        m = markets[0]
        t_rem = m.close_ts - now
        self.cur.update(ticker=m.ticker, close_ts=m.close_ts, strike=m.strike, t_rem=round(t_rem, 1))
        if m.ticker in self.traded_tickers or m.strike is None:
            return
        if t_rem > self.rule.max_t_rem:
            close = datetime.fromtimestamp(m.close_ts, timezone.utc).strftime("%H:%M:%S")
            self._say(f"{m.ticker}: closes {close} UTC, waiting for final {self.rule.max_t_rem:.0f}s")
            return

        self.in_window = True
        spot = self.spot_src.spot()
        if t_rem <= WINDOW_S:
            self.spot_samples.setdefault(m.ticker, []).append(spot)
        locked = self.spot_samples.get(m.ticker)
        if self.sigma is None or now - self.sigma_ts >= 60:
            self.sigma, self.sigma_ts = sigma_per_second(self.spot_src.closes_1m(60)), now
        fy = fair_yes(spot, m.strike, t_rem, self.sigma, sum(locked) / len(locked) if locked else None)
        book = self.kalshi.book(m.ticker)
        self.cur.update(spot=spot, fair_yes=round(fy, 4), yes_ask=book.yes_ask, no_ask=book.no_ask)

        sides = [("yes", book.yes_ask, fy, book.no_bid_size), ("no", book.no_ask, 1 - fy, book.yes_bid_size)]
        notes = []
        for side, ask, fair, depth in sides:
            if ask is None:
                continue
            fee = taker_fee_per_contract(ask, self.rule.contracts)
            edge = fair - ask - fee
            if not self.rule.lo <= ask <= self.rule.hi:
                notes.append(f"{side} ask {ask:.2f} outside band")
                continue
            if self.rule.mode == "model" and edge < self.rule.min_edge:
                notes.append(f"{side} ask {ask:.2f} fair {fair:.3f} edge {edge:+.3f} < {self.rule.min_edge:.2f}")
                continue
            self._buy(now, m, side, ask, fair, edge, depth, spot, t_rem)
            return
        if self.last_skip[0] == m.ticker and now - self.last_skip[1] < 15:
            return
        self.last_skip = (m.ticker, now)
        self._say(f"{m.ticker} t-{t_rem:5.1f}s spot {spot:,.2f} vs strike {m.strike:,.2f} | "
                  f"fair YES {fy:.3f} | skip: {'; '.join(notes) or 'empty book'}", kind="skip")

    def _buy(self, now, m, side, ask, fair, edge, depth, spot, t_rem) -> None:
        fill = self.executor.buy(m.ticker, side, ask, self.rule.contracts, depth)
        if fill is None:
            self._say(f"{m.ticker}: order not filled")
            return
        trade = {"ts": now, "ticker": m.ticker, "close_ts": m.close_ts, "strike": m.strike, "spot": spot,
                 "t_rem": round(t_rem, 1), "fair": round(fair, 4), "edge": round(edge, 4),
                 "rule": self.rule.mode, "status": "open", **asdict(fill)}
        self.open_trades[m.ticker] = trade
        self.traded_tickers.add(m.ticker)
        append_jsonl(self.trades_path, trade)
        self.last_status = ""
        self._emit("buy", f">>> BUY {side.upper()} x{fill.count} {m.ticker} @ {ask:.2f} (fee ${fill.fee:.2f}, "
                 f"cost ${fill.cost:.2f}) | fair {fair:.3f} edge {edge:+.3f} | t-{t_rem:.0f}s "
                 f"spot {spot:,.2f} strike {m.strike:,.2f} | order {fill.order_id}")

    def _settle(self) -> None:
        for ticker, t in list(self.open_trades.items()):
            m = self.kalshi.market(ticker)
            if m.result not in ("yes", "no"):
                continue
            won = m.result == t["side"]
            payout = float(t["count"]) if won else 0.0
            pnl = payout - t["count"] * t["price"] - t["fee"]
            row = {**t, "status": "settled", "result": m.result, "won": won,
                   "payout": payout, "pnl": round(pnl, 4), "return_pct": round(100 * pnl / (t["count"] * t["price"] + t["fee"]), 2)}
            append_jsonl(self.trades_path, row)
            self.settled.append(row)
            del self.open_trades[ticker]
            self._emit("win" if won else "loss", f"<<< SETTLED {ticker}: result {m.result.upper()} -> {'WIN' if won else 'LOSS'} "
                     f"payout ${payout:.2f} | P&L ${pnl:+.2f} ({row['return_pct']:+.1f}%)")

    def run(self, poll_s: float = 2.0, idle_s: float = 5.0) -> None:
        self.log(f"[trader] rule={self.rule} max_trades={self.max_trades} executor={type(self.executor).__name__}")
        while not self.done:
            try:
                self.tick(time.time())
            except Exception as e:  # keep running through network blips
                self._emit("error", f"[trader] error: {e!r}")
            time.sleep(poll_s if self.in_window or self.open_trades else idle_s)
        total = sum(r["pnl"] for r in self.settled)
        self.log(f"[trader] done: {len(self.settled)} trade(s), total P&L ${total:+.2f}")
