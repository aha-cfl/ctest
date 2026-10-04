#!/usr/bin/env bash
# One command to run the Kalshi paper desk in THIS environment (no systemd, no server).
#   ./run_here.sh start [RULE] [MAX_TRADES]   default: favorite 1
#   ./run_here.sh status                      what is running + last trader lines
#   ./run_here.sh stop
# Output: data/live/{trades,events,snapshots,outcomes}.jsonl, status.json, desk.html (refreshed every 30s)
set -euo pipefail
cd "$(dirname "$0")"
DATA=data/live
RUN=$DATA/run
mkdir -p "$RUN"

check_network() {
  local ok=1
  for url in "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1" \
             "https://api.exchange.coinbase.com/products/BTC-USD/ticker"; do
    host=$(echo "$url" | cut -d/ -f3)
    if curl -sS -m 10 -o /dev/null -f "$url" 2>/dev/null; then
      echo "  ok       $host"
    else
      echo "  BLOCKED  $host"; ok=0
    fi
  done
  if [[ $ok -eq 0 ]]; then
    cat <<'MSG'

Network access is blocking the market data hosts. To fix:
  1. Click the cloud environment name in this session's title bar, then Edit.
  2. Network access -> Custom.
  3. Under Allowed domains add:  api.elections.kalshi.com
                                  api.exchange.coinbase.com
     (keep the default package-manager list checked)
  4. Save, then run ./run_here.sh start again.
Docs: https://code.claude.com/docs/en/cloud-environments#network-access
MSG
    exit 2
  fi
}

start_bg() {  # name, command...
  local name=$1; shift
  if [[ -f $RUN/$name.pid ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then
    echo "  $name already running (pid $(cat "$RUN/$name.pid"))"; return
  fi
  nohup "$@" >"$RUN/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "  started $name (pid $!, log $RUN/$name.log)"
}

case "${1:-start}" in
  start)
    RULE=${2:-favorite}; MAX=${3:-1}
    echo "network:"; check_network
    python3 -c "import requests" 2>/dev/null || pip install -q requests
    echo "processes:"
    start_bg recorder python3 -u -m kalshi --data "$DATA" record
    start_bg trader   python3 -u -m kalshi --data "$DATA" paper-trade --rule "$RULE" --max-trades "$MAX"
    start_bg snapshot bash -c "while true; do python3 -m kalshi --data $DATA export $DATA/desk.html \
        --label 'Live Kalshi data · paper fills' --fragment >/dev/null 2>&1; sleep 30; done"
    echo
    echo "Paper trading: rule=$RULE, stops after $MAX trade(s). No real orders are placed."
    echo "Watch:  ./run_here.sh status     Stop:  ./run_here.sh stop"
    ;;
  status)
    for n in recorder trader snapshot; do
      if [[ -f $RUN/$n.pid ]] && kill -0 "$(cat "$RUN/$n.pid")" 2>/dev/null; then s="running"; else s="stopped"; fi
      printf "  %-9s %s\n" "$n" "$s"
    done
    echo "--- trader (last 15 lines)"; tail -n 15 "$RUN/trader.log" 2>/dev/null || true
    echo "--- trades"; cat "$DATA/trades.jsonl" 2>/dev/null || echo "  none yet"
    ;;
  stop)
    for n in snapshot trader recorder; do
      [[ -f $RUN/$n.pid ]] && kill "$(cat "$RUN/$n.pid")" 2>/dev/null && echo "  stopped $n"
      rm -f "$RUN/$n.pid"
    done
    ;;
  *) echo "usage: $0 start [RULE] [MAX_TRADES] | status | stop"; exit 1 ;;
esac
