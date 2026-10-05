"""Turn alert text into a structured call. Anything unclear raises ParseError instead of guessing.

Accepted, case-insensitive, extra words ignored:
  LONG STRK 0.0590 SL 0.0570 10x      short btc now sl 2%      buy $STRK @0.059
  STRKUSDT long entry 0.059 stop 0.057 tp 0.062 0.065
No SL in the text -> the source's standard SL (a % from entry) is applied and flagged.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

SIDES = {"long": 1, "buy": 1, "short": -1, "sell": -1}
NOISE = {"entry", "at", "now", "market", "mkt", "sl", "stop", "stoploss", "tp", "target", "targets", "lev",
         "leverage", "cross", "isolated", "perp", "perps", "usdt", "usd", "spot", "x", "and", "the", "on", "in"}
NUM = r"(\d+(?:\.\d+)?)"


class ParseError(ValueError):
    pass


@dataclass
class Call:
    side: int                    # +1 long, -1 short
    symbol: str                  # base asset, e.g. STRK
    entry: float | None          # None = market at alert time
    sl: float | None = None      # absolute stop price, resolved from % at fill time if needed
    sl_pct: float | None = None  # stop as % from entry (alert's own % or the standard SL)
    sl_source: str = "alert"     # alert | standard
    tps: list[float] = field(default_factory=list)
    leverage: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse(text: str, standard_sl_pct: float | None = None) -> Call:
    t = text.lower().replace(",", " ").replace("$", " ")
    side_m = re.search(r"\b(long|short|buy|sell)\b", t)
    if not side_m:
        raise ParseError("no side (LONG/SHORT/BUY/SELL)")
    side = SIDES[side_m.group(1)]

    lev_m = re.search(rf"\b{NUM}\s*x\b", t)
    leverage = float(lev_m.group(1)) if lev_m else None
    t_wo_lev = t[:lev_m.start()] + " " + t[lev_m.end():] if lev_m else t

    sl_m = re.search(rf"\b(?:sl|stop(?:\s*loss)?|stoploss)\b\s*[:=@]?\s*{NUM}\s*(%)?", t_wo_lev)
    tp_m = re.search(rf"\b(?:tp|targets?)\b\s*[:=@]?\s*((?:{NUM}\s*/?\s*)+)", t_wo_lev)
    cut = [m.span() for m in (sl_m, tp_m) if m]
    rest = t_wo_lev
    for a, b in sorted(cut, reverse=True):
        rest = rest[:a] + " " + rest[b:]

    words = re.findall(r"[a-z][a-z0-9]*", rest.replace(side_m.group(1), " ", 1))
    syms = [w for w in words if w not in SIDES and w not in NOISE]
    if not syms:
        raise ParseError("no symbol")
    symbol = re.sub(r"(usdt|usdc|usd|perp)$", "", syms[0]).upper()
    if not symbol:
        raise ParseError("no symbol")

    nums = [float(n) for n in re.findall(NUM, rest) if not re.fullmatch(r"\d+", n) or float(n) > 0]
    market = bool(re.search(r"\b(now|market|mkt)\b", rest))
    entry = None if market or not nums else nums[0]
    if not market and len(nums) > 1:
        raise ParseError(f"ambiguous entry: several numbers {nums}")

    call = Call(side, symbol, entry, leverage=leverage,
                tps=[float(x) for x in re.findall(NUM, tp_m.group(1))] if tp_m else [])
    if sl_m:
        if sl_m.group(2):
            call.sl_pct = float(sl_m.group(1))
        else:
            call.sl = float(sl_m.group(1))
    elif standard_sl_pct:
        call.sl_pct, call.sl_source = standard_sl_pct, "standard"
    else:
        raise ParseError("no stop-loss and no standard SL set (python -m alerts config --sl 3)")

    if call.entry is not None and call.sl is not None:
        if (call.side > 0 and call.sl >= call.entry) or (call.side < 0 and call.sl <= call.entry):
            raise ParseError(f"stop {call.sl} is on the wrong side of entry {call.entry} for a "
                             f"{'long' if call.side > 0 else 'short'}")
    return call


def resolve_stop(call: Call, fill: float) -> float:
    """Absolute stop price once the fill price is known."""
    if call.sl is not None:
        return call.sl
    return fill * (1 - call.side * call.sl_pct / 100)
