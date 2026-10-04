"""Step-by-step demo: 1% risk sizing, TP ladder, ratcheting stop. Run: python -m examples.walkthrough"""
from roundup_bot.broker import PaperBroker
from roundup_bot.config import Config, RiskConfig, validate
from roundup_bot.position import Bar, Position
from roundup_bot.sizing import size_position, stop_distance_frac


def run(title: str, equity: float, entry: float, bars: list[tuple]) -> None:
    cfg = validate(Config(risk=RiskConfig(risk_per_trade_pct=1.0, leverage=5)))
    r, lad = cfg.risk, cfg.ladder
    broker = PaperBroker(cash=equity, fee_rate=r.taker_fee_rate)

    print(f"\n=== {title} ===")
    s = size_position(equity, entry, lad, r)
    dist = stop_distance_frac(lad, r)
    print(f"equity ${equity:,.2f} | risk 1% = ${equity * 0.01:,.2f} | stop {lad.stop_loss_roe_pct:g}% ROE "
          f"@ {r.leverage:g}x = {dist:.1%} price move")
    print(f"notional = ${equity * 0.01:,.2f} / ({dist:.3f} + 2x{r.taker_fee_rate}) = ${s.notional:,.2f} "
          f"-> qty {s.qty:.4f} BTC, margin ${s.margin:,.2f}")

    broker.open(1, s.qty, entry, r.leverage)
    pos = Position(side=1, entry=entry, qty=s.qty, leverage=r.leverage, cfg=lad)
    print(f"OPEN long {s.qty:.4f} @ ${entry:,.0f} | stop ${pos.stop_price:,.0f}")

    for i, (o, h, l, c) in enumerate(bars, 1):
        stop_before = pos.stop_roe
        fills = pos.on_bar(Bar(str(i), o, h, l, c))
        for f in fills:
            broker.close(1, f.qty, f.price)
            print(f"  bar {i}: {f.reason:<10} sell {f.qty:.4f} @ ${f.price:,.0f} | "
                  f"left {pos.qty:.4f} | stop now {pos.stop_roe:g}% ROE (${pos.stop_price:,.0f})")
        if not fills and pos.stop_roe != stop_before:
            print(f"  bar {i}: tp@{pos.level_roe(pos.next_level - 1):g}%    runner kept, no sale | "
                  f"stop now {pos.stop_roe:g}% ROE (${pos.stop_price:,.0f})")
        elif not fills:
            print(f"  bar {i}: range ${l:,.0f}-${h:,.0f}, nothing triggered")
        if pos.qty == 0:
            break

    pnl = broker.equity() - equity
    print(f"RESULT: ${pnl:+,.2f} ({pnl / equity:+.2%} of equity), fees ${broker.fees_paid:,.2f}")


if __name__ == "__main__":
    # BTC at $60,000. At 5x: +50% ROE = +10% price = $66,000; stop -8% = $55,200.
    run("Winner: rides the ladder, runner stopped at +150% ROE", 1000, 60_000, [
        (60_000, 64_000, 59_000, 63_500),   # nothing
        (63_500, 66_500, 63_000, 66_000),   # +50%  -> sell 25%, stop to breakeven
        (66_000, 72_500, 65_500, 72_000),   # +100% -> sell 25%, stop to +50%
        (72_000, 78_200, 71_000, 78_000),   # +150% -> sell 25%, stop to +100%
        (78_000, 84_500, 77_500, 84_000),   # +200% -> keep runner, stop to +150%
        (84_000, 85_000, 77_000, 78_000),   # pullback hits +150% stop
    ])
    run("Loser: stopped out at -40% ROE = exactly 1% of equity", 1000, 60_000, [
        (60_000, 61_000, 58_000, 58_500),
        (58_500, 59_000, 55_000, 55_500),   # through $55,200 stop
    ])
    run("Scratch: +50% hit, then reverses to breakeven stop", 1000, 60_000, [
        (60_000, 66_200, 59_500, 65_000),
        (65_000, 65_500, 59_000, 59_500),
    ])
