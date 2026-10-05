"""Shared fakes: a one-market Kalshi + BTC feed served from memory (no network)."""
import math
from datetime import datetime, timezone

import pytest

from kalshi.desk import Desk, PaperBook
from kalshi.feeds import Coinbase, Kalshi
from kalshi.rules import Rule

TICKER = "KXBTC15M-T1"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeAPI:
    """One market closing at close_ts, strike 60000, spot 60010. Book is configurable in dollars."""

    def __init__(self, close_ts=10_000.0, yes_bid=0.60, no_bid=0.35, depth="50", strike=60000.0):
        self.close_ts, self.strike = close_ts, strike
        self.yes_bid, self.no_bid, self.depth = yes_bid, no_bid, depth
        self.spot, self.result, self.status = 60010.0, "", "open"
        self.calls: list[str] = []

    def __call__(self, url, params=None):
        self.calls.append(url)
        mk = {"ticker": TICKER, "close_time": iso(self.close_ts), "floor_strike": self.strike,
              "status": self.status, "result": self.result}
        if url.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [[f"{self.yes_bid:.4f}", self.depth]],
                                     "no_dollars": [[f"{self.no_bid:.4f}", self.depth]]}}
        if url.endswith("/markets"):
            return {"markets": [mk] if self.status == "open" else []}
        if "/markets/" in url:
            return {"market": mk}
        if url.endswith("/ticker"):
            return {"price": str(self.spot)}
        if url.endswith("/candles"):
            return [[0, 0, 0, 0, 60000 * math.exp(0.0003 * ((i % 3) - 1)), 0] for i in range(60)]
        raise AssertionError(url)

    def settle(self, result):
        self.status, self.result = "settled", result


def make_desk(api, out_dir, rules=None, risks=None, executions=("taker",), max_trades=1, logs=None):
    """Desk over FakeAPI with one book per execution. rules/risks: dicts keyed by execution."""
    rules, risks = rules or {}, risks or {}
    books = [PaperBook(e, rules.get(e, Rule(mode="favorite")), execution=e, max_trades=max_trades,
                       risk=risks.get(e)) for e in executions]
    log = logs.append if logs is not None else (lambda m: None)
    return Desk(Kalshi(api, markets_ttl=0), Coinbase(api), out_dir, books, log=log)


@pytest.fixture
def favorite_api():
    return FakeAPI(yes_bid=0.78, no_bid=0.20)        # YES ask 0.80, NO ask 0.22
