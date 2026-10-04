"""Bar-driven engine: signal -> size -> enter -> ladder/stop -> exit, with a drawdown kill switch."""
from __future__ import annotations

from dataclasses import dataclass, field

from .broker import Broker
from .config import Config
from .position import Bar, Fill, Position
from .sizing import size_position
from .strategy import signal


@dataclass
class TradeLog:
    ts: str
    action: str
    side: int
    qty: float
    price: float
    equity: float


@dataclass
class Engine:
    cfg: Config
    broker: Broker
    closes: list[float] = field(default_factory=list)
    pos: Position | None = None
    peak_equity: float = 0.0
    halted: bool = False
    log: list[TradeLog] = field(default_factory=list)

    def _record(self, ts: str, action: str, side: int, qty: float, price: float) -> None:
        self.log.append(TradeLog(ts, action, side, qty, price, self.broker.equity()))

    def _apply(self, ts: str, fills: list[Fill]) -> None:
        for f in fills:
            px = self.broker.close(self.pos.side, f.qty, f.price)
            self._record(ts, f.reason, self.pos.side, f.qty, px)
        if self.pos and self.pos.qty == 0:
            self.pos = None

    def deposit(self, usd: float) -> None:
        """Fresh cash raises the peak too, so deposits never mask trading losses."""
        self.broker.deposit(usd)
        self.peak_equity += usd

    def on_bar(self, bar: Bar) -> None:
        # 1. Manage open position against this bar's range.
        if self.pos:
            self._apply(bar.ts, self.pos.on_bar(bar))

        self.closes.append(bar.close)
        sig = signal(self.closes, self.cfg.strategy)

        # 2. Opposite signal exits whatever is left of the runner.
        if self.pos and sig == -self.pos.side:
            self._apply(bar.ts, self.pos.exit_all(bar.close, "signal-exit"))

        # 3. Kill switch on drawdown from peak equity.
        eq = self.broker.equity()
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0 and eq < self.peak_equity * (1 - self.cfg.risk.max_drawdown_pct / 100):
            if not self.halted:
                if self.pos:
                    self._apply(bar.ts, self.pos.exit_all(bar.close, "kill-switch"))
                self._record(bar.ts, "HALT", 0, 0, bar.close)
            self.halted = True
            return

        # 4. New entry on signal when flat.
        if not self.pos and sig != 0 and not self.halted:
            self._enter(bar, sig)

    def _enter(self, bar: Bar, side: int) -> None:
        r = self.cfg.risk
        size = size_position(self.broker.equity(), bar.close, self.cfg.ladder, r)
        if size is None:
            self._record(bar.ts, "skip-too-small", side, 0, bar.close)
            return
        px = self.broker.open(side, size.qty, bar.close, r.leverage)
        self.pos = Position(side=side, entry=px, qty=size.qty, leverage=r.leverage, cfg=self.cfg.ladder)
        self._record(bar.ts, "open", side, size.qty, px)
