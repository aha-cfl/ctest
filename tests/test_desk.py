import pytest

from kalshi.desk import read_jsonl
from kalshi.rules import RiskBook, RiskConfig, Rule
from tests.conftest import TICKER, FakeAPI, make_desk

T0 = 10_000.0            # FakeAPI close time


def trades(path):
    return read_jsonl(path / "trades.jsonl")


def test_favorite_buys_once_and_settles_win(tmp_path, favorite_api):
    logs = []
    d = make_desk(favorite_api, tmp_path, logs=logs)
    d.tick(T0 - 300)
    assert not d.books[0].traded and "waiting for final" in logs[-1]
    d.tick(T0 - 90)
    assert any(l.startswith("[taker] >>> BUY YES x1") for l in logs)
    d.tick(T0 - 80)
    assert sum("BUY" in l for l in logs) == 1                    # one order per market
    favorite_api.settle("yes")
    d.tick(T0 + 1)
    assert d.done
    rows = trades(tmp_path / "taker")
    assert [r["status"] for r in rows] == ["open", "settled"]
    assert rows[-1]["won"] and rows[-1]["pnl"] == pytest.approx(1 - 0.80 - 0.02)   # 1-contract fee rounds to 2c


def test_loss_is_full_cost(tmp_path, favorite_api):
    d = make_desk(favorite_api, tmp_path)
    d.tick(T0 - 90)
    favorite_api.settle("no")
    d.tick(T0 + 1)
    assert d.books[0].settled[0]["pnl"] == pytest.approx(-0.82)


def test_model_rule_skips_without_edge(tmp_path, favorite_api):
    logs = []
    d = make_desk(favorite_api, tmp_path, rules={"taker": Rule(mode="model")}, logs=logs)
    d.tick(T0 - 90)                                               # fair YES ~0.6-0.7 < 0.80 ask
    assert not d.books[0].traded and "skip:" in logs[-1] and "edge" in logs[-1]


def test_no_fill_without_depth(tmp_path):
    logs = []
    d = make_desk(FakeAPI(yes_bid=0.78, no_bid=0.20, depth="0.50"), tmp_path, logs=logs)
    d.tick(T0 - 90)
    assert not d.books[0].open_trades and "not filled" in logs[-1]


def test_one_market_read_per_tick_shared_by_both_books(tmp_path, favorite_api):
    d = make_desk(favorite_api, tmp_path, executions=("taker", "maker"))
    d.tick(T0 - 90)
    assert sum(u.endswith("/orderbook") for u in favorite_api.calls) == 1
    snaps = read_jsonl(tmp_path / "snapshots.jsonl")
    assert len(snaps) == 1 and snaps[0]["yes_ask"] == 0.80 and 0.5 < snaps[0]["fair_yes"] < 1
    assert d.books[0].traded and d.books[1].resting               # taker filled, maker resting
    favorite_api.settle("yes")
    d.tick(T0 + 1)
    assert read_jsonl(tmp_path / "outcomes.jsonl") == [
        {"ticker": TICKER, "result": "yes", "strike": 60000.0, "close_ts": T0}]


def test_gap_guard_and_vol_fields(tmp_path, favorite_api):
    logs = []
    d = make_desk(favorite_api, tmp_path, rules={"taker": Rule(mode="favorite", min_gap_usd=20)}, logs=logs)
    d.tick(T0 - 90)                                               # spot 60010 vs strike 60000 -> gap $10
    assert not d.books[0].traded and "basis guard" in logs[-1]
    assert d.cur["gap"] == pytest.approx(10) and d.cur["vol_realized"] > 0
    d.books[0].rule.min_gap_usd = 5
    d.books[0].last_skip = ("", -1e18)
    d.tick(T0 - 80)
    tr = d.books[0].open_trades[TICKER]
    assert tr["gap"] == pytest.approx(10) and "vol_ratio" in tr and tr["venues"] == "coinbase"


