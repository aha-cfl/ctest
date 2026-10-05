import math
from datetime import datetime, timezone

from kalshi.feeds import Book
from kalshi.rules import FilterConfig, RiskBook, RiskConfig, Rule, news, side_blocker, spread, vol_spike

CFG = FilterConfig()


def ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_side_blocker_order_and_messages():
    r = Rule(mode="model", min_edge=0.02, min_gap_usd=5)
    assert "outside band" in side_blocker(r, "yes", 0.90, 0.99, 50, None)
    assert "edge" in side_blocker(r, "yes", 0.80, 0.81, 50, None)
    assert "basis guard" in side_blocker(r, "yes", 0.80, 0.90, 3, None)
    assert side_blocker(r, "yes", 0.80, 0.90, 50, None) is None
    assert side_blocker(Rule(mode="favorite"), "yes", 0.80, 0.10, 50, None) is None     # blind rule ignores edge
    vr = Rule(mode="model", min_vol_ratio=1.2)
    assert "vol ratio" in side_blocker(vr, "yes", 0.80, 0.90, 50, 1.0)
    assert side_blocker(vr, "yes", 0.80, 0.90, 50, 1.3) is None


def test_spread_filter():
    assert spread(Book(0.80, 5, 0.18, 5), CFG) is None          # ask 0.82 -> 2c wide
    assert "spread 5c" in spread(Book(0.77, 5, 0.18, 5), CFG)   # ask 0.82 -> 5c wide
    assert spread(Book(None, 0, 0.18, 5), CFG) == "one-sided book"


def test_vol_spike_filter():
    calm = [100 * math.exp(0.0005 * ((i % 2) * 2 - 1)) for i in range(60)]
    reason, ratio = vol_spike(calm, CFG)
    assert reason is None and 0.7 < ratio < 1.3
    reason, ratio = vol_spike(calm[:-6] + [100, 101, 99.5, 101.5, 99, 102], CFG)
    assert reason and ratio > 2
    assert vol_spike(calm[:8], CFG) == (None, None)             # not enough history


def test_news_blackout_weekday_only():
    wed = ts(2026, 10, 7, 12, 30)                               # Wednesday 12:30 UTC release
    assert news(wed - 90, wed, CFG) == "news blackout 12:30 UTC"
    assert news(ts(2026, 10, 7, 12, 45) - 90, ts(2026, 10, 7, 12, 45), CFG) is None
    sun = ts(2026, 10, 4, 12, 30)
    assert news(sun - 90, sun, CFG) is None
    assert news(wed - 90, wed, FilterConfig(news_blackout=False)) is None


def test_size_respects_dollar_cap_and_kelly(tmp_path):
    rb = RiskBook(tmp_path, RiskConfig(bankroll=1000, per_trade_cap=5))
    assert rb.size(0.95, 0.80)[0] == 6                          # 6 x 0.80 + 7c fee = $4.87; 7 would be $5.68
    assert RiskBook(tmp_path / "s", RiskConfig(bankroll=20)).size(0.86, 0.80)[0] <= 1
    assert rb.size(0.70, 0.80)[0] == 0                          # negative edge -> no bet
    assert rb.size(0.70, 0.80, fixed=10)[0] == 6                # blind rule still capped by $


def test_daily_stop_and_kill_switch_persist(tmp_path):
    t0 = ts(2026, 10, 7, 15, 0)
    rb = RiskBook(tmp_path, RiskConfig(daily_loss_cap=25), kill_path=tmp_path / "KILL")
    for _ in range(5):
        rb.record(t0, -5.0)
    assert "daily loss stop" in rb.blocked(t0)
    assert "daily loss stop" in RiskBook(tmp_path).blocked(t0)   # survives restart
    assert RiskBook(tmp_path).blocked(t0 + 86400) is None        # new UTC day resets
    (tmp_path / "KILL").touch()
    assert "kill switch" in rb.blocked(t0 + 86400)
