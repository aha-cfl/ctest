# roundup_bot

Round-up spare change from bank transactions → sweep into a futures account → trend-following leveraged entries → auto take-profit ladder at **+50 / +100 / +150 / +200 % ROE**, then every +50 % after that with a ratcheting stop on the runner.

Paper mode is the default. Live mode requires `mode: live` in config **and** `ROUNDUP_BOT_LIVE=I_ACCEPT_TOTAL_LOSS`.

## Pipeline
| Stage | Module | Notes |
|---|---|---|
| Round-ups | `roundup.py` | CSV or Plaid `/transactions/sync`; idempotent ledger; sweeps at `min_sweep_usd` |
| Funding | `roundup.ManualSink` | Prints the transfer to make. Automating ACH needs a money-movement API (Plaid Transfer/Dwolla) or the broker's recurring deposit |
| Signal | `strategy.py` | EMA fast/slow crossover on closed bars |
| Sizing | `sizing.py` | Fixed-fractional: an initial stop-out costs `risk_per_trade_pct` (1–2 %) of equity, fees included; margin capped by `margin_fraction_per_trade` |
| Risk | `config.validate`, `engine.py` | Leverage cap, risk ≤ 2 %, stop must clear liquidation by a buffer, deposit-adjusted drawdown kill switch |
| Exits | `position.py` | Stop checked before TP within a bar (pessimistic); gap-through fills at the open |
| Execution | `broker.py` | `PaperBroker` (isolated-margin sim with fees) / `CcxtBroker` (reduce-only closes) |

## Ladder semantics
Levels are **ROE on margin**, not price. At 5x: +50 % ROE = +10 % price; −40 % ROE stop = −8 % price.

| Hit | Close (of original) | Stop moves to |
|---|---|---|
| +50 % | 25 % | 0 % (breakeven) |
| +100 % | 25 % | +50 % |
| +150 % | 25 % | +100 % |
| +200 % | 0 % (runner) | +150 % |
| +250, +300, … | 0 % | previous level |

## Position sizing
```
risk_usd = equity × risk_per_trade_pct
notional = risk_usd / (stop_distance + 2 × taker_fee)      stop_distance = |stop ROE| / leverage
```
At 5x with a −40 % ROE stop (8 % price) and 1 % risk, notional ≈ 12 % of equity and margin ≈ 2.5 %.
Leverage changes margin posted, **not** risk. Risk is set by stop distance × size.
Orders below `min_order_notional_usd` are skipped (`skip-too-small`); at 1 % risk that needs roughly $41+ equity.

## Usage
```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m roundup_bot --config config.yaml roundup --csv txns.csv        # or Plaid via env vars
python -m roundup_bot --config config.yaml backtest --prices bars.csv --cash 25 --monthly-deposit 30
python -m examples.walkthrough                                            # step-by-step demo of sizing + ladder
python -m roundup_bot --config config.yaml live                          # after paper validation
```
Plaid env: `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ACCESS_TOKEN`, `PLAID_ENV`. Exchange env: `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`.

## Known limits
- The risk budget is a ceiling for a normal stop. A price gap past the stop can lose more.
- Paper equity is realized-only (no mark-to-market of open positions).
- Backtest bars are processed on OHLC; intrabar order of TP vs stop is unknowable, so stop wins.
- Live loop does not reconcile state with the exchange after a restart — do not restart with an open position.
- US residents generally cannot legally access offshore crypto perps; regulated alternatives (CME micro futures) need ~$1–2k+ margin per contract, far above round-up balances.

---

# kalshi: KXBTC15M last-2-minute recorder (no trading)

Checks whether buying the ~80¢ favorite in Kalshi's 15-minute BTC up/down markets in the final 2 minutes actually has an edge. It **records only** and never places orders.

| Piece | File | What it does |
|---|---|---|
| Fair value | `kalshi/model.py` | P(YES) given spot, `floor_strike`, seconds left, realized vol, and the part of the 60s settlement average already locked in |
| Data | `kalshi/client.py` | Public Kalshi REST v2 (no key) + Coinbase Exchange spot/1m candles as the BRTI proxy |
| Recorder | `kalshi/recorder.py` | Every 2s in the last 120s: book + fair value → `data/kalshi/snapshots.jsonl`; settled result → `outcomes.jsonl` |
| Scoring | `kalshi/analyze.py` | One trade per market; win rate with Wilson 95% CI vs breakeven (ask + fee); Brier score model vs market; calibration table |

```bash
python -m kalshi record                 # leave running for weeks (tmux / systemd / a small VPS)
python -m kalshi resolve                # backfill outcomes after a restart
python -m kalshi analyze --lo 0.78 --hi 0.85 --min-edge 0.02 --contracts 10
python -m examples.kalshi_demo 600      # dry run vs a simulated exchange (no network)
```

