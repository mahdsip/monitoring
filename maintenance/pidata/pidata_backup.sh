#!/usr/bin/env bash
# Full pre-upgrade backup of pidata to the external disk (run as root).
#   sudo bash pidata_backup.sh /media/<disco>/backup-pidata-YYYYMMDD
# Restorable by: reinstalling the same OS release and rsync-ing rootfs/ back,
# or by picking individual files. Plex is stopped only while its library copies.
set -euo pipefail
B="${1:?backup dir}"
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
mkdir -p "$B/rootfs" "$B/meta"
echo "[$(date +%T)] 1/4 system files"
rsync -aHAX --numeric-ids --delete \
  --exclude=/proc/ --exclude=/sys/ --exclude=/dev/ --exclude=/run/ --exclude=/tmp/ \
  --exclude=/media/ --exclude=/mnt/ --exclude=/lost+found/ --exclude=/swapfile --exclude=/var/swap \
  --exclude=/var/lib/docker/ --exclude=/var/lib/plexmediaserver/ \
  --exclude="/var/cache/apt/archives/*.deb" --exclude=/home/*/.cache/ \
  / "$B/rootfs/"
echo "[$(date +%T)] 2/4 Plex library (Plex stopped)"
systemctl stop plexmediaserver
trap 'systemctl start plexmediaserver' EXIT
rsync -aHAX --numeric-ids --delete /var/lib/plexmediaserver/ "$B/rootfs/var/lib/plexmediaserver/"
systemctl start plexmediaserver
trap - EXIT
echo "[$(date +%T)] 3/4 MariaDB dump"
mysqldump --all-databases --single-transaction --routines --events | gzip > "$B/meta/mariadb-all.sql.gz"
echo "[$(date +%T)] 4/4 package lists and crontabs"
dpkg --get-selections > "$B/meta/dpkg-selections.txt"
apt-mark showmanual > "$B/meta/apt-manual.txt"
dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' > "$B/meta/packages-versions.txt"
crontab -l -u pi > "$B/meta/crontab-pi.txt" 2>/dev/null || true
crontab -l > "$B/meta/crontab-root.txt" 2>/dev/null || true
cp -a /etc/fstab /boot/config.txt /boot/cmdline.txt "$B/meta/"
chmod -R go-rwx "$B"
echo "[$(date +%T)] DONE: $(du -sh "$B" | cut -f1) in $B"
