import pytest

from roundup_bot.broker import PaperBroker
from roundup_bot.config import Config, LadderConfig, RiskConfig, validate
from roundup_bot.engine import Engine
from roundup_bot.position import Bar
from roundup_bot.sizing import size_position


def test_risk_budget_math():
    r = RiskConfig(leverage=5, risk_per_trade_pct=1.0, taker_fee_rate=0.0005)
    s = size_position(1000, 100, LadderConfig(stop_loss_roe_pct=-40), r)
    # stop = 8% price move, +0.1% fees -> $10 / 0.081
    assert s.notional == pytest.approx(10 / 0.081)
    assert s.risk_usd == pytest.approx(10.0)
    assert s.margin == pytest.approx(s.notional / 5)
    assert not s.capped_by_margin


def test_margin_cap_limits_size_and_reports_actual_risk():
    r = RiskConfig(leverage=5, risk_per_trade_pct=2.0, margin_fraction_per_trade=0.01, taker_fee_rate=0)
    s = size_position(1000, 100, LadderConfig(stop_loss_roe_pct=-40), r)
    assert s.capped_by_margin and s.notional == pytest.approx(50)
    assert s.risk_usd == pytest.approx(4.0)   # 50 * 8%


def test_below_min_notional_returns_none():
    # $25 * 1% = $0.25 risk -> ~$3 notional < $5 minimum
    assert size_position(25, 100, LadderConfig(), RiskConfig()) is None


def test_validate_caps_risk_per_trade():
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        validate(Config(risk=RiskConfig(risk_per_trade_pct=5)))
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        validate(Config(risk=RiskConfig(risk_per_trade_pct=0)))


def _engine_long(cash, risk_pct):
    cfg = validate(Config(risk=RiskConfig(risk_per_trade_pct=risk_pct)))
    cfg.strategy.fast_ema, cfg.strategy.slow_ema = 2, 4
    b = PaperBroker(cash=cash, fee_rate=cfg.risk.taker_fee_rate)
    eng = Engine(cfg, b)
    for p in [100, 99, 98, 97, 96, 95, 97, 100]:
        eng.on_bar(Bar("t", p, p, p, p))
    return eng, b


@pytest.mark.parametrize("risk_pct", [1.0, 2.0])
def test_stop_out_loses_exactly_risk_budget(risk_pct):
    eng, b = _engine_long(1000, risk_pct)
    assert eng.pos is not None
    start = 1000.0
    stop = eng.pos.stop_price
    eng.on_bar(Bar("t", eng.pos.entry, eng.pos.entry, stop * 0.99, stop))
    stop_fill = next(t for t in eng.log if t.action.startswith("stop@"))
    assert start - stop_fill.equity == pytest.approx(start * risk_pct / 100, rel=0.01)


def test_engine_logs_skip_when_account_too_small():
    eng, _ = _engine_long(25, 1.0)
    assert eng.pos is None
    assert eng.log[-1].action == "skip-too-small"
