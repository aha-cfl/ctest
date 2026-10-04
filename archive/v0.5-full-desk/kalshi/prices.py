"""Composite BTC/USD spot from several BRTI constituent exchanges.

CF Benchmarks BRTI aggregates order books from major USD venues (Coinbase, Kraken, Bitstamp,
Gemini, ...). A median of their last prices tracks it far better than one venue, which cuts
the basis error that matters most when spot sits near the strike.

Unreachable venues (network policy, outages) are benched for `retry_s` so a dead host
doesn't add its timeout to every tick. With one live venue this degrades to plain Coinbase.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .client import Coinbase, Getter, http_get

SOURCES: dict[str, tuple[str, Callable[[dict | list], float]]] = {
    "coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", lambda j: float(j["price"])),
    "kraken": ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
               lambda j: float(next(iter(j["result"].values()))["c"][0])),
    "bitstamp": ("https://www.bitstamp.net/api/v2/ticker/btcusd/", lambda j: float(j["last"])),
    "gemini": ("https://api.gemini.com/v1/pubticker/btcusd", lambda j: float(j["last"])),
}


class CompositeSpot:
    """Drop-in for Coinbase in Trader/Recorder: spot() = median across live venues."""

    def __init__(self, get: Getter = http_get, sources: dict | None = None, retry_s: float = 300.0,
                 max_dispersion: float = 0.003, clock: Callable[[], float] = time.time):
        self.get, self.sources, self.retry_s = get, sources or SOURCES, retry_s
        self.max_dispersion, self.clock = max_dispersion, clock
        self.benched: dict[str, float] = {}       # venue -> retry-after timestamp
        self.last_quotes: dict[str, float] = {}
        self._history = Coinbase(get)             # 1m candles for realized vol
        self._pool = ThreadPoolExecutor(max_workers=len(self.sources))

    def _fetch(self, name: str) -> float | None:
        url, parse = self.sources[name]
        try:
            return parse(self.get(url, None))
        except Exception:
            self.benched[name] = self.clock() + self.retry_s
            return None

    def spot(self) -> float:
        now = self.clock()
        live = [n for n in self.sources if self.benched.get(n, 0) <= now]
        quotes = dict(zip(live, self._pool.map(self._fetch, live)))
        quotes = {n: p for n, p in quotes.items() if p}
        if not quotes:
            raise RuntimeError("no BTC price source reachable")
        med = statistics.median(quotes.values())
        # drop a venue printing far from the pack (stale feed, bad tick)
        quotes = {n: p for n, p in quotes.items() if abs(p / med - 1) <= self.max_dispersion} or quotes
        self.last_quotes = quotes
        return statistics.median(quotes.values())

    def closes_1m(self, n: int = 60) -> list[float]:
        return self._history.closes_1m(n)

    @property
    def venues(self) -> str:
        return ",".join(sorted(self.last_quotes)) or "none"
