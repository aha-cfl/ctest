import pytest

from roundup_bot.config import LadderConfig
from roundup_bot.position import Bar, Position


def mk(side=1, lev=5.0, **kw):
    return Position(side=side, entry=100.0, qty=4.0, leverage=lev, cfg=LadderConfig(**kw))


def bar(o, h, l, c):
    return Bar("t", o, h, l, c)


def test_roe_price_mapping():
    p = mk()
    assert p.price_at_roe(50) == pytest.approx(110.0)   # 5x: +50% ROE = +10% price
    assert p.price_at_roe(-40) == pytest.approx(92.0)
    assert mk(side=-1).price_at_roe(50) == pytest.approx(90.0)


def test_ladder_hits_all_four_levels_in_one_bar():
    p = mk()
    fills = p.on_bar(bar(100, 140, 99, 139))   # 140 = +200% ROE
    assert [f.reason for f in fills] == ["tp@50%", "tp@100%", "tp@150%"]
    assert [f.price for f in fills] == pytest.approx([110, 120, 130])
    assert p.qty == pytest.approx(1.0)          # 25% runner left
    assert p.stop_roe == 150                    # ratcheted to previous level


def test_levels_continue_past_200():
    p = mk()
    p.on_bar(bar(100, 160, 99, 159))            # +300% ROE
    assert p.next_level == 6                     # 50,100,150,200,250,300
    assert p.stop_roe == 250


def test_stop_ratchets_to_breakeven_after_first_level():
    p = mk()
    p.on_bar(bar(100, 111, 100, 110))
    assert p.stop_roe == 0.0
    fills = p.on_bar(bar(105, 106, 99, 100))
    assert fills[-1].reason == "stop@0%" and fills[-1].price == pytest.approx(100)
    assert p.qty == 0


def test_initial_stop_and_gap_fill():
    p = mk()
    f = p.on_bar(bar(99, 99, 91, 95))
    assert f[0].price == pytest.approx(92.0)
    p2 = mk()
    f2 = p2.on_bar(bar(85, 86, 80, 84))          # gapped below 92
    assert f2[0].price == pytest.approx(85.0)


def test_stop_checked_before_tp_same_bar():
    p = mk()
    f = p.on_bar(bar(100, 115, 90, 100))
    assert [x.reason for x in f] == ["stop@-40%"]


def test_short_ladder():
    p = mk(side=-1)
    fills = p.on_bar(bar(100, 101, 89, 90))
    assert fills[0].reason == "tp@50%" and fills[0].price == pytest.approx(90)
