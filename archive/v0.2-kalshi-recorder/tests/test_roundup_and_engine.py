from decimal import Decimal

import pytest

from roundup_bot.broker import CcxtBroker, PaperBroker
from roundup_bot.config import Config, LadderConfig, RiskConfig, RoundupConfig, validate
from roundup_bot.engine import Engine
from roundup_bot.position import Bar
from roundup_bot.roundup import Ledger, Transaction, roundup_amount, sweep


def test_roundup_math():
    c = RoundupConfig()
    assert roundup_amount(Decimal("4.35"), c) == Decimal("0.65")
    assert roundup_amount(Decimal("5.00"), c) == Decimal("0")
    assert roundup_amount(Decimal("-20"), c) == Decimal("0")
    assert roundup_amount(Decimal("5.00"), RoundupConfig(whole_dollar_adds_one=True)) == Decimal("1.00")
    assert roundup_amount(Decimal("4.35"), RoundupConfig(multiplier=2)) == Decimal("1.30")


def test_ledger_is_idempotent_and_sweeps(tmp_path):
    cfg = RoundupConfig(min_sweep_usd=0.50, ledger_path=str(tmp_path / "l.json"))
    led = Ledger(cfg.ledger_path)
    txns = [Transaction("a", Decimal("3.40")), Transaction("b", Decimal("7.75"))]
    assert led.add(txns, cfg) == Decimal("0.85")
    assert led.add(txns, cfg) == Decimal("0")
    got = []
    assert sweep(led, type("S", (), {"deposit": lambda s, a: got.append(a)})(), cfg) == Decimal("0.85")
    led.save()
    again = Ledger(cfg.ledger_path)
    assert again.pending == 0 and again.swept_total == Decimal("0.85") and "a" in again.seen


def test_validate_rejects_stop_near_liquidation():
    with pytest.raises(ValueError, match="liquidation"):
        validate(Config(risk=RiskConfig(leverage=10), ladder=LadderConfig(stop_loss_roe_pct=-90)))
    with pytest.raises(ValueError, match="leverage"):
        validate(Config(risk=RiskConfig(leverage=50)))


def test_paper_isolated_margin_caps_loss():
    b = PaperBroker(cash=100, fee_rate=0)
    b.open(1, 1.0, 100.0, 5)      # margin 20
    b.close(1, 1.0, 50.0)         # -50 pnl, capped at -20 margin
    assert b.equity() == pytest.approx(80)


def test_live_requires_ack(monkeypatch):
    monkeypatch.delenv("ROUNDUP_BOT_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="blocked"):
        CcxtBroker("bybit", "BTC/USDT:USDT")


def test_engine_enters_on_cross_and_ladders_out():
    cfg = validate(Config())
    cfg.strategy.fast_ema, cfg.strategy.slow_ema = 2, 4
    b = PaperBroker(cash=100, fee_rate=0)
    eng = Engine(cfg, b)
    prices = [100, 99, 98, 97, 96, 95, 97, 100]
    for p in prices:
        eng.on_bar(Bar("t", p, p, p, p))
    assert eng.pos and eng.pos.side == 1
    entry = eng.pos.entry
    eng.on_bar(Bar("t", entry, entry * 1.25, entry, entry * 1.2))   # +125% ROE at 5x
    reasons = [t.action for t in eng.log]
    assert reasons[:3] == ["open", "tp@50%", "tp@100%"]
    assert b.equity() > 100


def test_deposit_raises_peak_so_losses_are_not_masked():
    b = PaperBroker(cash=100, fee_rate=0)
    eng = Engine(validate(Config()), b)
    eng.on_bar(Bar("t", 1, 1, 1, 1))
    eng.deposit(50)
    assert eng.peak_equity == 150