Needs outbound access to `api.elections.kalshi.com` and `api.exchange.coinbase.com`.

**Decision rule:** trade only if the model strategy's CI lower bound beats breakeven **and** model Brier < market Brier. Then size at ≤ ¼ Kelly.

Known model limits: Coinbase ≠ BRTI (basis risk); vol is 60-min realized (no jumps/fat tails); locked-average is estimated from our own 2s samples, not the 1s BRTI prints; fees assume taker at 10 contracts (1-contract orders round the fee up to 2¢).

## Paper trading (one trade, end to end)
`kalshi/trader.py` runs the full loop: wait for the final 120s → compute fair value → buy if the rule passes → wait for Kalshi's **real** settlement → log P&L to `trades.jsonl` → exit after `--max-trades`. Fills are simulated at the quoted ask (`PaperExecutor`), capped by displayed depth, with the real fee (1 contract rounds up to 2¢). **No orders are sent to Kalshi.**

```bash
python -m kalshi paper-trade --max-trades 1                 # model rule: may wait many markets for an edge
python -m kalshi paper-trade --max-trades 1 --rule favorite # blind 78-85c favorite: trades within ~1 market
python -m examples.kalshi_trade_demo                        # same trader vs simulated exchange, no network
```

## Deploy on a server (systemd)
```bash
git clone <this repo> && cd ctest
sudo ./deploy/install.sh                       # installs to /opt/kalshi, data in /var/lib/kalshi, starts recorder
sudo systemctl start kalshi-paper-trader       # one paper trade, then the service stops itself
journalctl -fu kalshi-paper-trader             # watch it live
sudo cat /var/lib/kalshi/trades.jsonl
```
| Unit | Behavior |
|---|---|
| `kalshi-recorder.service` | Always on, restarts on failure, enabled at boot |
| `kalshi-paper-trader.service` | Runs until `MAX_TRADES` settle, then exits 0 and stays stopped; restarts only on crash |

Tune via `/etc/kalshi/paper-trader.env` (`RULE`, `MAX_TRADES`, `CONTRACTS`, `MIN_EDGE`). Both units run as an unprivileged `kalshi` user with a read-only filesystem except the data dir.

## Dashboard
Read-only view of the trader: summary numbers, the market being evaluated (countdown, spot vs strike, fair vs ask on a 0–100¢ gauge), cumulative P&L, every execution, and the decision log. It reads `status.json`, `trades.jsonl` and `events.jsonl` from the data dir.

```bash
python -m kalshi --data /var/lib/kalshi dashboard            # http://127.0.0.1:8080, refreshes every 2s
python -m kalshi --data DIR export desk.html                 # static snapshot, no server
python -m examples.kalshi_trade_demo --favorite --max-trades 25 --data /tmp/desk --quiet
python -m kalshi --data /tmp/desk dashboard                  # browse the simulated run
```
`install.sh` also enables `kalshi-dashboard.service`. It binds to localhost only (no login), so view it from your laptop with `ssh -L 8080:localhost:8080 you@server` and open http://localhost:8080.

## Strategy upgrades (paper)
| Piece | File | Default |
|---|---|---|
| Composite BTC price (median of Coinbase, Kraken, Bitstamp, Gemini; skips unreachable venues) | `kalshi/prices.py` | on |
| Market-implied vs realized vol on every decision; optional filter | `kalshi/model.py` `implied_sigma` | logged; `--min-vol-ratio 0` (off) |
| Basis guard: expected 60s-average settlement within $X of strike | `kalshi/trader.py` | `--min-gap 5` |
| Wide spread / 5-min vol spike / scheduled US macro releases | `kalshi/filters.py` | 3¢, 2×, on (`--no-news-filter`) |
| Quarter-Kelly sizing, $ cap per trade, daily loss stop, kill switch | `kalshi/risk.py` | `--bankroll 100 --per-trade-cap 5 --daily-loss-cap 25`, `data/live/KILL` |
| Maker execution: bid 1¢+ under the ask, fill only on trade-through, cancel when edge is gone or at t-5s | `kalshi/trader.py` | `--execution maker` |
| Signal alerts: dashboard banner; optional phone push via ntfy | `--ntfy-topic` | banner on |
| Live report + real-money gate (200 trades, CI > breakeven, model Brier < market, both halves profitable) | `kalshi/analyze.py` | `python -m kalshi report` |

`./run_here.sh start` now runs a taker and a maker paper trader side by side (`data/live`, `data/live-maker`); `./run_here.sh report | kill | resume` manage them.

In simulation, naive maker orders lost money to adverse selection (fills happen when price moves against you); cancelling when the model edge disappears fixed most of it. Live data decides whether maker beats taker.
