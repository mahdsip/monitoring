#!/usr/bin/env bash
# pidata: Raspbian 12 (bookworm) -> 13 (trixie), in place, 32-bit (armhf).
# Run as root inside tmux after sources already point to trixie.
# Keeps local config files, keeps rpi-eeprom / raspi-utils / cron, moves
# Piwigo to PHP 8.4, and does NOT install samba-ad-dc or the dhcpcd service
# (the network is managed by NetworkManager).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a
SET="$(cat /home/pi/.pidata-trixie-set)"
APT=(apt-get -y -q -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)

echo "[$(date +%T)] full-upgrade to trixie"
"${APT[@]}" full-upgrade $SET

echo "[$(date +%T)] Apache: PHP 8.4 module"
a2dismod -q php8.2 >/dev/null 2>&1 || true
a2enmod -q php8.4
apache2ctl configtest
systemctl restart apache2

echo "[$(date +%T)] state"
cat /etc/debian_version
dpkg --audit && echo "dpkg audit clean"
for s in NetworkManager ssh smbd nmbd apache2 mariadb plexmediaserver transmission-daemon prometheus-node-exporter cron smartmontools; do
  printf '%-26s %s\n' "$s" "$(systemctl is-active "$s")"
done
echo "-- network"
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device | grep -E ":(ethernet|wifi):"
ip -4 -o addr show scope global | awk '{print $2, $4}'
ls /etc/netplan/ 2>/dev/null || echo "(no /etc/netplan)"
echo "[$(date +%T)] UPGRADE STEP DONE (reboot pending)"
