"""Paper-trade every alert on 1-minute candles and score it in R (multiples of the stop distance).

Lifecycle per alert:
  pending  -> market alerts fill at the price when added; limit alerts fill when a candle trades through
  open     -> each new candle: stop checked first (pessimistic), then max favorable excursion
  closed   -> stopped (R = -1, or worse on a gap) or expired after `hold_hours` at the last close
  unfilled -> limit never traded within `hold_hours`
  rejected -> text could not be parsed (reason kept)
Dollar views: `margin_usd` at the alert's leverage (or default_leverage), and `risk_usd` per alert (R x risk).

Files in <data>/alerts/: inbox.jsonl (raw adds, append-only), state.json (results), config.json.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from kalshi.desk import append_jsonl, read_jsonl

from .parse import Call, ParseError, parse, resolve_stop
from .prices import Candles


@dataclass
class AuditConfig:
    standard_sl_pct: float | None = None   # the source's one stop rule, % from entry
    hold_hours: float = 24.0
    margin_usd: float = 20000.0            # "at your size" view
    default_leverage: float = 10.0
    risk_usd: float = 100.0                # fixed-risk view: $ lost when the stop is hit


def load_config(d: Path) -> AuditConfig:
    p = Path(d) / "config.json"
    return AuditConfig(**json.loads(p.read_text())) if p.exists() else AuditConfig()


def save_config(d: Path, cfg: AuditConfig) -> None:
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / "config.json").write_text(json.dumps(asdict(cfg), indent=2))


class Auditor:
    def __init__(self, data_dir: Path, candles: Candles | None = None, clock=time.time):
        self.dir = Path(data_dir)
        self.candles, self.clock = candles or Candles(), clock

    # ---- input ---------------------------------------------------------------------------------
    def add(self, text: str, ts: float | None = None) -> dict:
        """Record an alert. Returns the row (parsed call or rejection reason). Safe to call from another thread."""
        ts = self.clock() if ts is None else ts
        row = {"id": uuid.uuid4().hex[:8], "ts": ts, "text": text.strip()}
        try:
            call = parse(text, load_config(self.dir).standard_sl_pct)
            row["call"] = call.to_dict()
            if call.entry is None:            # market: snapshot the price now so the fill is honest
                row["market_price"] = self.candles.last(call.symbol)
        except ParseError as e:
            row["error"] = str(e)
        except Exception as e:
            row["error"] = f"price lookup failed: {e!r}"
        append_jsonl(self.dir / "inbox.jsonl", row)
        return row

    # ---- scoring --------------------------------------------------------------------------------
    def _state(self) -> dict:
        p = self.dir / "state.json"
        return json.loads(p.read_text()) if p.exists() else {"alerts": {}}

    def _save(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.dir / "state.json")

    def update(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        cfg = load_config(self.dir)
        st = self._state()
        for row in read_jsonl(self.dir / "inbox.jsonl"):
            a = st["alerts"].setdefault(row["id"], {**row, "status": "rejected" if "error" in row else "pending",
                                                    "last_ts": row["ts"]})
            if a["status"] in ("closed", "unfilled", "rejected"):
                continue
            try:
                self._advance(a, cfg, now)
            except Exception as e:  # price outage: retry next minute
                a["note"] = f"price error: {e!r}"
        st["updated"] = now
        st["config"] = asdict(cfg)
        self._save(st)
        return st

    def _advance(self, a: dict, cfg: AuditConfig, now: float) -> None:
        call = Call(**a["call"])
        expiry = a["ts"] + cfg.hold_hours * 3600
        if a["status"] == "pending" and call.entry is None:
            self._fill(a, call, a["market_price"], a["ts"])
        rows = self.candles.fetch(call.symbol, a["last_ts"], min(now, expiry + 60))
        for t, o, h, l, c in rows:
            a["last_ts"] = t + 60
            if a["status"] == "pending":
                if call.side * (l if call.side > 0 else h) <= call.side * call.entry:   # traded through entry
                    self._fill(a, call, call.entry, t)
                else:
                    continue
            self._step(a, call, cfg, t, o, h, l, c)
            if a["status"] == "closed":
                return
        a["mark"] = rows[-1][4] if rows else a.get("mark")
        if a["status"] == "open" and a.get("mark"):
            a["r_now"] = round(self._r(a, a["mark"]), 3)
        if now >= expiry:
            if a["status"] == "pending":
                a["status"] = "unfilled"
            elif a["status"] == "open" and a.get("mark"):
                self._close(a, a["mark"], expiry, "expired", cfg)

    def _fill(self, a: dict, call: Call, price: float, t: float) -> None:
        stop = resolve_stop(call, price)
        a.update(status="open", fill=price, fill_ts=t, stop=stop, risk=abs(price - stop), mfe_r=0.0, mae_r=0.0,
                 marks={}, hit={"1R": False, "2R": False, "3R": False})

    def _r(self, a: dict, price: float) -> float:
        return (price - a["fill"]) * (1 if a["call"]["side"] > 0 else -1) / a["risk"]

    def _step(self, a: dict, call: Call, cfg: AuditConfig, t, o, h, l, c) -> None:
        adverse, favorable = (l, h) if call.side > 0 else (h, l)
        if call.side * (adverse - a["stop"]) <= 0:                     # stop first (pessimistic)
            gapped = call.side * (o - a["stop"]) < 0
            self._close(a, o if gapped else a["stop"], t, "stopped", cfg)
            return
        a["mfe_r"] = round(max(a["mfe_r"], self._r(a, favorable)), 3)
        a["mae_r"] = round(min(a["mae_r"], self._r(a, adverse)), 3)
        for k, lvl in (("1R", 1), ("2R", 2), ("3R", 3)):
            a["hit"][k] = a["hit"][k] or a["mfe_r"] >= lvl
        for label, sec in (("1h", 3600), ("4h", 14400), ("24h", 86400)):
            if label not in a["marks"] and t + 60 >= a["fill_ts"] + sec:
                a["marks"][label] = round(self._r(a, c), 3)

    def _close(self, a: dict, price: float, t: float, how: str, cfg: AuditConfig) -> None:
        r = self._r(a, price)
        lev = a["call"].get("leverage") or cfg.default_leverage
        move = (price / a["fill"] - 1) * (1 if a["call"]["side"] > 0 else -1)
        a.update(status="closed", exit=price, exit_ts=t, how=how, r=round(r, 3), move_pct=round(move * 100, 3),
                 usd_at_size=round(cfg.margin_usd * lev * move, 2), usd_fixed_risk=round(cfg.risk_usd * r, 2),
                 leverage_used=lev)


# ---- summary for reports / dashboard --------------------------------------------------------
def summary(data_dir: Path) -> dict | None:
    p = Path(data_dir) / "state.json"
    if not p.exists():
        return None
    st = json.loads(p.read_text())
    alerts = sorted(st["alerts"].values(), key=lambda a: -a["ts"])
    closed = sorted((a for a in alerts if a["status"] == "closed"), key=lambda a: a["exit_ts"])
    n = len(closed)
    wins = sum(a["r"] > 0 for a in closed)
    from kalshi.report import wilson
    lo, hi = wilson(wins, n)
    rs = [a["r"] for a in closed]
    mean = sum(rs) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else None
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    for a in closed:
        eq += a["usd_fixed_risk"]
        peak, dd = max(peak, eq), max(dd, peak - eq)
        curve.append({"ts": a["exit_ts"], "pnl": round(eq, 2), "won": a["r"] > 0})
    by_sym: dict[str, dict] = {}
    for a in closed:
        s = by_sym.setdefault(a["call"]["symbol"], {"n": 0, "wins": 0, "r": 0.0})
        s["n"] += 1; s["wins"] += a["r"] > 0; s["r"] = round(s["r"] + a["r"], 3)
    ci_r = (mean - 1.96 * sd / math.sqrt(n), mean + 1.96 * sd / math.sqrt(n)) if sd else None
    return {
        "updated": st.get("updated"), "config": st.get("config"),
        "counts": {k: sum(a["status"] == k for a in alerts) for k in ("pending", "open", "closed", "unfilled", "rejected")},
        "closed": n, "wins": wins, "losses": n - wins, "win_rate": wins / n if n else None, "ci": [lo, hi],
        "avg_r": mean, "ci_r": ci_r, "usd_at_size": round(sum(a["usd_at_size"] for a in closed), 2),
        "usd_fixed_risk": round(eq, 2), "max_drawdown_fixed_risk": round(dd, 2), "curve": curve,
        "by_symbol": by_sym,
        "gate": {"enough": n >= 30, "positive": bool(mean and mean > 0), "ci_excludes_zero": bool(ci_r and ci_r[0] > 0)},
        "alerts": alerts[:200],
    }
