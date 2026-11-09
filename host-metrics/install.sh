#!/usr/bin/env bash
# Install familia-host-metrics on one host (idempotent). Run with sudo from a
# directory containing familia_host_metrics.py and this script.
#
#   sudo bash install.sh --textfile-dir DIR [--apt] [--docker] [--pihole] \
#        [--patch-unit node_exporter.service] [--recreate-container node-exporter]
#
#   --patch-unit U          node_exporter runs as systemd unit U from a manual
#                           binary: add --collector.textfile.directory=DIR to it.
#   --recreate-container C  node_exporter runs in Docker container C with the
#                           host root at /host: recreate it (same image/options)
#                           adding --collector.textfile.directory=/host/DIR.
# Nothing listens on the network; the collector only writes DIR/familia_host.prom.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/familia
TEXTFILE_DIR="" ; SECTIONS=() ; PATCH_UNIT="" ; RECREATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --textfile-dir) TEXTFILE_DIR="$2"; shift 2;;
    --apt|--docker|--pihole) SECTIONS+=("$1"); shift;;
    --patch-unit) PATCH_UNIT="$2"; shift 2;;
    --recreate-container) RECREATE="$2"; shift 2;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ -n "$TEXTFILE_DIR" ] && [ ${#SECTIONS[@]} -gt 0 ] || { echo "need --textfile-dir and at least one section" >&2; exit 2; }
TS="$(date +%Y%m%d-%H%M%S)"

echo "== 1. collector"
install -d -m 755 "$LIB" "$TEXTFILE_DIR"
install -m 755 "$SRC/familia_host_metrics.py" "$LIB/familia_host_metrics.py"
python3 -c "import sys; assert sys.version_info >= (3, 9), sys.version" 

echo "== 2. systemd timer (daily, plus 5 min after boot)"
cat > /etc/systemd/system/familia-host-metrics.service <<EOF
[Unit]
Description=familIA host metrics (apt / docker / app updates) for node_exporter textfile
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $LIB/familia_host_metrics.py --textfile-dir $TEXTFILE_DIR ${SECTIONS[*]}
Nice=10
IOSchedulingClass=idle
EOF
cat > /etc/systemd/system/familia-host-metrics.timer <<EOF
[Unit]
Description=Run familIA host metrics daily

[Timer]
OnBootSec=5min
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now familia-host-metrics.timer >/dev/null

if [ -n "$PATCH_UNIT" ]; then
  echo "== 3. node_exporter unit $PATCH_UNIT: textfile directory"
  UNIT_FILE="$(systemctl show -p FragmentPath --value "$PATCH_UNIT")"
  [ -f "$UNIT_FILE" ] || { echo "unit file for $PATCH_UNIT not found" >&2; exit 1; }
  if grep -q -- "--collector.textfile.directory" "$UNIT_FILE"; then
    echo "already configured: $(grep -o -- '--collector.textfile.directory[= ][^ ]*' "$UNIT_FILE")"
  else
    cp -a "$UNIT_FILE" "$UNIT_FILE.bak-$TS"
    sed -i -E "s|^(ExecStart=.*)$|\1 --collector.textfile.directory=$TEXTFILE_DIR|" "$UNIT_FILE"
    systemctl daemon-reload
    if ! systemctl restart "$PATCH_UNIT" || ! sleep 3 || ! systemctl is-active --quiet "$PATCH_UNIT"; then
      echo "node_exporter failed after the change -> restoring $UNIT_FILE" >&2
      cp -a "$UNIT_FILE.bak-$TS" "$UNIT_FILE"; systemctl daemon-reload; systemctl restart "$PATCH_UNIT"; exit 1
    fi
    grep -E "^ExecStart=" "$UNIT_FILE"
  fi
fi

if [ -n "$RECREATE" ]; then
  echo "== 3. node_exporter container $RECREATE: textfile directory"
  ARGS_JSON="$(docker inspect "$RECREATE" --format '{{json .Config.Cmd}}')"
  if echo "$ARGS_JSON" | grep -q -- "--collector.textfile.directory"; then
    echo "already configured: $ARGS_JSON"
  else
    docker inspect "$RECREATE" > "$TEXTFILE_DIR/../$RECREATE.inspect.bak-$TS.json" 2>/dev/null || docker inspect "$RECREATE" > "/root/$RECREATE.inspect.bak-$TS.json"
    python3 - "$RECREATE" "/host$TEXTFILE_DIR" > /tmp/familia-recreate-$TS.sh <<'PY'
import json, shlex, subprocess, sys
name, textfile = sys.argv[1], sys.argv[2]
c = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
hc, cfg = c["HostConfig"], c["Config"]
if not any(m.get("Destination") == "/host" for m in c.get("Mounts", [])):
    sys.exit("container has no /host root mount; refusing to guess paths")
cmd = ["docker", "run", "-d", "--name", name, "--restart", hc["RestartPolicy"]["Name"] or "always",
       "--network", hc.get("NetworkMode") or "default"]
if hc.get("PidMode"): cmd += ["--pid", hc["PidMode"]]
for b in hc.get("Binds") or []: cmd += ["-v", b]
for m in c.get("Mounts", []):
    if m["Type"] == "bind" and not any(b.split(":")[1] == m["Destination"] for b in hc.get("Binds") or []):
        cmd += ["-v", f'{m["Source"]}:{m["Destination"]}' + ("" if m.get("RW", True) else ":ro")]
for port, binds in (hc.get("PortBindings") or {}).items():
    for b in binds or []: cmd += ["-p", f'{b.get("HostIp") + ":" if b.get("HostIp") else ""}{b["HostPort"]}:{port}']
cmd += [c["Image"], *(cfg.get("Cmd") or []), f"--collector.textfile.directory={textfile}"]
print(" ".join(shlex.quote(x) for x in cmd))
PY
    echo "new: $(cat /tmp/familia-recreate-$TS.sh)"
    docker stop -t 20 "$RECREATE" >/dev/null && docker update --restart=no "$RECREATE" >/dev/null
    docker rename "$RECREATE" "$RECREATE-old-$TS"
    if bash /tmp/familia-recreate-$TS.sh >/dev/null && sleep 5 && curl -sf -m 5 http://127.0.0.1:9100/metrics >/dev/null; then
      echo "recreated; previous container kept as $RECREATE-old-$TS (stopped)"
    else
      echo "new container not healthy -> rolling back" >&2
      docker rm -f "$RECREATE" >/dev/null 2>&1 || true
      docker rename "$RECREATE-old-$TS" "$RECREATE"; docker update --restart=always "$RECREATE" >/dev/null; docker start "$RECREATE" >/dev/null; exit 1
    fi
    rm -f /tmp/familia-recreate-$TS.sh
  fi
fi

echo "== 4. first run"
systemctl start familia-host-metrics.service || echo "first run reported a failed section (see journalctl -u familia-host-metrics)"
# Oneshot units are "inactive" when done: systemctl status exits 3, which is fine here.
(systemctl --no-pager --lines=0 status familia-host-metrics.service || true) | sed -n '1,3p'
grep -E "^(familia_host_metrics_section_success|apt_upgrades_pending|node_reboot_required|familia_docker_image_update_available|familia_app_update_available)" "$TEXTFILE_DIR/familia_host.prom" | head -20 || true
echo "== node_exporter sees it: $(curl -s -m 5 http://127.0.0.1:9100/metrics | grep -c '^familia_host_metrics_section_success' || true) section metric(s)"
