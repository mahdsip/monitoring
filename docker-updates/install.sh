#!/usr/bin/env bash
# Install the weekly Docker image updater on this host.
#
#   sudo bash install.sh --config pimonitor.json [--time "Mon 02:30"] [--dry-run]
#
# Copies familia_docker_update.py to /usr/local/lib/familia/, the config to
# /etc/familia/docker-updates.json and installs a systemd timer
# (default Monday 02:30, before the 03:00 unattended-upgrades run).
# --dry-run: after installing, run once without pulling or recreating anything.
# Log: /var/log/familia-docker-update.log
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG=""; WHEN="Mon *-*-* 02:30:00"; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --time) WHEN="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -n "$CONFIG" ] || { echo "--config is required" >&2; exit 2; }
[ -f "$CONFIG" ] || CONFIG="$HERE/$CONFIG"
command -v python3 >/dev/null || { echo "python3 missing" >&2; exit 1; }

install -d -m 755 /usr/local/lib/familia /etc/familia
install -m 755 "$HERE/familia_docker_update.py" /usr/local/lib/familia/familia_docker_update.py
install -m 644 "$CONFIG" /etc/familia/docker-updates.json
# Fails here (not on Monday) if the config is invalid.
python3 - <<'PY'
import json, sys
sys.path.insert(0, "/usr/local/lib/familia")
import familia_docker_update as f
f.validate_config(json.load(open("/etc/familia/docker-updates.json")))
print("config ok")
PY

cat > /etc/systemd/system/familia-docker-update.service <<'EOF'
[Unit]
Description=familIA weekly Docker image update (pull, recreate, health check, rollback)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/familia/familia_docker_update.py --config /etc/familia/docker-updates.json
StandardOutput=append:/var/log/familia-docker-update.log
StandardError=append:/var/log/familia-docker-update.log
TimeoutStartSec=2h
Nice=10
# Refresh the "image update available" metrics right away (they come from the
# daily familia-host-metrics run). StopPost also runs when the update failed;
# "-" ignores hosts without that unit.
ExecStopPost=-/bin/systemctl start --no-block familia-host-metrics.service
EOF

cat > /etc/systemd/system/familia-docker-update.timer <<EOF
[Unit]
Description=familIA weekly Docker image update

[Timer]
OnCalendar=$WHEN
# A missed run (host off) is skipped, not replayed in the middle of the day.
Persistent=false

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now familia-docker-update.timer
systemctl list-timers familia-docker-update.timer --no-pager | sed -n '1,2p'

if [ "$DRY" = 1 ]; then
  echo "== dry run"
  python3 /usr/local/lib/familia/familia_docker_update.py --config /etc/familia/docker-updates.json --dry-run
fi
