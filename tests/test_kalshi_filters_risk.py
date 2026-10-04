import math
from datetime import datetime, timezone

import pytest

from kalshi.client import Book, Coinbase, Kalshi
from kalshi.execution import PaperExecutor
from kalshi.filters import FilterConfig, news, spread, vol_spike
from kalshi.recorder import read_jsonl
from kalshi.risk import RiskBook, RiskConfig, kelly_fraction
from kalshi.trader import Rule, Trader
from tests.test_kalshi_trader import BookAPI

CFG = FilterConfig()


def ts(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


# ---- filters -----------------------------------------------------------------------------
def test_spread_filter():
    assert spread(Book(0.80, 5, 0.18, 5), CFG) is None          # ask 0.82 -> 2c wide
    assert "spread 5c" in spread(Book(0.77, 5, 0.18, 5), CFG)   # ask 0.82 -> 5c wide
    assert spread(Book(None, 0, 0.18, 5), CFG) == "one-sided book"


def test_vol_spike_filter():
    calm = [100 * math.exp(0.0005 * ((i % 2) * 2 - 1)) for i in range(60)]
    reason, ratio = vol_spike(calm, CFG)
    assert reason is None and ratio == pytest.approx(1, abs=0.3)
    spiky = calm[:-6] + [100, 101, 99.5, 101.5, 99, 102]
    reason, ratio = vol_spike(spiky, CFG)
    assert reason and ratio > 2
    assert vol_spike(calm[:8], CFG) == (None, None)             # not enough history


def test_news_blackout_weekday_only():
    wed_close = ts(2026, 10, 7, 12, 30)                         # Wednesday 12:30 UTC release
    assert news(wed_close - 90, wed_close, CFG) == "news blackout 12:30 UTC"
    assert news(ts(2026, 10, 7, 12, 45) - 90, ts(2026, 10, 7, 12, 45), CFG) is None
    sun_close = ts(2026, 10, 4, 12, 30)                         # Sunday
    assert news(sun_close - 90, sun_close, CFG) is None
    assert news(wed_close - 90, wed_close, FilterConfig(news_blackout=False)) is None


# ---- risk --------------------------------------------------------------------------------
def test_kelly_math():
    assert kelly_fraction(0.80, 0.79, 0.01) == pytest.approx(0.0, abs=1e-9)   # fair price -> no bet
    f = kelly_fraction(0.90, 0.80, 0.01)
    assert f == pytest.approx((0.9 * (0.19 / 0.81) - 0.1) / (0.19 / 0.81))
    assert kelly_fraction(0.70, 0.80, 0.01) < 0


def test_size_respects_dollar_cap_and_kelly(tmp_path):
    rb = RiskBook(tmp_path, RiskConfig(bankroll=1000, per_trade_cap=5))
    n, why = rb.size(0.95, 0.80)                                # big edge on a big bankroll -> capped
    assert n == 6                                               # 6 x 0.80 + 7c fee = $4.87; 7 would be $5.68
    small = RiskBook(tmp_path / "s", RiskConfig(bankroll=20))
    n2, _ = small.size(0.86, 0.80)                              # tiny bankroll -> Kelly says 0-1 contracts
    assert n2 <= 1
    assert rb.size(0.70, 0.80)[0] == 0                          # negative edge -> no bet
    assert rb.size(0.70, 0.80, fixed=10)[0] == 6                # blind rule still capped by $


def test_daily_stop_and_kill_switch_persist(tmp_path):
    t0 = ts(2026, 10, 7, 15, 0)
    rb = RiskBook(tmp_path, RiskConfig(daily_loss_cap=25))
    for _ in range(5):
        rb.record(t0, -5.0)
    assert "daily loss stop" in rb.blocked(t0)
    assert "daily loss stop" in RiskBook(tmp_path).blocked(t0)  # survives restart
    assert RiskBook(tmp_path).blocked(t0 + 86400) is None       # new UTC day resets
    (tmp_path / "KILL").touch()
    assert "kill switch" in rb.blocked(t0 + 86400)


# ---- trader integration ------------------------------------------------------------------
def mk(api, tmp_path, **kw):
    logs, alerts = [], []
    t = Trader(Kalshi(api), Coinbase(api), PaperExecutor(), tmp_path, kw.pop("rule"), max_trades=1,
               log=logs.append, notify=alerts.append, **kw)
    return t, logs, alerts


def test_kill_switch_blocks_entry_and_alert_fires_on_buy(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    rb = RiskBook(tmp_path, RiskConfig(bankroll=100))
    (tmp_path / "KILL").touch()
    t, logs, alerts = mk(api, tmp_path, rule=Rule(mode="favorite"), risk=rb)
    t.tick(10_000 - 90)
    assert not t.traded_tickers and "kill switch" in logs[-1] and not alerts
    (tmp_path / "KILL").unlink()
    t.last_skip = ("", -1e18)
    t.tick(10_000 - 80)
    assert t.traded_tickers and alerts and alerts[0].startswith("BUY YES <= 80c")
    assert t.signal["side"] == "yes" and t.signal["price"] == 0.80


def test_maker_fills_only_when_ask_trades_through(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)          # yes ask 0.80
    t, logs, _ = mk(api, tmp_path, rule=Rule(mode="favorite", execution="maker"))
    t.tick(10_000 - 100)
    o = t.resting["KXBTC15M-T1"]
    assert o["limit"] == 0.79 and not t.traded_tickers
    api.no_bid = 0.21                                           # ask 0.79 == limit: touch, not through
    t.tick(10_000 - 90)
    assert t.resting and not t.traded_tickers
    api.no_bid = 0.22                                           # ask 0.78 < 0.79: through -> filled at 0.79
    t.tick(10_000 - 80)
    assert not t.resting and t.traded_tickers
    tr = t.open_trades["KXBTC15M-T1"]
    assert tr["price"] == 0.79 and tr["execution"] == "maker" and tr["improvement"] == pytest.approx(0.01)
    assert tr["fee"] < 0.0112 + 1e-9                            # maker fee below taker fee


def test_maker_cancels_near_close(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    t, logs, _ = mk(api, tmp_path, rule=Rule(mode="favorite", execution="maker"))
    t.tick(10_000 - 100)
    t.tick(10_000 - 4)
    assert not t.resting and not t.traded_tickers and logs[-1].startswith("xxx CANCEL")
    t.tick(10_000 - 3)                                           # no re-placement in the same market
    assert not t.resting
    assert len(read_jsonl(tmp_path / "maker_cancels.jsonl")) == 1


def test_settlement_updates_risk_bankroll(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    rb = RiskBook(tmp_path, RiskConfig(bankroll=100))
    t, _, _ = mk(api, tmp_path, rule=Rule(mode="favorite"), risk=rb)
    t.tick(10_000 - 90)
    api.status, api.result = "settled", "no"
    t.tick(10_001)
    assert rb.bankroll == pytest.approx(100 - 0.82) and rb.day_pnl == pytest.approx(-0.82)


def test_edge_gone_rule(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    t, _, _ = mk(api, tmp_path, rule=Rule(mode="model", execution="maker", min_edge=0.02))
    o = {"side": "yes", "limit": 0.80, "count": 1}
    assert not t._edge_gone(o, 0.90)          # 0.90 - 0.80 - ~0.01 fee >= 0.02
    assert t._edge_gone(o, 0.81)              # edge evaporated -> cancel
    assert not t._edge_gone({**o, "side": "no"}, 0.15)   # NO fair 0.85 - 0.80 - fee > 0.02 -> keep
    assert t._edge_gone({**o, "side": "no"}, 0.20)       # NO fair 0.80 -> no edge -> cancel


def test_resume_restores_open_and_settled(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    t, _, _ = mk(api, tmp_path, rule=Rule(mode="favorite"))
    t.tick(10_000 - 90)                                         # opens a position
    t2, _, _ = mk(api, tmp_path, rule=Rule(mode="favorite"))
    t2.resume()
    assert "KXBTC15M-T1" in t2.open_trades and "KXBTC15M-T1" in t2.traded_tickers
    api.status, api.result = "settled", "yes"
    t2.tick(10_001)                                             # restarted trader still settles it
    assert t2.done and t2.settled[0]["won"]
