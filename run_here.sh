#!/usr/bin/env bash
# Run the Kalshi paper desk in this environment: ONE process, restarted within 1s if it ever exits.
#   ./run_here.sh start [extra run flags]   e.g. ./run_here.sh start --min-edge 0.03
#   ./run_here.sh status | report | kill | resume | stop
# Data: data/live (snapshots, outcomes, events, status, desk.html) + data/live/{taker,maker}/
set -euo pipefail
cd "$(dirname "$0")"
DATA=data/live
RUN=$DATA/run
mkdir -p "$RUN"
alive() { [[ -f $RUN/desk.pid ]] && kill -0 "$(cat "$RUN/desk.pid")" 2>/dev/null; }

migrate() {  # one-time move from the old 4-process layout
  if [[ -f $DATA/trades.jsonl || -f $DATA/risk.json ]]; then
    mkdir -p "$DATA/taker"
    for f in trades.jsonl risk.json; do [[ -f $DATA/$f ]] && mv "$DATA/$f" "$DATA/taker/$f"; done
    echo "  migrated taker book -> $DATA/taker/"
  fi
  if [[ -d data/live-maker ]]; then
    mkdir -p "$DATA/maker"
    for f in trades.jsonl risk.json maker_cancels.jsonl; do
      [[ -f data/live-maker/$f ]] && mv "data/live-maker/$f" "$DATA/maker/$f"
    done
    mv data/live-maker data/live-maker.old && rm -f "$DATA/desk-maker.html"
    echo "  migrated maker book -> $DATA/maker/ (leftovers in data/live-maker.old)"
  fi
}

case "${1:-status}" in
  start)
    shift || true
    if alive; then echo "already running (pid $(cat "$RUN/desk.pid"))"; exit 0; fi
    for h in api.elections.kalshi.com api.exchange.coinbase.com; do
      curl -sS -m 10 -o /dev/null "https://$h/" 2>/dev/null || {
        echo "BLOCKED: $h. Add it under Network access > Allowed domains (cloud environment settings)."; exit 2; }
    done
    python3 -c "import requests" 2>/dev/null || pip install -q requests
    migrate
    rm -f "$RUN/stop"
    nohup bash -c "while [[ ! -f $RUN/stop ]]; do python3 -u -m kalshi --data $DATA run $*; sleep 1; done" \
      >>"$RUN/desk.log" 2>&1 &
    echo $! >"$RUN/desk.pid"
    echo "desk started (pid $!, log $RUN/desk.log). Paper only: no real orders."
    ;;
  status)
    alive && echo "desk: running (pid $(cat "$RUN/desk.pid"))" || echo "desk: stopped"
    [[ -f $DATA/KILL ]] && echo "KILL SWITCH ON: no new entries (./run_here.sh resume)"
    tail -n 12 "$RUN/desk.log" 2>/dev/null || true
    ;;
  report) python3 -m kalshi --data "$DATA" report ;;
  kill)   touch "$DATA/KILL"; echo "kill switch ON: no new positions (open ones still settle)" ;;
  resume) rm -f "$DATA/KILL"; echo "kill switch OFF" ;;
  stop)
    touch "$RUN/stop"
    if alive; then pkill -P "$(cat "$RUN/desk.pid")" 2>/dev/null || true; kill "$(cat "$RUN/desk.pid")" 2>/dev/null || true; fi
    rm -f "$RUN/desk.pid"; echo "desk stopped"
    ;;
  *) echo "usage: $0 start [run flags] | status | report | kill | resume | stop"; exit 1 ;;
esac
