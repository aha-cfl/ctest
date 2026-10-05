# Kalshi BTC 15-minute paper desk

Watches Kalshi's **KXBTC15M** markets ("BTC up or down in 15 minutes"). In the final 2 minutes of each market it compares the order book to a model fair value and **paper-trades** when the model sees an edge. It then records Kalshi's real settlement and reports whether the edge is real. **No real orders are ever sent.**

```bash
pip install -r requirements.txt
./run_here.sh start          # one process, auto-restarts; needs api.elections.kalshi.com + api.exchange.coinbase.com
./run_here.sh report         # win rate vs breakeven per book + the real-money gate
./run_here.sh kill | resume  # block / allow new entries instantly (open positions still settle)
python -m kalshi demo        # the same desk against a simulated exchange, no network
```
On a server: `sudo ./deploy/install.sh` installs one systemd service (`Restart=always`, `RestartSec=1`). Dashboard: `ssh -L 8080:localhost:8080 you@server`, then open http://localhost:8080.

## How it decides
| Step | Where |
|---|---|
| BTC price = median of Coinbase/Kraken/Bitstamp/Gemini (BRTI proxy); 429 backoff | `kalshi/feeds.py` |
| Fair value with the 60s settlement average (locked part + remaining), implied vs realized vol, fees, Kelly | `kalshi/model.py` |
| Entry checks in order: 78–85¢ band → model edge ≥ 2¢ → $5 basis guard → spread / vol spike / news → kill switch / −$25 day stop → quarter-Kelly size, ≤ $5 per trade | `kalshi/rules.py` |
| One loop: one market read per tick, snapshot row, **taker** and **maker** paper books decide from the same view; restarts reload positions and the locked-average samples | `kalshi/desk.py` |
| Stats, the snapshot backtest, the gate, dashboard state | `kalshi/report.py`, `kalshi/dashboard.py` |
| Simulated exchange for demos, tests and parity checks | `kalshi/sim.py` |

## Data (`data/live/`)
`snapshots.jsonl` (every decision tick) · `outcomes.jsonl` · `events.jsonl` · `status.json` · `desk.html` (dashboard snapshot, refreshed every 30s) · `KILL` · `taker/` and `maker/` each with `trades.jsonl` and `risk.json`.

## Real-money gate (per book)
200+ settled trades **and** win-rate 95% CI lower bound above breakeven **and** the model's Brier score beats the market's **and** both halves of the sample are profitable. Until then: paper only.

## History
Every earlier version is frozen and runnable in [`archive/`](archive/README.md): v0.1 round-ups + leveraged perps, then the Kalshi recorder, paper trader, dashboard, and the v0.5 four-process desk. Git tags `v0.1-perps-roundup` … `v0.5-full-desk` mark the same commits. They were created locally; push them with `git push origin --tags` from a machine with tag-push rights.
