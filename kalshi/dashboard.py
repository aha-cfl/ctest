"""Dashboard over the data dir (state comes from report.build_state).

  serve  : stdlib HTTP server; the page polls /api/state every 2s. Binds 127.0.0.1 (no login).
           View remotely with: ssh -L 8080:localhost:8080 you@server
  export : one self-contained HTML file with the state embedded (no server needed)
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .report import build_state

TEMPLATE = Path(__file__).with_name("dashboard.html")


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
