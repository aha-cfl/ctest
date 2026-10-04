"""Poll loop: in the last `window_s` seconds of each 15-min market, append order book + fair value
snapshots to JSONL; after close, record the settled result. No orders are ever placed."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .client import Coinbase, Kalshi
from .model import WINDOW_S, fair_yes, sigma_per_second


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class Recorder:
    kalshi: Kalshi
    spot_src: Coinbase
    out_dir: Path
    window_s: float = 120.0
    sigma_refresh_s: float = 60.0
    sigma: float | None = None
    sigma_ts: float = 0.0
    spot_samples: dict[str, list[float]] = field(default_factory=dict)  # ticker -> spots in final 60s
    pending: set[str] = field(default_factory=set)

    @property
    def snapshots_path(self) -> Path:
        return self.out_dir / "snapshots.jsonl"

    @property
    def outcomes_path(self) -> Path:
        return self.out_dir / "outcomes.jsonl"

    def _sigma(self, now: float) -> float:
        if self.sigma is None or now - self.sigma_ts >= self.sigma_refresh_s:
            self.sigma = sigma_per_second(self.spot_src.closes_1m(60))
            self.sigma_ts = now
        return self.sigma

    def tick(self, now: float) -> dict | None:
        """One poll. Returns the snapshot row written, if any."""
        self.resolve_pending()
        markets = [m for m in self.kalshi.open_markets() if m.close_ts > now]
        if not markets:
            return None
        m = markets[0]
        t_rem = m.close_ts - now
        if t_rem > self.window_s or m.strike is None:
            return None

        spot = self.spot_src.spot()
        if t_rem <= WINDOW_S:
            self.spot_samples.setdefault(m.ticker, []).append(spot)
        locked = self.spot_samples.get(m.ticker)
        locked_mean = sum(locked) / len(locked) if locked else None
        sigma = self._sigma(now)
        book = self.kalshi.book(m.ticker)
        fy = fair_yes(spot, m.strike, t_rem, sigma, locked_mean)

        row = {
            "ts": now, "ticker": m.ticker, "close_ts": m.close_ts, "t_rem": round(t_rem, 2),
            "strike": m.strike, "spot": spot, "locked_mean": locked_mean, "sigma_s": sigma,
            "fair_yes": round(fy, 5), **asdict(book), "yes_ask": book.yes_ask, "no_ask": book.no_ask,
        }
        append_jsonl(self.snapshots_path, row)
        self.pending.add(m.ticker)
        return row

    def resolve_pending(self) -> None:
        for ticker in list(self.pending):
            m = self.kalshi.market(ticker)
            if m.result in ("yes", "no"):
                append_jsonl(self.outcomes_path, {"ticker": ticker, "result": m.result,
                                                  "strike": m.strike, "close_ts": m.close_ts})
                self.pending.discard(ticker)
                self.spot_samples.pop(ticker, None)

    def run(self, poll_s: float = 2.0, idle_poll_s: float = 15.0) -> None:
        done = {o["ticker"] for o in read_jsonl(self.outcomes_path)}
        self.pending |= {s["ticker"] for s in read_jsonl(self.snapshots_path)} - done
        print(f"[recorder] writing to {self.out_dir} (pending {len(self.pending)})")
        while True:
            try:
                row = self.tick(time.time())
            except Exception as e:  # network blips must not kill a multi-week recording
                print(f"[recorder] error: {e!r}")
                time.sleep(idle_poll_s)
                continue
            if row:
                print(f"{row['ticker']} t-{row['t_rem']:>6.1f}s spot {row['spot']:,.2f} "
                      f"strike {row['strike']:,.2f} fair {row['fair_yes']:.3f} "
                      f"yes {row['yes_bid']}/{row['yes_ask']}")
            time.sleep(poll_s if row else idle_poll_s)
