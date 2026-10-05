"""Market data in, nothing out: Kalshi REST v2 (public endpoints) and a composite BTC/USD spot.

Kalshi settles KXBTC15M on the CF Benchmarks BRTI, an index over several USD exchanges. The
median of those venues' last prices tracks it far better than any one venue; the remaining
gap (basis) is why the desk refuses trades within a few dollars of the strike.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_BASE = "https://api.exchange.coinbase.com"
SERIES = "KXBTC15M"

Getter = Callable[[str, dict | None], dict | list]


def http_get(url: str, params: dict | None = None, retries: int = 3) -> dict | list:
    """GET JSON; on HTTP 429 wait (Retry-After, else 1s/2s/4s) and retry."""
    for attempt in range(retries + 1):
        r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "kalshi-desk/1.0"})
        if r.status_code == 429 and attempt < retries:
            wait = r.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()


# ---- Kalshi --------------------------------------------------------------------------------
@dataclass
class Market:
    ticker: str
    close_ts: float
    strike: float | None
    status: str
    result: str  # "yes" | "no" | "" while unresolved


@dataclass
class Book:
    """Kalshi returns bids only; each side's ask is 1 minus the other side's best bid."""
    yes_bid: float | None
    yes_bid_size: float
    no_bid: float | None
    no_bid_size: float

    @property
    def yes_ask(self) -> float | None:
        return None if self.no_bid is None else round(1 - self.no_bid, 4)

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_market(m: dict) -> Market:
    strike = m.get("floor_strike")
    return Market(m["ticker"], parse_ts(m["close_time"]), float(strike) if strike is not None else None,
                  m.get("status", ""), m.get("result", "") or "")


def _best(levels: list | None, scale: float) -> tuple[float | None, float]:
    if not levels:
        return None, 0.0
    px, qty = max(((float(p) * scale, float(q)) for p, q in levels), key=lambda x: x[0])
    return round(px, 4), qty


def parse_book(raw: dict) -> Book:
    """Current fixed-point dollars format, or the legacy integer-cents format."""
    if "orderbook_fp" in raw:
        ob = raw["orderbook_fp"]
        yes, no = _best(ob.get("yes_dollars"), 1.0), _best(ob.get("no_dollars"), 1.0)
    else:
        ob = raw.get("orderbook") or {}
        yes, no = _best(ob.get("yes"), 0.01), _best(ob.get("no"), 0.01)
    return Book(yes[0], yes[1], no[0], no[1])


class Kalshi:
    def __init__(self, get: Getter = http_get, base: str = KALSHI_BASE, markets_ttl: float = 15.0):
        self.get, self.base, self.markets_ttl = get, base, markets_ttl
        self._markets, self._markets_at = None, 0.0

    def open_markets(self) -> list[Market]:
        """Cached for `markets_ttl` seconds: the list only changes when a 15-minute market rolls."""
        now = time.monotonic()
        if self._markets is None or now - self._markets_at >= self.markets_ttl:
            raw = self.get(f"{self.base}/markets", {"series_ticker": SERIES, "status": "open", "limit": 20})
            self._markets = sorted((parse_market(m) for m in raw.get("markets", [])), key=lambda m: m.close_ts)
            self._markets_at = now
        return self._markets

    def market(self, ticker: str) -> Market:
        return parse_market(self.get(f"{self.base}/markets/{ticker}", None)["market"])

    def book(self, ticker: str) -> Book:
        return parse_book(self.get(f"{self.base}/markets/{ticker}/orderbook", None))


# ---- BTC spot --------------------------------------------------------------------------------
class Coinbase:
    def __init__(self, get: Getter = http_get, base: str = COINBASE_BASE):
        self.get, self.base = get, base

    def spot(self) -> float:
        return float(self.get(f"{self.base}/products/BTC-USD/ticker", None)["price"])

    def closes_1m(self, n: int = 60) -> list[float]:
        rows = self.get(f"{self.base}/products/BTC-USD/candles", {"granularity": 60})  # newest first
        return [float(r[4]) for r in reversed(rows[:n])]


SOURCES: dict[str, tuple[str, Callable[[dict | list], float]]] = {
    "coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", lambda j: float(j["price"])),
    "kraken": ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
               lambda j: float(next(iter(j["result"].values()))["c"][0])),
    "bitstamp": ("https://www.bitstamp.net/api/v2/ticker/btcusd/", lambda j: float(j["last"])),
    "gemini": ("https://api.gemini.com/v1/pubticker/btcusd", lambda j: float(j["last"])),
}


class CompositeSpot:
    """spot() = median of live venues. A dead venue is benched for `retry_s`; a venue printing
    more than `max_dispersion` from the median is ignored. Drop-in for Coinbase."""

    def __init__(self, get: Getter = http_get, sources: dict | None = None, retry_s: float = 300.0,
                 max_dispersion: float = 0.003, clock: Callable[[], float] = time.time):
        self.get, self.sources, self.retry_s = get, sources or SOURCES, retry_s
        self.max_dispersion, self.clock = max_dispersion, clock
        self.benched: dict[str, float] = {}
        self.last_quotes: dict[str, float] = {}
        self._history = Coinbase(get)
        self._pool = ThreadPoolExecutor(max_workers=len(self.sources))

    def _fetch(self, name: str) -> float | None:
        url, parse = self.sources[name]
        try:
            return parse(self.get(url, None))
        except Exception:
            self.benched[name] = self.clock() + self.retry_s
            return None

    def spot(self) -> float:
        live = [n for n in self.sources if self.benched.get(n, 0) <= self.clock()]
        quotes = {n: p for n, p in zip(live, self._pool.map(self._fetch, live)) if p}
        if not quotes:
            raise RuntimeError("no BTC price source reachable")
        med = statistics.median(quotes.values())
        quotes = {n: p for n, p in quotes.items() if abs(p / med - 1) <= self.max_dispersion} or quotes
        self.last_quotes = quotes
        return statistics.median(quotes.values())

    def closes_1m(self, n: int = 60) -> list[float]:
        return self._history.closes_1m(n)

    @property
    def venues(self) -> str:
        return ",".join(sorted(self.last_quotes)) or "none"
