#!/usr/bin/env bash
# familIA: automatic OS updates for a Raspberry Pi (Debian or Raspbian).
#
#   sudo bash setup_auto_updates.sh --reboot-time HH:MM [--apply] [--reboot-if-needed]
#
# What it does (idempotent, additive: the distro's 50unattended-upgrades is untouched):
#   * installs unattended-upgrades and enables the daily run
#   * adds the origins this distro needs: Raspberry Pi Foundation (kernel and
#     firmware) and, on Raspbian, Raspbian itself (the stock config only lists
#     "Debian", which never matches on Raspbian)
#   * third-party repos (Docker, Plex, ...) are NOT auto-upgraded
#   * runs the upgrade at ~03:00 and, only if a reboot is required, reboots at
#     --reboot-time (stagger it per host)
#   * --apply: also installs everything pending now (all repos) once
#   * --reboot-if-needed: reboot right after --apply if the system requires it
# The heavy part runs detached (systemd-run), logging to /var/log/familia-updates.log,
# so it survives a dropped SSH session.
set -euo pipefail
LOG=/var/log/familia-updates.log
ARGS=("$@")  # kept for the detached re-run (the parser below shifts them away)
REBOOT_TIME="" APPLY=0 REBOOT_NOW=0 ALLOW_REMOVALS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reboot-time) REBOOT_TIME="$2"; shift 2 ;;
    --allow-removals) ALLOW_REMOVALS="$2"; shift 2 ;;  # space-separated, reviewed by a human
    --apply) APPLY=1; shift ;;
    --reboot-if-needed) REBOOT_NOW=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[[ "$REBOOT_TIME" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || { echo "--reboot-time HH:MM is required" >&2; exit 2; }

if [ -z "${FAMILIA_DETACHED:-}" ]; then
  install -d -m 755 /usr/local/lib/familia
  install -m 755 "$0" /usr/local/lib/familia/setup_auto_updates.sh
  : > "$LOG"; chmod 644 "$LOG"
  systemctl reset-failed familia-updates.service >/dev/null 2>&1 || true
  systemd-run --unit=familia-updates --collect --quiet -E FAMILIA_DETACHED=1 \
    /bin/bash /usr/local/lib/familia/setup_auto_updates.sh "${ARGS[@]}"
  sleep 5
  if [ ! -s "$LOG" ]; then  # failed before logging anything: show why
    echo "The background run did not start correctly:" >&2
    journalctl -u familia-updates.service --since "-1min" --no-pager -o cat >&2 || true
    exit 1
  fi
  echo "Started in the background. Progress: tail -f $LOG"
  exit 0
fi

exec >>"$LOG" 2>&1
export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a
# Lock::Timeout: enabling the timers can start a pending unattended-upgrades
# run right away; wait for it (up to 1 h) instead of failing on the dpkg lock.
APT=(apt-get -y -q -o DPkg::Lock::Timeout=3600 -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)
. /etc/os-release
log() { echo "[$(date +%T)] $*"; }
trap 'log "FAILED (line $LINENO)"; echo "RESULT=failed"' ERR

log "host $(hostname) | $PRETTY_NAME | reboot time $REBOOT_TIME | apply=$APPLY"
log "apt-get update"
apt-get update -q 2>&1 | grep -E "^(E|W|Err):" || log "apt update ok"

log "unattended-upgrades"
dpkg -s unattended-upgrades >/dev/null 2>&1 || "${APT[@]}" install unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

PATTERNS='        "origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation";'
if [ "$ID" = "raspbian" ]; then
  PATTERNS='        "origin=Raspbian,codename=${distro_codename},label=Raspbian";'$'\n'"$PATTERNS"
fi
cat > /etc/apt/apt.conf.d/52familia-unattended-upgrades <<EOF
// familIA (setup_auto_updates.sh). Appended to the distro defaults.
Unattended-Upgrade::Origins-Pattern {
$PATTERNS
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
// Reboot only when /run/reboot-required exists, at a per-host time.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "$REBOOT_TIME";
EOF

log "daily run at ~03:00"
install -d /etc/systemd/system/apt-daily-upgrade.timer.d
cat > /etc/systemd/system/apt-daily-upgrade.timer.d/familia.conf <<'EOF'
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:00
RandomizedDelaySec=20m
EOF
systemctl daemon-reload
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true

log "dry run: what the automatic run would pick up"
unattended-upgrade --dry-run -v 2>&1 | grep -E "Allowed origins are|Packages that will be upgraded|No packages found" | cut -c1-400 || true

if [ "$APPLY" -eq 1 ]; then
  # Never apply blindly: a full-upgrade that removes packages (e.g. the
  # Raspberry Pi libcamera -> rpicam transition) needs a human review first.
  sim=$(apt-get -s -q -o Dpkg::Options::=--force-confold full-upgrade 2>&1)
  removals=$(printf '%s\n' "$sim" | awk '/^Remv /{print $2}' | tr '\n' ' ')
  log "pending: $(printf '%s\n' "$sim" | grep -c '^Inst') | removals: ${removals:-none}"
  unexpected=""
  for p in $removals; do
    case " $ALLOW_REMOVALS " in *" $p "*) ;; *) unexpected="$unexpected $p" ;; esac
  done
  if [ -n "$unexpected" ]; then
    log "NOT applying: the upgrade would remove packages not reviewed:$unexpected"
    echo "RESULT=needs-review"
    exit 0
  fi
  log "applying all pending updates now"
  "${APT[@]}" full-upgrade
  "${APT[@]}" autoremove --purge
  apt-get -q clean
fi

log "state"
dpkg --audit && log "dpkg audit clean"
log "pending now: $(apt-get -s -q dist-upgrade 2>/dev/null | grep -c '^Inst')"
log "next automatic run: $(systemctl list-timers apt-daily-upgrade.timer --no-legend | awk '{print $1, $2, $3}')"
if [ -f /run/reboot-required ]; then
  log "reboot required: $(cat /run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')"
  if [ "$REBOOT_NOW" -eq 1 ]; then
    log "rebooting now"; echo "RESULT=ok-rebooting"; sync; systemctl reboot; exit 0
  fi
else
  log "no reboot required"
fi
echo "RESULT=ok"
