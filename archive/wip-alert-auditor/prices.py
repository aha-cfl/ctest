"""Public 1-minute candles for any coin: Coinbase <SYM>-USD, Kraken <SYM>USD as fallback.

Spot, not perp: perp prices track spot within a small funding-driven basis, which the dashboard notes.
"""
from __future__ import annotations

from typing import Callable

from kalshi.feeds import http_get

Getter = Callable[[str, dict | None], dict | list]
Candle = tuple[float, float, float, float, float]      # (ts_open, open, high, low, close)


class Candles:
    def __init__(self, get: Getter = http_get):
        self.get = get

    def _coinbase(self, sym: str, start: float, end: float) -> list[Candle]:
        out: list[Candle] = []
        s = int(start // 60 * 60)
        while s < end:
            e = min(s + 300 * 60, int(end))
            rows = self.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles",
                            {"granularity": 60, "start": s, "end": e})
            out += [(float(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows]  # t,l,h,o,c
            s = e
        return out

    def _kraken(self, sym: str, start: float, end: float) -> list[Candle]:
        j = self.get("https://api.kraken.com/0/public/OHLC", {"pair": f"{sym}USD", "interval": 1, "since": int(start)})
        if j.get("error"):
            raise RuntimeError(j["error"])
        rows = next(v for k, v in j["result"].items() if k != "last")
        return [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if float(r[0]) < end]

    def fetch(self, sym: str, start: float, end: float) -> list[Candle]:
        """Closed 1-minute candles with start <= ts_open < end - 60, oldest first."""
        errors = []
        for src in (self._coinbase, self._kraken):
            try:
                rows = src(sym, start, end)
                rows = sorted({c[0]: c for c in rows if start <= c[0] <= end - 60}.values())
                return rows
            except Exception as e:  # unknown pair, outage: try the next venue
                errors.append(f"{src.__name__}: {e!r}")
        raise RuntimeError(f"no price source for {sym}: {'; '.join(errors)}")

    def last(self, sym: str) -> float:
        try:
            return float(self.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker", None)["price"])
        except Exception:
            j = self.get("https://api.kraken.com/0/public/Ticker", {"pair": f"{sym}USD"})
            return float(next(iter(j["result"].values()))["c"][0])
