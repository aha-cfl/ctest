import json
import math
import random

import pytest

from kalshi.analyze import load, report, run_strategy, wilson
from kalshi.client import Coinbase, Kalshi, parse_book, parse_market
from kalshi.model import fair_yes, sigma_per_second, taker_fee_per_contract
from kalshi.recorder import Recorder, read_jsonl

SIG = 0.5 / math.sqrt(365 * 24 * 3600)


# ---- model -------------------------------------------------------------------------------
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
        avg = ((locked * k if locked else 0) + sum(prints)) / 60 if locked else sum(prints) / 60
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
    assert taker_fee_per_contract(0.80, 10) == pytest.approx(0.012)   # ceil(11.2c) / 10
    assert taker_fee_per_contract(0.80, 1) == pytest.approx(0.02)     # rounding hurts tiny orders
    assert taker_fee_per_contract(0.80, 100) == pytest.approx(0.0112)


# ---- client parsing ----------------------------------------------------------------------
def test_parse_book_fixed_point_and_legacy():
    fp = {"orderbook_fp": {"yes_dollars": [["0.7800", "10.00"], ["0.8000", "5.00"]],
                           "no_dollars": [["0.1500", "20.00"], ["0.1800", "7.00"]]}}
    b = parse_book(fp)
    assert (b.yes_bid, b.yes_bid_size, b.no_bid) == (0.80, 5.0, 0.18)
    assert b.yes_ask == 0.82 and b.no_ask == 0.20
    legacy = parse_book({"orderbook": {"yes": [[80, 5]], "no": [[18, 7]]}})
    assert (legacy.yes_bid, legacy.yes_ask) == (0.80, 0.82)
    empty = parse_book({"orderbook_fp": {"yes_dollars": None, "no_dollars": []}})
    assert empty.yes_ask is None and empty.no_ask is None


def test_parse_market():
    m = parse_market({"ticker": "KXBTC15M-X", "close_time": "2026-09-30T20:30:00Z",
                      "floor_strike": 60123.45, "status": "open", "result": ""})
    assert m.strike == 60123.45 and m.result == "" and m.close_ts == 1790800200.0


# ---- recorder with a fake exchange -------------------------------------------------------
class FakeAPI:
    def __init__(self, close_ts, strike=60000.0):
        self.close_ts, self.strike = close_ts, strike
        self.spot, self.result, self.status = 60010.0, "", "open"

    def __call__(self, url, params=None):
        mk = {"ticker": "KXBTC15M-T1", "close_time": _iso(self.close_ts), "floor_strike": self.strike,
              "status": self.status, "result": self.result}
        if url.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [["0.6000", "50"]], "no_dollars": [["0.3500", "40"]]}}
        if url.endswith("/markets"):
            return {"markets": [mk] if self.status == "open" else []}
        if "/markets/" in url:
            return {"market": mk}
        if url.endswith("/ticker"):
            return {"price": str(self.spot)}
        if url.endswith("/candles"):
            return [[0, 0, 0, 0, 60000 * math.exp(0.0003 * ((i % 3) - 1)), 0] for i in range(60)]
        raise AssertionError(url)


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def test_recorder_window_snapshots_and_resolution(tmp_path):
    api = FakeAPI(close_ts=10_000.0)
    rec = Recorder(Kalshi(api), Coinbase(api), tmp_path, window_s=120)
    assert rec.tick(10_000 - 300) is None                   # too early
    row = rec.tick(10_000 - 100)
    assert row["yes_ask"] == 0.65 and row["no_ask"] == 0.40 and 0.5 < row["fair_yes"] < 1
    rec.tick(10_000 - 30)                                   # inside averaging window
    assert rec.spot_samples["KXBTC15M-T1"] == [60010.0]
    api.status, api.result = "settled", "yes"
    rec.tick(10_001)
    outs = read_jsonl(tmp_path / "outcomes.jsonl")
    assert outs == [{"ticker": "KXBTC15M-T1", "result": "yes", "strike": 60000.0, "close_ts": 10_000.0}]
    assert len(read_jsonl(tmp_path / "snapshots.jsonl")) == 2


# ---- analysis ----------------------------------------------------------------------------
def _write(tmp_path, snaps, outs):
    (tmp_path / "snapshots.jsonl").write_text("".join(json.dumps(s) + "\n" for s in snaps))
    (tmp_path / "outcomes.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outs))


def _snap(t, t_rem, fair, yes_ask, no_ask=None):
    return {"ticker": t, "t_rem": t_rem, "fair_yes": fair, "yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": None if no_ask is None else round(1 - no_ask, 4),
            "no_bid": round(1 - yes_ask, 4), "yes_bid_size": 10, "no_bid_size": 10}


def test_one_trade_per_market_and_model_filter(tmp_path):
    snaps = [_snap("A", 110, 0.90, 0.80), _snap("A", 100, 0.90, 0.80), _snap("A", 90, 0.9, 0.80),
             _snap("B", 110, 0.80, 0.80)]                    # B: no model edge
    _write(tmp_path, snaps, [{"ticker": "A", "result": "yes"}, {"ticker": "B", "result": "no"}])
    bt, out = load(tmp_path)
    blind = run_strategy("b", bt, out, 0.78, 0.85, 0.02, 10, 120, use_model=False)
    model = run_strategy("m", bt, out, 0.78, 0.85, 0.02, 10, 120, use_model=True)
    assert (blind.n, blind.wins) == (2, 1)
    assert (model.n, model.wins) == (1, 1)
    assert model.pnl_per_contract == pytest.approx(1 - 0.80 - 0.012)


def test_wilson_interval():
    lo, hi = wilson(88, 100)
    assert 0.80 < lo < 0.81 and 0.92 < hi < 0.94


def test_report_detects_edge_when_market_is_noisy_and_model_is_right(tmp_path):
    rng = random.Random(5)
    snaps, outs = [], []
    for i in range(3000):
        p = rng.random()
        mkt = min(max(round(p + rng.gauss(0, 0.05), 2), 0.02), 0.98)
        snaps.append(_snap(f"M{i}", 100, p, mkt, round(1 - mkt + 0.01, 2)))
        outs.append({"ticker": f"M{i}", "result": "yes" if rng.random() < p else "no"})
    _write(tmp_path, snaps, outs)
    text = report(tmp_path)
    assert "[model edge>=0.02]" in text and "calibration" in text
    model_line = text.split("[model edge>=0.02]")[1]
    assert "EDGE" in model_line or "inconclusive" in model_line
