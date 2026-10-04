# Archive: every version, frozen and runnable

Each folder is an exact copy of the repo at a git tag (`git archive <tag>`). Nothing here is imported by the active code. Each has its own `pytest.ini`, so its tests run in place:

```bash
cd archive/v0.5-full-desk && python -m pytest
```

You can also check out any version directly with `git checkout <tag>`, or `git worktree add ../v01 v0.1-perps-roundup`.

| Version | Tag / commit | What it was | What it taught |
|---|---|---|---|
| `v0.1-perps-roundup` | `816708b` | Bank round-ups → leveraged BTC perps, EMA crossover entries, take-profit ladder at +50/100/150/200% ROE with a ratcheting stop, 1–2% risk sizing, step-by-step walkthrough | Leverage doesn't set risk; stop distance does. On random prices the strategy lost to fees and whipsaw. Offshore perps are mostly off-limits for US users, and round-up balances sit below exchange minimums. |
| `v0.2-kalshi-recorder` | `6519d25` | Kalshi 15-min BTC up/down: fair value from the 60s BRTI settlement average, order-book recorder, edge analyzer with Wilson confidence intervals | Buying the 80¢ favorite blindly loses about the fee (−1.2¢/contract in simulation). Proving a real edge takes about 3,000 markets. |
| `v0.3-paper-trader` | `556e4fa` | One-trade paper trader, systemd units, simulated exchange demo | Needs network access to Kalshi and Coinbase. One winning trade proves nothing. |
| `v0.4-dashboard` | `1a77177` | Read-only dashboard (live server + static export), `run_here.sh` one-command launcher | Watching decisions and skip reasons matters more than P&L early on. |
| `v0.5-full-desk` | `8f3f642` | Composite 4-exchange price, implied vs realized vol, $5 basis guard, spread/vol-spike/news filters, quarter-Kelly with $5/trade and −$25/day caps, kill switch, paper maker orders, go/no-go gate, 429 backoff. Ran as 4 processes. | Naive maker bids get adversely selected (−$11 vs +$25 taker in simulation) until bids cancel when the edge disappears. The market priced near-strike cases better than a single-venue proxy. 4 processes polling the same books hit Kalshi rate limits. |

Note: in `v0.5-full-desk`, `examples/kalshi_trade_demo.py` uses the 15-second market-list cache added in that version, which can make the fast simulated clock see a stale market list. Pass `markets_ttl=0` to `Kalshi(...)` when experimenting there. The active code fixes this in `kalshi/sim.py`.
