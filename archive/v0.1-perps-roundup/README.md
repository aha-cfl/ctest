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
