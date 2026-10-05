#!/usr/bin/env bash
# Install the paper desk as one systemd service on Debian/Ubuntu.  Usage: sudo ./deploy/install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/kalshi DATA=/var/lib/kalshi

python3 -c 'import venv, ensurepip' 2>/dev/null || { apt-get update -y && apt-get install -y python3-venv; }
id kalshi &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin kalshi
install -d -o kalshi -g kalshi -m 750 "$DATA"
install -d -m 755 "$APP"
rm -rf "$APP/kalshi" && cp -r "$REPO/kalshi" "$APP/"
python3 -m venv "$APP/.venv" && "$APP/.venv/bin/pip" install -q --upgrade pip requests

(cd "$APP" && "$APP/.venv/bin/python" - <<'PY'
from kalshi.feeds import CompositeSpot, Kalshi
m = Kalshi().open_markets()
s = CompositeSpot(); p = s.spot()
print(f"kalshi ok: {len(m)} open market(s) | BTC {p:,.2f} from {s.venues}")
PY
)

install -m 644 "$REPO/deploy/kalshi-desk.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-desk.service
cat <<MSG
Installed and running (paper only).
  Logs:       journalctl -fu kalshi-desk
  Report:     cd $APP && sudo -u kalshi .venv/bin/python -m kalshi --data $DATA report
  Kill/resume: sudo -u kalshi touch $DATA/KILL   |   sudo rm $DATA/KILL
  Dashboard:  ssh -L 8080:localhost:8080 you@this-server, then open http://localhost:8080
MSG
