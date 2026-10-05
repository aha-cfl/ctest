import math
import random

import pytest

from kalshi.model import annualize, fair_yes, implied_sigma, kelly_fraction, sigma_per_second, taker_fee_per_contract

SIG = 0.5 / math.sqrt(365 * 24 * 3600)


def test_fair_at_the_money_is_half_and_monotonic():
    assert fair_yes(60000, 60000, 120, SIG) == pytest.approx(0.5)
    ps = [fair_yes(s, 60000, 120, SIG) for s in (59950, 59990, 60000, 60010, 60050)]
    assert ps == sorted(ps) and ps[0] < 0.2 and ps[-1] > 0.8


def test_locked_average_dominates_near_close():
    # 50s of the 60s window already averaged well above strike; spot just dipped below.
    assert fair_yes(59990, 60000, 10, SIG, locked_mean=60040) > 0.99
    assert fair_yes(60000, 60000, 0, SIG, locked_mean=60001) == 1.0


@pytest.mark.parametrize("t_rem,locked", [(120, None), (90, None), (30, 60012.0)])
def test_fair_matches_monte_carlo_of_60s_average(t_rem, locked):
    rng = random.Random(3)
    spot, strike, sims, yes = 60010.0, 60000.0, 20000, 0
    k = 60 - min(t_rem, 60)
    for _ in range(sims):
        s, prints = spot, []
        for sec in range(int(t_rem), 0, -1):
            s *= math.exp(rng.gauss(0, SIG))
            if sec <= 60:
                prints.append(s)
        avg = (locked * k + sum(prints)) / 60 if locked else sum(prints) / 60
        yes += avg > strike
    assert fair_yes(spot, strike, t_rem, SIG, locked) == pytest.approx(yes / sims, abs=0.015)


def test_sigma_recovers_true_vol():
    rng = random.Random(0)
    p, closes = 60000.0, []
    for _ in range(2000):
        p *= math.exp(rng.gauss(0, SIG * math.sqrt(60)))
        closes.append(p)
    assert sigma_per_second(closes) == pytest.approx(SIG, rel=0.05)


def test_fee():
    assert taker_fee_per_contract(0.80, 10) == pytest.approx(0.012)    # ceil(11.2c) / 10
    assert taker_fee_per_contract(0.80, 1) == pytest.approx(0.02)      # rounding hurts tiny orders
    assert taker_fee_per_contract(0.80, 100) == pytest.approx(0.0112)


@pytest.mark.parametrize("ann,t,spot,locked", [(0.2, 110, 60030, None), (0.6, 90, 60030, None),
                                               (0.4, 30, 60004, 60001.0)])
def test_implied_vol_round_trip(ann, t, spot, locked):
    s = ann / math.sqrt(365 * 24 * 3600)
    p = fair_yes(spot, 60000, t, s, locked)
    assert 0.02 < p < 0.98
    assert annualize(implied_sigma(p, spot, 60000, t, locked)) == pytest.approx(ann, rel=1e-3)


def test_implied_vol_undefined_cases():
    assert implied_sigma(0.995, 60030, 60000, 30) is None          # price on the rail
    assert implied_sigma(0.5, 60000, 60000, 90) is None            # at the money: any vol gives 50%
    assert implied_sigma(0.8, 59960, 60000, 90) is None            # price points the other way


def test_kelly_math():
    assert kelly_fraction(0.80, 0.79, 0.01) == pytest.approx(0.0, abs=1e-9)   # fair price -> no bet
    b = 0.19 / 0.81
    assert kelly_fraction(0.90, 0.80, 0.01) == pytest.approx((0.9 * b - 0.1) / b)
    assert kelly_fraction(0.70, 0.80, 0.01) < 0
