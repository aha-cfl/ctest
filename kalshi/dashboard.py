"""Read-only dashboard over the trader's files: status.json, trades.jsonl, events.jsonl.

  serve  : stdlib HTTP server; the page polls /api/state every 2s. Binds 127.0.0.1 by default
           (there is no login). View remotely with: ssh -L 8080:localhost:8080 you@server
  export : one self-contained HTML file with the state embedded (no server needed)
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .recorder import read_jsonl

TEMPLATE = Path(__file__).with_name("dashboard.html")


def build_state(data_dir: Path, events_tail: int = 150, label: str | None = None) -> dict:
    data_dir = Path(data_dir)
    latest: dict[str, dict] = {}
    for row in read_jsonl(data_dir / "trades.jsonl"):        # settled row supersedes open row
        latest[row["ticker"]] = row
    trades = sorted(latest.values(), key=lambda r: r["ts"])
    settled = [t for t in trades if t["status"] == "settled"]

    cum, curve = 0.0, []
    for t in settled:
        cum += t["pnl"]
        curve.append({"ts": t["close_ts"], "pnl": round(cum, 4), "won": t["won"]})

    n = len(settled)
    wins = sum(t["won"] for t in settled)
    avg_px = sum(t["price"] for t in settled) / n if n else None
    staked = sum(t["count"] * t["price"] + t["fee"] for t in settled)
    contracts = sum(t["count"] for t in settled)
    status_path = data_dir / "status.json"
    return {
        "generated": time.time(),
        "label": label,
        "status": json.loads(status_path.read_text()) if status_path.exists() else None,
        "summary": {
            "settled": n, "open": len(trades) - n, "wins": wins, "losses": n - wins,
            "win_rate": wins / n if n else None,
            "avg_price": avg_px,
            # cost per contract actually paid (price + fee) = win rate needed to break even
            "breakeven": staked / contracts if contracts else None,
            "pnl": round(cum, 4), "staked": round(staked, 4),
            "roi": cum / staked if staked else None,
            "avg_edge": sum(t["edge"] for t in settled) / n if n else None,
        },
        "curve": curve,
        "trades": list(reversed(trades)),
        "events": list(reversed(read_jsonl(data_dir / "events.jsonl")[-events_tail:])),
    }


def render(state: dict | None, full_document: bool = True) -> str:
    """state=None -> live page that polls /api/state; otherwise a static snapshot."""
    html = TEMPLATE.read_text()
    payload = "null" if state is None else json.dumps(state).replace("</", "<\\/")
    html = html.replace("/*__STATE__*/null", payload)
    return f'<!doctype html>\n<html lang="en">\n{html}\n</html>\n' if full_document else html


def export(data_dir: Path, out: Path, full_document: bool = True, label: str | None = None) -> Path:
    out = Path(out)
    out.write_text(render(build_state(data_dir, label=label), full_document))
    return out


def serve(data_dir: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    page = render(None).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/api/state":
                body, ctype = json.dumps(build_state(data_dir)).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = page, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep journald quiet
            pass

    print(f"[dashboard] http://{host}:{port}  (data: {data_dir})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
