import pytest

from kalshi.feeds import CompositeSpot, Kalshi, parse_book, parse_market


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


def test_composite_median_benches_dead_and_drops_outlier():
    calls, clock = [], [1000.0]

    def get(url, params=None):
        calls.append(url)
        if url == "dead":
            raise ConnectionError("blocked")
        return {}

    srcs = {"a": ("ok", lambda j: 100.0), "b": ("ok", lambda j: 100.2), "c": ("ok", lambda j: 130.0),
            "d": ("dead", lambda j: 0.0)}
    cs = CompositeSpot(get, srcs, retry_s=300, clock=lambda: clock[0])
    assert cs.spot() == pytest.approx(100.1)                # c is >0.3% off the median -> ignored
    assert set(cs.last_quotes) == {"a", "b"} and "d" in cs.benched and cs.venues == "a,b"
    n = calls.count("dead")
    cs.spot()
    assert calls.count("dead") == n                          # benched: not retried
    clock[0] += 301
    cs.spot()
    assert calls.count("dead") == n + 1                      # retried after cool-off


def test_composite_raises_when_nothing_reachable():
    def get(url, params=None):
        raise OSError("down")
    with pytest.raises(RuntimeError):
        CompositeSpot(get, {"x": ("u", float)}).spot()


def test_http_get_retries_on_429(monkeypatch):
    import kalshi.feeds as f
    calls, sleeps = [], []

    class R:
        def __init__(self, code, headers=None):
            self.status_code, self.headers = code, headers or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return {"ok": True}

    seq = [R(429, {"Retry-After": "0.5"}), R(429), R(200)]
    monkeypatch.setattr(f.requests, "get", lambda *a, **k: calls.append(1) or seq.pop(0))
    monkeypatch.setattr(f.time, "sleep", sleeps.append)
    assert f.http_get("u") == {"ok": True}
    assert len(calls) == 3 and sleeps == [0.5, 2]


def test_open_markets_cached():
    hits = []

    def get(url, params=None):
        hits.append(url)
        return {"markets": []}

    k = Kalshi(get, markets_ttl=60)
    k.open_markets(); k.open_markets()
    assert len(hits) == 1
    k0 = Kalshi(get, markets_ttl=0)
    k0.open_markets(); k0.open_markets()
    assert len(hits) == 3
