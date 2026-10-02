import json

from kalshi.dashboard import build_state, render


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _trade(ticker, ts, status, **kw):
    base = {"ticker": ticker, "ts": ts, "close_ts": ts + 60, "side": "yes", "count": 1, "price": 0.80,
            "fee": 0.02, "fair": 0.85, "edge": 0.03, "t_rem": 90, "order_id": "paper-x", "status": status}
    return {**base, **kw}


def test_state_merges_open_and_settled_rows(tmp_path):
    _w(tmp_path / "trades.jsonl", [
        _trade("A", 1, "open"), _trade("A", 1, "settled", won=True, result="yes", pnl=0.18),
        _trade("B", 2, "open"), _trade("B", 2, "settled", won=False, result="no", pnl=-0.82),
        _trade("C", 3, "open"),
    ])
    _w(tmp_path / "events.jsonl", [{"ts": i, "kind": "info", "msg": f"m{i}"} for i in range(5)])
    s = build_state(tmp_path)
    sm = s["summary"]
    assert (sm["settled"], sm["open"], sm["wins"], sm["losses"]) == (2, 1, 1, 1)
    assert sm["win_rate"] == 0.5 and abs(sm["breakeven"] - 0.82) < 1e-9
    assert abs(sm["pnl"] - (-0.64)) < 1e-9
    assert [c["pnl"] for c in s["curve"]] == [0.18, -0.64]
    assert [t["ticker"] for t in s["trades"]] == ["C", "B", "A"]          # newest first
    assert s["events"][0]["msg"] == "m4" and s["status"] is None


def test_empty_dir_renders():
    from pathlib import Path
    s = build_state(Path("/nonexistent-dir"))
    assert s["summary"]["settled"] == 0 and s["summary"]["win_rate"] is None


def test_render_embeds_state_safely(tmp_path):
    _w(tmp_path / "events.jsonl", [{"ts": 1, "kind": "info", "msg": "</script><b>x"}])
    html = render(build_state(tmp_path))
    assert html.startswith("<!doctype html>")
    assert "</script><b>x" not in html and "<\\/script>" in html
    live = render(None, full_document=False)
    assert "const SNAPSHOT = null;" in live and not live.startswith("<!doctype")
