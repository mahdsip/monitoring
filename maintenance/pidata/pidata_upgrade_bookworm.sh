#!/usr/bin/env bash
# pidata: Raspbian 11 (bullseye) -> 12 (bookworm), in place, 32-bit (armhf).
# Run as root inside tmux after sources.list already point to bookworm.
# Keeps local config files (--force-confold), keeps cron / rpi-eeprom /
# libraspberrypi-bin, and moves Piwigo from PHP 7.4 to PHP 8.2.
# The legacy raspberrypi-kernel package is left as is (boot files untouched).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a
SET="$(cat /tmp/pidata-upgrade-set)"
APT=(apt-get -y -q -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)

echo "[$(date +%T)] full-upgrade to bookworm"
"${APT[@]}" full-upgrade $SET

echo "[$(date +%T)] Apache: PHP 8.2 module"
a2dismod -q php7.4 >/dev/null 2>&1 || true
a2enmod -q php8.2
apache2ctl configtest
systemctl restart apache2

echo "[$(date +%T)] state"
cat /etc/debian_version
dpkg --audit && echo "dpkg audit clean"
for s in smbd nmbd apache2 mariadb plexmediaserver transmission-daemon prometheus-node-exporter cron; do
  printf '%-26s %s\n' "$s" "$(systemctl is-active "$s")"
done
echo "[$(date +%T)] UPGRADE STEP DONE (reboot pending)"
