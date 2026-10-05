import json
import random

import pytest

from kalshi.dashboard import render
from kalshi.report import backtest, backtest_report, build_state, skip_tally, trades_report, wilson


def _w(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _trade(ticker, ts, status, **kw):
    return {"ticker": ticker, "ts": ts, "close_ts": ts + 60, "side": "yes", "count": 1, "price": 0.80, "fee": 0.02,
            "fair": 0.85, "edge": 0.03, "t_rem": 90, "order_id": "paper-x", "status": status, **kw}


def _snap(t, t_rem, fair, yes_ask, no_ask=None):
    return {"ticker": t, "t_rem": t_rem, "fair_yes": fair, "yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": None if no_ask is None else round(1 - no_ask, 4), "no_bid": round(1 - yes_ask, 4),
            "yes_bid_size": 10, "no_bid_size": 10}


def test_wilson_interval():
    lo, hi = wilson(88, 100)
    assert 0.80 < lo < 0.81 and 0.92 < hi < 0.94


def test_state_per_book_merges_open_and_settled(tmp_path):
    _w(tmp_path / "taker" / "trades.jsonl", [
        _trade("A", 1, "open"), _trade("A", 1, "settled", won=True, result="yes", pnl=0.18),
        _trade("B", 2, "open"), _trade("B", 2, "settled", won=False, result="no", pnl=-0.82),
        _trade("C", 3, "open")])
    _w(tmp_path / "maker" / "trades.jsonl", [])
    _w(tmp_path / "events.jsonl", [{"ts": i, "kind": "info", "msg": f"m{i}"} for i in range(5)])
    s = build_state(tmp_path)
    assert list(s["books"]) == ["taker", "maker"]
    sm = s["books"]["taker"]["summary"]
    assert (sm["settled"], sm["open"], sm["wins"], sm["losses"]) == (2, 1, 1, 1)
    assert sm["win_rate"] == 0.5 and sm["breakeven"] == pytest.approx(0.82) and sm["pnl"] == pytest.approx(-0.64)
    assert [c["pnl"] for c in s["books"]["taker"]["curve"]] == [0.18, -0.64]
    assert [t["ticker"] for t in s["books"]["taker"]["trades"]] == ["C", "B", "A"]
    assert s["events"][0]["msg"] == "m4" and s["status"] is None
    assert s["books"]["maker"]["summary"]["settled"] == 0
    assert "=== TAKER" in trades_report(tmp_path) and "GATE: NO-GO" in trades_report(tmp_path)


def test_skip_tally_reports_real_blocker_per_book():
    ev = [{"kind": "skip", "msg": "[taker] X | skip: yes ask 0.70 outside band; no ask 0.80 fair 0.79 edge -0.02 < 0.02"},
          {"kind": "skip", "msg": "[maker] X | skip: yes ask 0.70 outside band; no ask 0.30 outside band"}]
    assert skip_tally(ev, "taker") == [["no model edge", 1]]
    assert skip_tally(ev, "maker") == [["price outside band", 1]]


def test_backtest_one_trade_per_market_and_model_filter(tmp_path):
    _w(tmp_path / "snapshots.jsonl", [_snap("A", 110, 0.90, 0.80), _snap("A", 100, 0.90, 0.80),
                                      _snap("B", 110, 0.80, 0.80)])          # B: no model edge
    _w(tmp_path / "outcomes.jsonl", [{"ticker": "A", "result": "yes"}, {"ticker": "B", "result": "no"}])
    blind, model = backtest(tmp_path, use_model=False), backtest(tmp_path)
    assert (blind["n"], blind["wins"]) == (2, 1)
    assert (model["n"], model["wins"]) == (1, 1) and model["pnl"] == pytest.approx(1 - 0.80 - 0.012)


def test_backtest_detects_edge_when_market_is_noisy(tmp_path):
    rng = random.Random(5)
    snaps, outs = [], []
    for i in range(3000):
        p = rng.random()
        mkt = min(max(round(p + rng.gauss(0, 0.05), 2), 0.02), 0.98)
        snaps.append(_snap(f"M{i}", 100, p, mkt, round(1 - mkt + 0.01, 2)))
        outs.append({"ticker": f"M{i}", "result": "yes" if rng.random() < p else "no"})
    _w(tmp_path / "snapshots.jsonl", snaps)
    _w(tmp_path / "outcomes.jsonl", outs)
    text = backtest_report(tmp_path)
    assert "model edge>=0.02" in text and "calibration" in text
    model_line = text.split("model edge>=0.02")[1].split("\n")[1]
    assert "EDGE" in model_line or "inconclusive" in model_line
    assert backtest(tmp_path)["pnl"] > 0 > backtest(tmp_path, use_model=False)["pnl"]


def test_render_embeds_state_safely(tmp_path):
    _w(tmp_path / "events.jsonl", [{"ts": 1, "kind": "info", "msg": "</script><b>x"}])
    html = render(build_state(tmp_path))
    assert html.startswith("<!doctype html>") and "</script><b>x" not in html and "<\\/script>" in html
    live = render(None, full_document=False)
    assert "const SNAPSHOT = null;" in live and not live.startswith("<!doctype")
