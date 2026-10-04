from kalshi.client import Coinbase, Kalshi
from kalshi.execution import PaperExecutor
from kalshi.recorder import read_jsonl
from kalshi.trader import Rule, Trader
from tests.test_kalshi import FakeAPI


class BookAPI(FakeAPI):
    """FakeAPI with a configurable book: yes_bid / no_bid in dollars."""
    def __init__(self, close_ts, yes_bid, no_bid, depth="5"):
        super().__init__(close_ts)
        self.yes_bid, self.no_bid, self.depth = yes_bid, no_bid, depth

    def __call__(self, url, params=None):
        if url.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [[f"{self.yes_bid:.4f}", self.depth]],
                                     "no_dollars": [[f"{self.no_bid:.4f}", self.depth]]}}
        return super().__call__(url, params)


def mk(api, tmp_path, rule):
    logs = []
    t = Trader(Kalshi(api), Coinbase(api), PaperExecutor(), tmp_path, rule, max_trades=1, log=logs.append)
    return t, logs


def test_favorite_rule_buys_once_and_settles_win(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)      # yes ask 0.80, no ask 0.22
    t, logs = mk(api, tmp_path, Rule(mode="favorite"))
    t.tick(10_000 - 300)
    assert not t.traded_tickers and "waiting for final" in logs[-1]
    t.tick(10_000 - 90)
    assert any(l.startswith(">>> BUY YES x1") for l in logs)
    t.tick(10_000 - 80)                                       # no second order in same market
    assert len([l for l in logs if l.startswith(">>> BUY")]) == 1
    api.status, api.result = "settled", "yes"
    t.tick(10_001)
    assert t.done
    rows = read_jsonl(tmp_path / "trades.jsonl")
    assert [r["status"] for r in rows] == ["open", "settled"]
    s = rows[-1]
    assert s["won"] and s["payout"] == 1.0
    assert abs(s["pnl"] - (1 - 0.80 - 0.02)) < 1e-9           # 1-contract fee rounds up to 2c


def test_loss_is_full_cost(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    t, _ = mk(api, tmp_path, Rule(mode="favorite"))
    t.tick(10_000 - 90)
    api.status, api.result = "settled", "no"
    t.tick(10_001)
    assert t.settled[0]["pnl"] == -0.82 and not t.settled[0]["won"]


def test_model_rule_skips_without_edge(tmp_path):
    # spot 60010 vs strike 60000 at t-90 gives fair YES ~0.6-0.7; YES ask 0.80 is overpriced
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20)
    t, logs = mk(api, tmp_path, Rule(mode="model"))
    t.tick(10_000 - 90)
    assert not t.traded_tickers and "skip:" in logs[-1] and "edge" in logs[-1]


def test_no_fill_without_depth(tmp_path):
    api = BookAPI(10_000.0, yes_bid=0.78, no_bid=0.20, depth="0.50")
    t, logs = mk(api, tmp_path, Rule(mode="favorite"))
    t.tick(10_000 - 90)
    assert not t.open_trades and "not filled" in logs[-1]
