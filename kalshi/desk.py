"""The desk: one loop, one market read per tick, several paper books deciding from the same view.

Per tick:
  1. settle closed markets (one Kalshi call per closed ticker, shared by every book)
  2. pick the open market; outside the final `max_t_rem` seconds, wait
  3. build ONE MarketView: composite spot, locked part of the 60s settlement average, fair value,
     order book, implied vs realized vol, expected-settlement gap -> append to snapshots.jsonl
  4. each PaperBook (taker, maker) decides from that view; one book's error never stops another

Data dir layout:
  snapshots.jsonl outcomes.jsonl events.jsonl status.json KILL
  <book>/trades.jsonl <book>/risk.json [<book>/maker_cancels.jsonl]

Restarts are cheap: books reload trades (open positions still settle) and the locked-average
samples for the current market are rebuilt from snapshots.jsonl, so fair value stays accurate.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .feeds import Book, Kalshi, Market
from .model import WINDOW_S, annualize, fair_yes, implied_sigma, sigma_per_second, taker_fee_per_contract
from .rules import FilterConfig, RiskBook, Rule, news, side_blocker, spread, vol_spike

MAKER_FEE_RATE = 0.0175   # Kalshi maker fee where charged; verify the current schedule before live use
CANCEL_AT_T_REM = 5.0


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


@dataclass
class Fill:
    order_id: str
    side: str          # "yes" | "no"
    count: int
    price: float       # per contract, dollars
    fee: float         # total dollars

    @property
    def cost(self) -> float:
        return self.count * self.price + self.fee


@dataclass
class MarketView:
    market: Market
    t_rem: float
    spot: float
    locked_mean: float | None
    fair_yes: float
    book: Book
    gap: float                    # expected settlement value - strike
    vol_realized: float           # annualized
    vol_implied: float | None
    vol_ratio: float | None
    venues: str
    vol_spike: float | None
    market_block: str | None      # news / vol spike / spread reason, shared by all books

    @property
    def note(self) -> str:
        if self.vol_implied:
            return f"vol mkt {self.vol_implied:.0%} vs real {self.vol_realized:.0%}"
        return f"vol real {self.vol_realized:.0%}"


# ---- one paper book ------------------------------------------------------------------------
@dataclass
class PaperBook:
    """One execution style. taker: fill now at the ask. maker: rest a bid under the ask, fill only
    if a later ask trades *through* it, cancel when the model edge is gone or at t-5s."""
    name: str
    rule: Rule = field(default_factory=Rule)
    execution: str = "taker"
    risk: RiskBook | None = None
    max_trades: int = 10**9
    dir: Path | None = None
    emit: Callable[[str, str], None] = lambda kind, msg: None
    open_trades: dict[str, dict] = field(default_factory=dict)
    settled: list[dict] = field(default_factory=list)
    traded: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)
    resting: dict[str, dict] = field(default_factory=dict)
    last_skip: tuple[str, float] = ("", -1e18)

    @property
    def maxed(self) -> bool:
        return len(self.traded) >= self.max_trades

    @property
    def done(self) -> bool:
        return len(self.settled) >= self.max_trades and not self.open_trades and not self.resting

    def wants(self, ticker: str) -> bool:
        if self.maxed or ticker in self.traded:
            return False
        return ticker not in self.attempted or ticker in self.resting

    def resume(self) -> None:
        latest = {r["ticker"]: r for r in read_jsonl(self.dir / "trades.jsonl")}
        for ticker, r in latest.items():
            self.traded.add(ticker)
            if r["status"] == "settled":
                self.settled.append(r)
            else:
                self.open_trades[ticker] = r

    def _say(self, kind: str, msg: str) -> None:
        self.emit(kind, f"[{self.name}] {msg}")

    # decide ----------------------------------------------------------------------------------
    def step(self, v: MarketView, now: float) -> None:
        m = v.market
        if m.ticker in self.resting:
            self._work_resting(v, now)
            return
        blocked = self.risk.blocked(now) if self.risk else None
        notes = []
        for side, ask, fair, depth in (("yes", v.book.yes_ask, v.fair_yes, v.book.no_bid_size),
                                       ("no", v.book.no_ask, 1 - v.fair_yes, v.book.yes_bid_size)):
            if ask is None:
                continue
            note = side_blocker(self.rule, side, ask, fair, v.gap, v.vol_ratio) or v.market_block or blocked
            if note is None and self.risk:
                fixed = self.rule.contracts if self.rule.mode == "favorite" else None
                count, why = self.risk.size(fair, ask, fixed=fixed)
                note = None if count >= 1 else f"size 0 ({why})"
            elif note is None:
                count = self.rule.contracts
            if note:
                notes.append(note)
                continue
            if self.execution == "maker":
                self._place_resting(v, side, ask, fair, count, now)
            else:
                self._take(v, side, ask, fair, count, depth, now)
            return
        if self.last_skip[0] == m.ticker and now - self.last_skip[1] < 15:
            return
        self.last_skip = (m.ticker, now)
        self._say("skip", f"{m.ticker} t-{v.t_rem:5.1f}s spot {v.spot:,.2f} vs strike {m.strike:,.2f} | "
                          f"fair YES {v.fair_yes:.3f} | {v.note} | skip: {'; '.join(notes) or 'empty book'}")

    def _take(self, v: MarketView, side, ask, fair, count, depth, now) -> None:
        n = int(min(count, depth))
        if n < 1:
            self._say("info", f"{v.market.ticker}: order not filled")
            return
        fill = Fill(f"paper-{uuid.uuid4().hex[:10]}", side, n, ask, taker_fee_per_contract(ask, n) * n)
        self._record_fill(v, fill, fair, now, {"execution": "taker"})

    def _record_fill(self, v: MarketView, fill: Fill, fair: float, now: float, extra: dict) -> None:
        m = v.market
        edge = fair - fill.price - fill.fee / fill.count
        trade = {"ts": now, "ticker": m.ticker, "close_ts": m.close_ts, "strike": m.strike, "spot": v.spot,
                 "t_rem": round(v.t_rem, 1), "fair": round(fair, 4), "edge": round(edge, 4),
                 "rule": self.rule.mode, "status": "open", **asdict(fill), **extra,
                 "gap": round(v.gap, 2), "vol_realized": round(v.vol_realized, 4),
                 "vol_implied": round(v.vol_implied, 4) if v.vol_implied else None,
                 "vol_ratio": round(v.vol_ratio, 3) if v.vol_ratio else None,
                 "venues": v.venues, "vol_spike": round(v.vol_spike, 2) if v.vol_spike else None}
        self.open_trades[m.ticker] = trade
        self.traded.add(m.ticker)
        append_jsonl(self.dir / "trades.jsonl", trade)
        self._say("buy", f">>> BUY {fill.side.upper()} x{fill.count} {m.ticker} @ {fill.price:.2f} "
                         f"(fee ${fill.fee:.2f}, cost ${fill.cost:.2f}) | fair {fair:.3f} edge {edge:+.3f} | "
                         f"t-{v.t_rem:.0f}s spot {v.spot:,.2f} strike {m.strike:,.2f} | order {fill.order_id}")

    def _place_resting(self, v: MarketView, side, ask, fair, count, now) -> None:
        cap = ask - 0.01                                   # always improve on the ask
        if self.rule.mode == "model":                      # never bid above fair - edge - fee
            cap = min(cap, fair - self.rule.min_edge - taker_fee_per_contract(ask, count, MAKER_FEE_RATE))
        limit = math.floor(cap * 100 + 1e-9) / 100
        if limit < 0.01:
            self._say("info", f"{v.market.ticker}: maker limit would be {limit:.2f}, not placing")
            return
        self.resting[v.market.ticker] = {"side": side, "limit": limit, "count": count, "placed_ts": now,
                                         "ask_at_place": ask, "fair": fair}
        self.attempted.add(v.market.ticker)
        self._say("order", f"=== REST BID {side.upper()} x{count} {v.market.ticker} @ {limit:.2f} "
                           f"(ask {ask:.2f}, fair {fair:.3f}) | t-{v.t_rem:.0f}s")

    def _edge_gone(self, o: dict, fair_yes: float) -> bool:
        fair = fair_yes if o["side"] == "yes" else 1 - fair_yes
        return fair - o["limit"] - taker_fee_per_contract(o["limit"], o["count"], MAKER_FEE_RATE) < self.rule.min_edge

    def _work_resting(self, v: MarketView, now: float) -> None:
        t = v.market.ticker
        o = self.resting[t]
        ask = v.book.yes_ask if o["side"] == "yes" else v.book.no_ask
        fair = v.fair_yes if o["side"] == "yes" else 1 - v.fair_yes
        if ask is not None and ask < o["limit"] - 1e-9:       # a seller crossed below our bid: we'd be hit first
            del self.resting[t]
            n = o["count"]
            fill = Fill(f"paper-maker-{t[-6:]}", o["side"], n, o["limit"],
                        taker_fee_per_contract(o["limit"], n, MAKER_FEE_RATE) * n)
            self._record_fill(v, fill, fair, now, {
                "execution": "maker", "ask_at_place": o["ask_at_place"],
                "improvement": round(o["ask_at_place"] - o["limit"], 4), "wait_s": round(now - o["placed_ts"], 1)})
        elif self.rule.mode == "model" and self._edge_gone(o, v.fair_yes):
            del self.resting[t]
            append_jsonl(self.dir / "maker_cancels.jsonl", {"ts": now, "ticker": t, "why": "edge gone", **o})
            self._say("cancel", f"xxx CANCEL {o['side'].upper()} bid @ {o['limit']:.2f} on {t}: edge gone (fair now {fair:.3f})")
        elif v.t_rem <= CANCEL_AT_T_REM:
            del self.resting[t]
            append_jsonl(self.dir / "maker_cancels.jsonl", {"ts": now, "ticker": t, **o})
            self._say("cancel", f"xxx CANCEL {o['side'].upper()} bid @ {o['limit']:.2f} on {t}: "
                                f"not filled by t-{CANCEL_AT_T_REM:.0f}s")

    def settle(self, m: Market) -> None:
        t = self.open_trades.pop(m.ticker, None)
        if t is None:
            return
        won = m.result == t["side"]
        payout = float(t["count"]) if won else 0.0
        pnl = payout - t["count"] * t["price"] - t["fee"]
        row = {**t, "status": "settled", "result": m.result, "won": won, "payout": payout, "pnl": round(pnl, 4),
               "return_pct": round(100 * pnl / (t["count"] * t["price"] + t["fee"]), 2)}
        append_jsonl(self.dir / "trades.jsonl", row)
        self.settled.append(row)
        if self.risk:
            self.risk.record(t["close_ts"], pnl)
        self._say("win" if won else "loss", f"<<< SETTLED {m.ticker}: result {m.result.upper()} -> "
                  f"{'WIN' if won else 'LOSS'} payout ${payout:.2f} | P&L ${pnl:+.2f} ({row['return_pct']:+.1f}%)")


# ---- the desk ------------------------------------------------------------------------------
@dataclass
class Desk:
    kalshi: Kalshi
    spot_src: object                 # CompositeSpot or Coinbase: .spot(), .closes_1m()
    out_dir: Path
    books: list[PaperBook]
    filters: FilterConfig = field(default_factory=FilterConfig)
    log: Callable[[str], None] = print
    spot_samples: dict[str, list[float]] = field(default_factory=dict)   # ticker -> spots in final 60s
    pending: dict[str, float] = field(default_factory=dict)              # ticker -> close_ts, outcome unknown
    sigma: float | None = None
    sigma_ts: float = -1e18
    spike: tuple = (None, None)
    in_window: bool = False
    last_status: str = ""
    now: float = 0.0
    cur: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        for b in self.books:
            b.dir = b.dir or self.out_dir / b.name
            b.emit = self._emit

    @property
    def max_t_rem(self) -> float:
        return max(b.rule.max_t_rem for b in self.books)

    @property
    def done(self) -> bool:
        return all(b.done for b in self.books)

    def _emit(self, kind: str, msg: str) -> None:
        self.log(msg)
        append_jsonl(self.out_dir / "events.jsonl", {"ts": self.now, "kind": kind, "msg": msg})

    def _say(self, msg: str) -> None:
        self.cur["msg"] = msg
        if msg != self.last_status:
            self._emit("info", msg)
            self.last_status = msg

    def resume(self, now: float | None = None) -> "Desk":
        """Reload books; rebuild locked-average samples and pending outcomes from disk."""
        now = time.time() if now is None else now
        for b in self.books:
            b.resume()
        done = {o["ticker"] for o in read_jsonl(self.out_dir / "outcomes.jsonl")}
        rebuilt = 0
        for s in read_jsonl(self.out_dir / "snapshots.jsonl"):
            if s["ticker"] not in done:
                self.pending[s["ticker"]] = s["close_ts"]
            if s["close_ts"] > now and s["t_rem"] <= WINDOW_S:
                self.spot_samples.setdefault(s["ticker"], []).append(s["spot"])
                rebuilt += 1
        self.now = now
        opened = sum(len(b.open_trades) for b in self.books)
        self._emit("info", f"desk started: {len(self.books)} book(s), {opened} open position(s) reloaded, "
                           f"{rebuilt} locked-average sample(s) rebuilt")
        return self

    def _write_status(self) -> None:
        status = {"ts": self.now, "mode": "paper", "rule": asdict(self.books[0].rule), "in_window": self.in_window,
                  "done": self.done, "settled": sum(len(b.settled) for b in self.books),
                  "open": sum(len(b.open_trades) for b in self.books),
                  "books": {b.name: {"execution": b.execution, "settled": len(b.settled), "open": len(b.open_trades),
                                     "resting": len(b.resting), "max_trades": b.max_trades,
                                     "risk": None if b.risk is None else {
                                         "bankroll": round(b.risk.bankroll, 2), "day_pnl": round(b.risk.day_pnl, 2),
                                         "daily_loss_cap": b.risk.cfg.daily_loss_cap,
                                         "per_trade_cap": b.risk.cfg.per_trade_cap}} for b in self.books},
                  **self.cur}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.out_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status))
        os.replace(tmp, self.out_dir / "status.json")

    def tick(self, now: float) -> None:
        self.now, self.cur, self.in_window = now, {"msg": self.cur.get("msg", "")}, False
        try:
            self._tick(now)
        finally:
            if self.done:
                pnl = sum(r["pnl"] for b in self.books for r in b.settled)
                self.cur = {"msg": f"Finished: {sum(len(b.settled) for b in self.books)} trade(s) settled, P&L ${pnl:+.2f}"}
            self._write_status()

    def _resolve(self, now: float) -> None:
        closed = {t for t, c in self.pending.items() if c <= now}
        closed |= {t for b in self.books for t, tr in b.open_trades.items() if tr["close_ts"] <= now}
        for ticker in sorted(closed):
            m = self.kalshi.market(ticker)
            if m.result not in ("yes", "no"):
                continue
            if ticker in self.pending:
                append_jsonl(self.out_dir / "outcomes.jsonl",
                             {"ticker": ticker, "result": m.result, "strike": m.strike, "close_ts": m.close_ts})
                del self.pending[ticker]
            self.spot_samples.pop(ticker, None)
            for b in self.books:
                try:
                    b.settle(m)
                except Exception as e:
                    self._emit("error", f"[{b.name}] settle error: {e!r}")

    def _tick(self, now: float) -> None:
        self._resolve(now)
        active = [b for b in self.books if not b.maxed]
        if not active:
            if any(b.open_trades for b in self.books):
                self._say("all books at max trades; waiting for open positions to settle")
            return
        markets = [m for m in self.kalshi.open_markets() if m.close_ts > now]
        if not markets:
            self._say("no open KXBTC15M market")
            return
        m = markets[0]
        t_rem = m.close_ts - now
        self.cur.update(ticker=m.ticker, close_ts=m.close_ts, strike=m.strike, t_rem=round(t_rem, 1))
        needing = [b for b in active if b.wants(m.ticker)]
        if m.strike is None or not needing:
            return
        if t_rem > self.max_t_rem:
            close = datetime.fromtimestamp(m.close_ts, timezone.utc).strftime("%H:%M:%S")
            self._say(f"{m.ticker}: closes {close} UTC, waiting for final {self.max_t_rem:.0f}s")
            return

        self.in_window = True
        v = self._view(m, t_rem, now)
        for b in needing:
            try:
                b.step(v, now)
            except Exception as e:  # one book's bug must not stop the others
                self._emit("error", f"[{b.name}] error: {e!r}")

    def _view(self, m: Market, t_rem: float, now: float) -> MarketView:
        spot = self.spot_src.spot()
        if t_rem <= WINDOW_S:
            self.spot_samples.setdefault(m.ticker, []).append(spot)
        if self.sigma is None or now - self.sigma_ts >= 60:
            closes = self.spot_src.closes_1m(60)
            self.sigma, self.sigma_ts = sigma_per_second(closes), now
            self.spike = vol_spike(closes, self.filters)
        locked = self.spot_samples.get(m.ticker)
        locked_mean = sum(locked) / len(locked) if locked else None
        fy = fair_yes(spot, m.strike, t_rem, self.sigma, locked_mean)
        book = self.kalshi.book(m.ticker)
        k = max(0.0, WINDOW_S - t_rem)          # expected settlement: locked part + spot for the rest
        expected = (k * locked_mean + (WINDOW_S - k) * spot) / WINDOW_S if locked_mean is not None else spot
        mid = (book.yes_bid + book.yes_ask) / 2 if book.yes_bid is not None and book.yes_ask is not None else None
        iv = implied_sigma(mid, spot, m.strike, t_rem, locked_mean) if mid is not None else None
        rv_ann, iv_ann = annualize(self.sigma), (annualize(iv) if iv else None)
        v = MarketView(m, t_rem, spot, locked_mean, fy, book, expected - m.strike, rv_ann, iv_ann,
                       iv_ann / rv_ann if iv_ann and rv_ann else None, getattr(self.spot_src, "venues", "coinbase"),
                       self.spike[1], news(now, m.close_ts, self.filters) or self.spike[0] or spread(book, self.filters))
        self.cur.update(spot=spot, fair_yes=round(fy, 4), yes_bid=book.yes_bid, yes_ask=book.yes_ask,
                        no_ask=book.no_ask, gap=round(v.gap, 2), vol_realized=round(rv_ann, 4),
                        vol_implied=round(iv_ann, 4) if iv_ann else None,
                        vol_ratio=round(v.vol_ratio, 3) if v.vol_ratio else None, venues=v.venues,
                        vol_spike=round(v.vol_spike, 2) if v.vol_spike else None)
        append_jsonl(self.out_dir / "snapshots.jsonl", {
            "ts": now, "ticker": m.ticker, "close_ts": m.close_ts, "t_rem": round(t_rem, 2), "strike": m.strike,
            "spot": spot, "locked_mean": locked_mean, "sigma_s": self.sigma, "fair_yes": round(fy, 5),
            **asdict(book), "yes_ask": book.yes_ask, "no_ask": book.no_ask, "vol_implied": self.cur["vol_implied"],
            "vol_ratio": self.cur["vol_ratio"], "venues": v.venues})
        self.pending[m.ticker] = m.close_ts
        return v

    def run(self, poll_s: float = 2.0, idle_s: float = 5.0, every: Callable[[], None] | None = None,
            every_s: float = 30.0) -> None:
        """Loop forever (until every book is done). `every` runs every `every_s` seconds (dashboard export)."""
        last_every = 0.0
        while not self.done:
            try:
                self.tick(time.time())
            except Exception as e:  # network blips: log and keep going
                self._emit("error", f"[desk] error: {e!r}")
            if every and time.time() - last_every >= every_s:
                try:
                    every()
                except Exception as e:
                    self._emit("error", f"[desk] export error: {e!r}")
                last_every = time.time()
            final_minute = self.in_window and (self.cur.get("t_rem") or 999) <= WINDOW_S
            resting = any(b.resting for b in self.books)
            busy = self.in_window or any(b.open_trades for b in self.books)
            time.sleep(1.0 if final_minute or resting else poll_s if busy else idle_s)
        self.log(f"[desk] done: {sum(len(b.settled) for b in self.books)} trade(s) settled")
