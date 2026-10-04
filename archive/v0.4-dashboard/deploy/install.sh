#!/usr/bin/env bash
# Install the Kalshi recorder + paper trader as systemd services on Debian/Ubuntu.
# Usage (from the repo root, as root):  sudo ./deploy/install.sh [--start-paper-trade]
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/kalshi DATA=/var/lib/kalshi ETC=/etc/kalshi

echo "==> packages"
command -v python3 >/dev/null || apt-get install -y python3
python3 -c 'import venv, ensurepip' 2>/dev/null || { apt-get update -y && apt-get install -y python3-venv; }

echo "==> user + dirs"
id kalshi &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin kalshi
install -d -o kalshi -g kalshi -m 750 "$DATA"
install -d -m 755 "$APP" "$ETC"

echo "==> code -> $APP"
rm -rf "$APP/kalshi"
cp -r "$REPO/kalshi" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip requests
[[ -f "$ETC/paper-trader.env" ]] || cp "$REPO/deploy/paper-trader.env.example" "$ETC/paper-trader.env"

echo "==> connectivity check"
(cd "$APP" && "$APP/.venv/bin/python" - <<'PY'
from kalshi.client import Coinbase, Kalshi
m = Kalshi().open_markets()
print(f"   kalshi ok: {len(m)} open KXBTC15M market(s){' next ' + m[0].ticker if m else ''}")
print(f"   coinbase ok: BTC {Coinbase().spot():,.2f}")
PY
)

echo "==> systemd units"
install -m 644 "$REPO/deploy/kalshi-recorder.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/kalshi-paper-trader.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/kalshi-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-recorder.service kalshi-dashboard.service

if [[ "${1:-}" == "--start-paper-trade" ]]; then
  systemctl start kalshi-paper-trader.service
fi

cat <<MSG

Installed.
  Recorder (always on):   journalctl -fu kalshi-recorder
  One paper trade:        sudo systemctl start kalshi-paper-trader && journalctl -fu kalshi-paper-trader
  Trade log:              sudo cat $DATA/trades.jsonl
  Dashboard:              on your laptop: ssh -L 8080:localhost:8080 you@this-server, then open http://localhost:8080
  Edge report (any time): cd $APP && sudo -u kalshi .venv/bin/python -m kalshi --data $DATA analyze
  Settings:               $ETC/paper-trader.env
MSG
