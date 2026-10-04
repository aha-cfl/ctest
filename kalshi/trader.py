"""Decide -> size -> buy (taker) or rest a bid (maker) -> wait for settlement -> report.
Stops after `max_trades` settled trades.

Rules:
  model    : buy the side whose ask is in [lo, hi] AND model fair - ask - fee >= min_edge
  favorite : buy the side whose ask is in [lo, hi] (the blind strategy; for demos only)
Then: basis guard, optional vol-ratio filter, market filters (spread, vol spike, news),
risk limits (kill switch, daily loss stop) and quarter-Kelly sizing with a $ cap.

Execution (paper only):
  taker : fill now at the ask, taker fee
  maker : rest a bid below the ask; fills only if a later ask trades *through* it; cancel at t-5s
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .client import Coinbase, Kalshi
from .execution import Fill
from .filters import FilterConfig, news, spread, vol_spike
from .model import WINDOW_S, annualize, fair_yes, implied_sigma, sigma_per_second, taker_fee_per_contract
from .recorder import append_jsonl
from .risk import RiskBook

MAKER_FEE_RATE = 0.0175   # Kalshi maker fee rate where charged; verify current schedule before live use
CANCEL_AT_T_REM = 5.0


@dataclass
class Rule:
    mode: str = "model"
    lo: float = 0.78
    hi: float = 0.85
    min_edge: float = 0.02
    max_t_rem: float = 120.0
    contracts: int = 1              # used when no RiskBook is attached (tests, demos)
    min_gap_usd: float = 5.0        # skip when expected settlement is this close to strike (venue basis)
    min_vol_ratio: float = 0.0      # 0=off; else buy only if market-implied vol / realized vol >= this
    execution: str = "taker"        # taker | maker


@dataclass
class Trader:
    kalshi: Kalshi
    spot_src: Coinbase
    executor: object
    out_dir: Path
    rule: Rule = field(default_factory=Rule)
    max_trades: int = 1
    log: callable = print
    risk: RiskBook | None = None
    filters: FilterConfig = field(default_factory=FilterConfig)
    notify: Callable[[str], None] | None = None
    open_trades: dict[str, dict] = field(default_factory=dict)
    settled: list[dict] = field(default_factory=list)
    traded_tickers: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)      # maker orders placed (filled or cancelled)
    resting: dict[str, dict] = field(default_factory=dict)
    spot_samples: dict[str, list[float]] = field(default_factory=dict)
    sigma: float | None = None
    sigma_ts: float = -1e18
    spike: tuple = (None, None)
    last_status: str = ""
    in_window: bool = False
    last_skip: tuple[str, float] = ("", -1e18)
    now: float = 0.0
    cur: dict = field(default_factory=dict)   # live view of the market being evaluated
    signal: dict | None = None                # last buy signal (dashboard banner / alerts)

    @property
    def trades_path(self) -> Path:
        return self.out_dir / "trades.jsonl"

    @property
    def done(self) -> bool:
        return len(self.settled) >= self.max_trades and not self.open_trades and not self.resting

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
                  "in_window": self.in_window, "signal": self.signal,
                  "risk": None if self.risk is None else {"bankroll": round(self.risk.bankroll, 2),
                                                          "day_pnl": round(self.risk.day_pnl, 2),
                                                          "daily_loss_cap": self.risk.cfg.daily_loss_cap,
                                                          "per_trade_cap": self.risk.cfg.per_trade_cap},
                  **self.cur}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.out_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status))
        os.replace(tmp, self.out_dir / "status.json")

    def _alert(self, m, side: str, price: float, count: int, t_rem: float, kind: str) -> None:
        self.signal = {"ts": self.now, "ticker": m.ticker, "close_ts": m.close_ts, "side": side,
                       "price": price, "count": count, "t_rem": round(t_rem, 1), "kind": kind}
        if self.notify:
            verb = "BUY" if kind == "taker" else "BID"
            try:
                self.notify(f"{verb} {side.upper()} <= {round(price * 100)}c x{count} | {m.ticker} | "
                            f"{t_rem:.0f}s left")
            except Exception as e:  # alerts must never break trading
                self._emit("error", f"[alert] {e!r}")

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
        if m.strike is None or m.ticker in self.traded_tickers or (m.ticker in self.attempted and m.ticker not in self.resting):
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
            closes = self.spot_src.closes_1m(60)
            self.sigma, self.sigma_ts = sigma_per_second(closes), now
            self.spike = vol_spike(closes, self.filters)
        locked_mean = sum(locked) / len(locked) if locked else None
        fy = fair_yes(spot, m.strike, t_rem, self.sigma, locked_mean)
        book = self.kalshi.book(m.ticker)
        # expected settlement value: locked part of the 60s average + spot for the rest
        k = max(0.0, WINDOW_S - t_rem)
        expected = (k * locked_mean + (WINDOW_S - k) * spot) / WINDOW_S if locked_mean is not None else spot
        gap = expected - m.strike
        mid = (book.yes_bid + book.yes_ask) / 2 if book.yes_bid is not None and book.yes_ask is not None else None
        iv = implied_sigma(mid, spot, m.strike, t_rem, locked_mean) if mid is not None else None
        rv_ann, iv_ann = annualize(self.sigma), (annualize(iv) if iv else None)
        ratio = iv_ann / rv_ann if iv_ann and rv_ann else None
        venues = getattr(self.spot_src, "venues", "coinbase")
        self.cur.update(spot=spot, fair_yes=round(fy, 4), yes_ask=book.yes_ask, no_ask=book.no_ask,
                        yes_bid=book.yes_bid, gap=round(gap, 2), vol_realized=round(rv_ann, 4),
                        vol_implied=round(iv_ann, 4) if iv_ann else None,
                        vol_ratio=round(ratio, 3) if ratio else None, venues=venues,
                        vol_spike=round(self.spike[1], 2) if self.spike[1] else None)
        vol_note = f"vol mkt {iv_ann:.0%} vs real {rv_ann:.0%}" if iv_ann else f"vol real {rv_ann:.0%}"

        if m.ticker in self.resting:
            self._work_resting(m, book, fy, spot, t_rem)
            return

        blocked = self.risk.blocked(now) if self.risk else None
        market_block = news(now, m.close_ts, self.filters) or self.spike[0] or spread(book, self.filters)
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
            if abs(gap) < self.rule.min_gap_usd:
                notes.append(f"expected settle ${gap:+.0f} from strike < ${self.rule.min_gap_usd:.0f} basis guard")
                continue
            if self.rule.min_vol_ratio and (ratio is None or ratio < self.rule.min_vol_ratio):
                notes.append(f"vol ratio {ratio if ratio is None else round(ratio, 2)} < {self.rule.min_vol_ratio}")
                continue
            if market_block:
                notes.append(market_block)
                continue
            if blocked:
                notes.append(blocked)
                continue
            if self.risk:
                fixed = self.rule.contracts if self.rule.mode == "favorite" else None
                count, why = self.risk.size(fair, ask, fixed=fixed)
                if count < 1:
                    notes.append(f"size 0 ({why})")
                    continue
            else:
                count = self.rule.contracts
            if self.rule.execution == "maker":
                self._place_resting(m, side, ask, fair, count, t_rem)
            else:
                self._buy(now, m, side, ask, fair, edge, depth, spot, t_rem, count)
            return
        if self.last_skip[0] == m.ticker and now - self.last_skip[1] < 15:
            return
        self.last_skip = (m.ticker, now)
        self._say(f"{m.ticker} t-{t_rem:5.1f}s spot {spot:,.2f} vs strike {m.strike:,.2f} | "
                  f"fair YES {fy:.3f} | {vol_note} | skip: {'; '.join(notes) or 'empty book'}", kind="skip")

    # ---- taker -------------------------------------------------------------------------------
    def _buy(self, now, m, side, ask, fair, edge, depth, spot, t_rem, count=None) -> None:
        count = count or self.rule.contracts
        fill = self.executor.buy(m.ticker, side, ask, count, depth)
        if fill is None:
            self._say(f"{m.ticker}: order not filled")
            return
        self._record_fill(m, fill, fair, spot, t_rem, extra={"execution": "taker"})
        self._alert(m, side, ask, fill.count, t_rem, "taker")

    def _record_fill(self, m, fill: Fill, fair: float, spot: float, t_rem: float, extra: dict) -> None:
        edge = fair - fill.price - fill.fee / fill.count
        trade = {"ts": self.now, "ticker": m.ticker, "close_ts": m.close_ts, "strike": m.strike, "spot": spot,
                 "t_rem": round(t_rem, 1), "fair": round(fair, 4), "edge": round(edge, 4),
                 "rule": self.rule.mode, "status": "open", **asdict(fill), **extra,
                 **{k: self.cur.get(k) for k in ("gap", "vol_realized", "vol_implied", "vol_ratio", "venues",
                                                 "vol_spike")}}
        self.open_trades[m.ticker] = trade
        self.traded_tickers.add(m.ticker)
        append_jsonl(self.trades_path, trade)
        self.last_status = ""
        self._emit("buy", f">>> BUY {fill.side.upper()} x{fill.count} {m.ticker} @ {fill.price:.2f} "
                          f"(fee ${fill.fee:.2f}, cost ${fill.cost:.2f}) | fair {fair:.3f} edge {edge:+.3f} | "
                          f"t-{t_rem:.0f}s spot {spot:,.2f} strike {m.strike:,.2f} | order {fill.order_id}")

    # ---- maker -------------------------------------------------------------------------------
    def _place_resting(self, m, side, ask, fair, count, t_rem) -> None:
        fee_guess = taker_fee_per_contract(ask, count, MAKER_FEE_RATE)
        cap = ask - 0.01                                     # always improve on the ask
        if self.rule.mode == "model":                        # and never bid above fair - edge - fee
            cap = min(cap, fair - self.rule.min_edge - fee_guess)
        limit = math.floor(cap * 100 + 1e-9) / 100
        if limit < 0.01:
            self._say(f"{m.ticker}: maker limit would be {limit:.2f}, not placing")
            return
        self.resting[m.ticker] = {"side": side, "limit": limit, "count": count, "placed_ts": self.now,
                                  "ask_at_place": ask, "fair": fair}
        self.attempted.add(m.ticker)
        self.last_status = ""
        self._emit("order", f"=== REST BID {side.upper()} x{count} {m.ticker} @ {limit:.2f} "
                            f"(ask {ask:.2f}, fair {fair:.3f}) | t-{t_rem:.0f}s")
        self._alert(m, side, limit, count, t_rem, "maker")

    def _work_resting(self, m, book, fy, spot, t_rem) -> None:
        o = self.resting[m.ticker]
        ask = book.yes_ask if o["side"] == "yes" else book.no_ask
        if ask is not None and ask < o["limit"] - 1e-9:
            # a seller crossed below our bid: in a real book they would have hit us first
            n = o["count"]
            fill = Fill(f"paper-maker-{m.ticker[-6:]}", o["side"], n, o["limit"],
                        taker_fee_per_contract(o["limit"], n, MAKER_FEE_RATE) * n)
            del self.resting[m.ticker]
            fair = fy if o["side"] == "yes" else 1 - fy
            self._record_fill(m, fill, fair, spot, t_rem, extra={
                "execution": "maker", "ask_at_place": o["ask_at_place"],
                "improvement": round(o["ask_at_place"] - o["limit"], 4),
                "wait_s": round(self.now - o["placed_ts"], 1)})
        elif self.rule.mode == "model" and self._edge_gone(o, fy):
            del self.resting[m.ticker]
            self.last_status = ""
            fair = fy if o["side"] == "yes" else 1 - fy
            append_jsonl(self.out_dir / "maker_cancels.jsonl", {"ts": self.now, "ticker": m.ticker, "why": "edge gone", **o})
            self._emit("cancel", f"xxx CANCEL {o['side'].upper()} bid @ {o['limit']:.2f} on {m.ticker}: "
                                 f"edge gone (fair now {fair:.3f})")
        elif t_rem <= CANCEL_AT_T_REM:
            del self.resting[m.ticker]
            self.last_status = ""
            append_jsonl(self.out_dir / "maker_cancels.jsonl", {"ts": self.now, "ticker": m.ticker, **o})
            self._emit("cancel", f"xxx CANCEL {o['side'].upper()} bid @ {o['limit']:.2f} on {m.ticker}: "
                                 f"not filled by t-{CANCEL_AT_T_REM:.0f}s")
        else:
            shown = "none" if ask is None else f"{ask:.2f}"
            self._say(f"{m.ticker} t-{t_rem:5.1f}s resting {o['side'].upper()} bid {o['limit']:.2f} vs ask {shown}")

    def _edge_gone(self, o: dict, fy: float) -> bool:
        fair = fy if o["side"] == "yes" else 1 - fy
        fee = taker_fee_per_contract(o["limit"], o["count"], MAKER_FEE_RATE)
        return fair - o["limit"] - fee < self.rule.min_edge

    # ---- settlement --------------------------------------------------------------------------
    def _settle(self) -> None:
        for ticker, t in list(self.open_trades.items()):
            m = self.kalshi.market(ticker)
            if m.result not in ("yes", "no"):
                continue
            won = m.result == t["side"]
            payout = float(t["count"]) if won else 0.0
            pnl = payout - t["count"] * t["price"] - t["fee"]
            row = {**t, "status": "settled", "result": m.result, "won": won, "payout": payout,
                   "pnl": round(pnl, 4), "return_pct": round(100 * pnl / (t["count"] * t["price"] + t["fee"]), 2)}
            append_jsonl(self.trades_path, row)
            self.settled.append(row)
            del self.open_trades[ticker]
            if self.risk:
                self.risk.record(t["close_ts"], pnl)
            self._emit("win" if won else "loss", f"<<< SETTLED {ticker}: result {m.result.upper()} -> "
                       f"{'WIN' if won else 'LOSS'} payout ${payout:.2f} | P&L ${pnl:+.2f} ({row['return_pct']:+.1f}%)")

    def resume(self) -> "Trader":
        """Reload trades.jsonl so a restart keeps open positions (they still settle) and counts."""
        from .recorder import read_jsonl
        latest: dict[str, dict] = {}
        for r in read_jsonl(self.trades_path):
            latest[r["ticker"]] = r
        for ticker, r in latest.items():
            self.traded_tickers.add(ticker)
            if r["status"] == "settled":
                self.settled.append(r)
            else:
                self.open_trades[ticker] = r
        return self

    def run(self, poll_s: float = 2.0, idle_s: float = 5.0) -> None:
        self.log(f"[trader] rule={self.rule} max_trades={self.max_trades} executor={type(self.executor).__name__} "
                 f"risk={None if self.risk is None else self.risk.cfg}")
        while not self.done:
            try:
                self.tick(time.time())
            except Exception as e:  # keep running through network blips
                self._emit("error", f"[trader] error: {e!r}")
            final_minute = self.in_window and (self.cur.get("t_rem") or 999) <= WINDOW_S
            fast = final_minute or bool(self.resting)
            time.sleep(1.0 if fast else poll_s if self.in_window or self.open_trades else idle_s)
        total = sum(r["pnl"] for r in self.settled)
        self.log(f"[trader] done: {len(self.settled)} trade(s), total P&L ${total:+.2f}")
