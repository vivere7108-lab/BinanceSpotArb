#!/usr/bin/env bash
# Phase 0 recorder set-up for a fresh Amazon Linux 2023 EC2 instance in ap-northeast-1.
# Run as root from the repo checkout at /opt/binance-arb:
#   cd /opt/binance-arb && BUCKET=<your-bucket> bash ops/bootstrap.sh
# Safe to re-run: it refreshes the venv, units and configs and restarts the recorders.
set -euo pipefail
: "${BUCKET:?set BUCKET to the S3 bucket for recordings}"
APP=/opt/binance-arb
[[ "$(cd "$(dirname "$0")/.." && pwd)" == "$APP" ]] || { echo "clone the repo to $APP first"; exit 1; }

echo "== packages"
command -v dnf >/dev/null || { echo "this script targets Amazon Linux 2023"; exit 1; }
dnf install -y -q python3.11 python3.11-pip git chrony tmux   # AWS CLI v2 ships with Amazon Linux 2023
PY=python3.11
systemctl enable --now chronyd

echo "== user, venv, data dir"
id recorder >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/recorder --shell /sbin/nologin recorder
$PY -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip websockets
install -d -o recorder -g recorder /data/live
chmod +x "$APP/ops/upload_to_s3.sh"

echo "== systemd units"
install -d /etc/binance-recorder
cp "$APP"/ops/recorders/*.env /etc/binance-recorder/
cp "$APP/ops/binance-recorder@.service" "$APP/ops/binance-upload.timer" /etc/systemd/system/
sed "s|__BUCKET__|$BUCKET|" "$APP/ops/binance-upload.service" > /etc/systemd/system/binance-upload.service
systemctl daemon-reload
for f in /etc/binance-recorder/*.env; do
  systemctl enable "binance-recorder@$(basename "$f" .env)"
  systemctl restart "binance-recorder@$(basename "$f" .env)"
done
systemctl enable --now binance-upload.timer

echo "== checks"
sleep 5
systemctl --no-pager --plain list-units 'binance-*'
ls -la /data/live
chronyc tracking | grep -E 'Reference ID|System time' || true
aws s3 ls "s3://$BUCKET" >/dev/null && echo "S3 bucket reachable" || echo "WARNING: cannot reach s3://$BUCKET (instance role?)"
