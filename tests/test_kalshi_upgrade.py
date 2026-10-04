import math

import pytest

from kalshi.client import Coinbase, Kalshi
from kalshi.execution import PaperExecutor
from kalshi.model import annualize, fair_yes, implied_sigma
from kalshi.prices import CompositeSpot
from kalshi.trader import Rule, Trader
from tests.test_kalshi_trader import BookAPI


def _src(price):
    return ("u-" + str(price), lambda j, p=price: p)


def test_composite_median_benches_dead_and_drops_outlier():
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url == "dead":
            raise ConnectionError("blocked")
        return {}

    clock = [1000.0]
    srcs = {"a": ("ok", lambda j: 100.0), "b": ("ok", lambda j: 100.2), "c": ("ok", lambda j: 130.0),
            "d": ("dead", lambda j: 0.0)}
    cs = CompositeSpot(get, srcs, retry_s=300, clock=lambda: clock[0])
    assert cs.spot() == pytest.approx(100.1)           # c is >0.3% off the median -> dropped
    assert set(cs.last_quotes) == {"a", "b"} and "d" in cs.benched
    n = calls.count("dead")
    cs.spot()
    assert calls.count("dead") == n                     # benched: not retried
    clock[0] += 301
    cs.spot()
    assert calls.count("dead") == n + 1                 # retried after cool-off


def test_composite_raises_when_nothing_reachable():
    cs = CompositeSpot(lambda u, p=None: (_ for _ in ()).throw(OSError()), {"x": ("u", float)})
    with pytest.raises(RuntimeError):
        cs.spot()


@pytest.mark.parametrize("ann,t,spot,locked", [(0.2, 110, 60030, None), (0.6, 90, 60030, None),
                                               (0.4, 30, 60004, 60001.0)])
def test_implied_vol_round_trip(ann, t, spot, locked):
    s = ann / math.sqrt(365 * 24 * 3600)
    p = fair_yes(spot, 60000, t, s, locked)
    assert 0.02 < p < 0.98
    assert annualize(implied_sigma(p, spot, 60000, t, locked)) == pytest.approx(ann, rel=1e-3)


def test_implied_vol_none_on_rails():
    assert implied_sigma(0.995, 60030, 60000, 30) is None


def test_trader_records_vol_and_applies_gap_guard(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)   # spot 60010 vs strike 60000 -> gap $10
    logs = []
    t = Trader(Kalshi(api), Coinbase(api), PaperExecutor(), tmp_path, Rule(mode="favorite", min_gap_usd=20),
               max_trades=1, log=logs.append)
    t.tick(10_000 - 90)
    assert not t.traded_tickers and "basis guard" in logs[-1]
    assert t.cur["gap"] == pytest.approx(10) and t.cur["vol_realized"] > 0
    t.rule.min_gap_usd = 5
    t.tick(10_000 - 80)
    assert t.traded_tickers
    trade = t.open_trades["KXBTC15M-T1"]
    assert trade["gap"] == pytest.approx(10) and "vol_ratio" in trade
