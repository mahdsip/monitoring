#!/usr/bin/env bash
# Read-only survey of OS / updates / auto-update setup. Runs as the login user.
. /etc/os-release
echo "os: $PRETTY_NAME | arch: $(dpkg --print-architecture) | kernel: $(uname -r) | up $(uptime -p | sed 's/up //')"
model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
echo "model: ${model:-?} | ram: $(free -h | awk '/Mem:/{print $2}')"
k=$(dpkg-query -W -f='${Package} ' raspberrypi-kernel 'linux-image-rpi-*' 'linux-image-current-*' raspi-firmware 2>/dev/null)
echo "kernel pkgs: ${k:-none}"
sim=$(apt-get -s -q -o Debug::NoLocking=1 dist-upgrade 2>/dev/null)
up=$(printf '%s\n' "$sim" | grep -c '^Inst')
sec=$(printf '%s\n' "$sim" | grep '^Inst' | grep -ci securi)
held=$(apt-mark showhold 2>/dev/null | tr '\n' ' ')
age=$(( ( $(date +%s) - $(stat -c %Y /var/lib/apt/lists 2>/dev/null || echo 0) ) / 86400 ))
echo "pending: $up (security: $sec) | held: ${held:-none} | apt lists age: ${age} d"
ua=$(dpkg-query -W -f='${db:Status-Abbrev}' unattended-upgrades 2>/dev/null)
echo "unattended-upgrades: ${ua:-not installed} | apt-daily timers: $(systemctl is-active apt-daily.timer apt-daily-upgrade.timer 2>/dev/null | tr '\n' ' ')"
if [ -f /etc/apt/apt.conf.d/20auto-upgrades ]; then
  echo "20auto-upgrades: $(grep -oE '"[0-9]+"' /etc/apt/apt.conf.d/20auto-upgrades | tr -d '"' | tr '\n' ' ')"
fi
if [ -f /etc/apt/apt.conf.d/50unattended-upgrades ]; then
  echo "origins: $(grep -E '^[[:space:]]*"(origin|o)=' /etc/apt/apt.conf.d/50unattended-upgrades | sed -E 's/^[[:space:]]*//' | tr '\n' ' ' | cut -c1-220)"
  r=$(grep -E '^[[:space:]]*Unattended-Upgrade::Automatic-Reboot ' /etc/apt/apt.conf.d/50unattended-upgrades | grep -oE '"[a-z]+"')
  echo "auto-reboot: ${r:-default false}"
fi
last=$(tail -1 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null | cut -c1-70)
echo "last unattended log line: ${last:-never or unreadable}"
rr=no; [ -f /run/reboot-required ] && rr=yes
dk=no; command -v docker >/dev/null && dk=yes
sn=no; sudo -n true 2>/dev/null && sn=yes
echo "reboot required: $rr | docker: $dk | sudo nopasswd: $sn"