def test_kill_switch_blocks_entry(tmp_path, favorite_api):
    logs = []
    rb = RiskBook(tmp_path / "taker", RiskConfig(bankroll=100), kill_path=tmp_path / "KILL")
    (tmp_path / "KILL").touch()
    d = make_desk(favorite_api, tmp_path, risks={"taker": rb}, logs=logs)
    d.tick(T0 - 90)
    assert not d.books[0].traded and "kill switch" in logs[-1]
    (tmp_path / "KILL").unlink()
    d.books[0].last_skip = ("", -1e18)
    d.tick(T0 - 80)
    assert d.books[0].traded


def test_maker_fills_only_when_ask_trades_through(tmp_path, favorite_api):
    d = make_desk(favorite_api, tmp_path, executions=("maker",))
    d.tick(T0 - 100)
    book = d.books[0]
    assert book.resting[TICKER]["limit"] == 0.79 and not book.traded
    favorite_api.no_bid = 0.21                                    # ask 0.79 == limit: touch, not through
    d.tick(T0 - 90)
    assert book.resting and not book.traded
    favorite_api.no_bid = 0.22                                    # ask 0.78 < 0.79: through -> filled at 0.79
    d.tick(T0 - 80)
    tr = book.open_trades[TICKER]
    assert not book.resting and tr["price"] == 0.79 and tr["execution"] == "maker"
    assert tr["improvement"] == pytest.approx(0.01) and tr["fee"] < 0.02


def test_maker_cancels_near_close_and_does_not_replace(tmp_path, favorite_api):
    logs = []
    d = make_desk(favorite_api, tmp_path, executions=("maker",), logs=logs)
    d.tick(T0 - 100)
    d.tick(T0 - 4)
    assert not d.books[0].resting and not d.books[0].traded and "CANCEL" in logs[-1]
    d.tick(T0 - 3)
    assert not d.books[0].resting
    assert len(read_jsonl(tmp_path / "maker" / "maker_cancels.jsonl")) == 1


def test_maker_edge_gone_rule(tmp_path, favorite_api):
    d = make_desk(favorite_api, tmp_path, rules={"maker": Rule(mode="model")}, executions=("maker",))
    b, o = d.books[0], {"side": "yes", "limit": 0.80, "count": 1}
    assert not b._edge_gone(o, 0.90) and b._edge_gone(o, 0.81)
    assert not b._edge_gone({**o, "side": "no"}, 0.15) and b._edge_gone({**o, "side": "no"}, 0.20)


def test_settlement_updates_risk(tmp_path, favorite_api):
    rb = RiskBook(tmp_path / "taker", RiskConfig(bankroll=100))
    d = make_desk(favorite_api, tmp_path, risks={"taker": rb})
    d.tick(T0 - 90)
    favorite_api.settle("no")
    d.tick(T0 + 1)
    assert rb.bankroll == pytest.approx(99.18) and rb.day_pnl == pytest.approx(-0.82)


def test_restart_keeps_open_position_and_locked_average(tmp_path, favorite_api):
    d = make_desk(favorite_api, tmp_path, rules={"taker": Rule(mode="model")})
    for t in (50, 40, 30):                                        # inside the 60s averaging window, no trade
        d.tick(T0 - t)
    before = d.spot_samples[TICKER]
    d2 = make_desk(favorite_api, tmp_path, rules={"taker": Rule(mode="model")}).resume(now=T0 - 25)
    assert d2.spot_samples[TICKER] == before and TICKER in d2.pending

    fav = make_desk(favorite_api, tmp_path / "fav")
    fav.tick(T0 - 90)
    again = make_desk(favorite_api, tmp_path / "fav").resume(now=T0 - 60)
    assert TICKER in again.books[0].open_trades
    favorite_api.settle("yes")
    again.tick(T0 + 1)
    assert again.done and again.books[0].settled[0]["won"]


def test_one_book_error_does_not_stop_the_other(tmp_path, favorite_api):
    logs = []
    d = make_desk(favorite_api, tmp_path, executions=("taker", "maker"), logs=logs)
    d.books[1].step = lambda v, now: (_ for _ in ()).throw(RuntimeError("boom"))
    d.tick(T0 - 90)
    assert d.books[0].traded and any("[maker] error" in l for l in logs)
