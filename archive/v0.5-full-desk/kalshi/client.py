"""Public (unauthenticated) market-data clients: Kalshi REST v2 and Coinbase Exchange as the BRTI proxy.

Coinbase is one of BRTI's constituent venues, so it tracks the index closely but is NOT the
settlement source. The basis between them is a real model error, and the recorder logs it
implicitly via outcomes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_BASE = "https://api.exchange.coinbase.com"
SERIES = "KXBTC15M"

Getter = Callable[[str, dict | None], dict | list]


def http_get(url: str, params: dict | None = None, retries: int = 3) -> dict | list:
    """GET JSON; on HTTP 429 wait (Retry-After or 1s, 2s, 4s) and retry."""
    for attempt in range(retries + 1):
        r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "kalshi-recorder/0.1"})
        if r.status_code == 429 and attempt < retries:
            wait = r.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


@dataclass
class Market:
    ticker: str
    close_ts: float
    strike: float | None
    status: str
    result: str  # "yes" | "no" | "" while unresolved


@dataclass
class Book:
    yes_bid: float | None
    yes_bid_size: float
    no_bid: float | None
    no_bid_size: float

    @property
    def yes_ask(self) -> float | None:   # buying YES = hitting the best NO bid
        return None if self.no_bid is None else round(1 - self.no_bid, 4)

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)


def parse_market(m: dict) -> Market:
    strike = m.get("floor_strike")
    return Market(m["ticker"], parse_ts(m["close_time"]),
                  float(strike) if strike is not None else None,
                  m.get("status", ""), m.get("result", "") or "")


def _best(levels: list | None, scale: float) -> tuple[float | None, float]:
    if not levels:
        return None, 0.0
    px, qty = max(((float(p) * scale, float(q)) for p, q in levels), key=lambda x: x[0])
    return round(px, 4), qty


def parse_book(raw: dict) -> Book:
    """Handles current fixed-point dollars format and the legacy integer-cents format."""
    if "orderbook_fp" in raw:
        ob = raw["orderbook_fp"]
        yes, no = _best(ob.get("yes_dollars"), 1.0), _best(ob.get("no_dollars"), 1.0)
    else:
        ob = raw.get("orderbook") or {}
        yes, no = _best(ob.get("yes"), 0.01), _best(ob.get("no"), 0.01)
    return Book(yes[0], yes[1], no[0], no[1])


class Kalshi:
    def __init__(self, get: Getter = http_get, base: str = KALSHI_BASE, markets_ttl: float = 15.0):
        self.get, self.base = get, base
        self.markets_ttl, self._markets, self._markets_at = markets_ttl, None, 0.0

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


class Coinbase:
    def __init__(self, get: Getter = http_get, base: str = COINBASE_BASE):
        self.get, self.base = get, base

    def spot(self) -> float:
        return float(self.get(f"{self.base}/products/BTC-USD/ticker", None)["price"])

    def closes_1m(self, n: int = 60) -> list[float]:
        # rows: [time, low, high, open, close, volume], newest first
        rows = self.get(f"{self.base}/products/BTC-USD/candles", {"granularity": 60})
        return [float(r[4]) for r in reversed(rows[:n])]
