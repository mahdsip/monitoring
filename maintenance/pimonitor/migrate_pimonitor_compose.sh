#!/usr/bin/env bash
# One-off (2026-11-02): move pimonitor's `docker run` containers to
# /home/pi/monitoring/docker-compose.yml without changing any image.
# Data volumes are external, so stop+rm loses nothing. ~30 s of downtime.
# Previous `docker inspect` saved in backup-20261101-083914/containers-before-compose.json.
#   sudo bash migrate_pimonitor_compose.sh
set -euo pipefail
cd /home/pi/monitoring

# Name the images that are running now with the tags used in the compose
# file, so `up` does not pull anything (versions change only on Monday).
docker tag 75aa74e50f1e prom/prometheus:v2.51.1
docker tag 608fb26fe6fc prom/blackbox-exporter:latest
docker-compose config -q

for c in prometheus grafana alertmanager blackbox node-exporter; do
  if docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null | grep -q .; then
    continue  # already managed by compose
  fi
  docker stop -t 20 "$c" >/dev/null 2>&1 || true
  docker rm "$c" >/dev/null 2>&1 || true
done
docker-compose up -d --no-build
sleep 25
docker-compose ps

echo "prometheus ready: $(curl -s -m5 localhost:9090/-/ready)"
echo "grafana: $(curl -s -m5 localhost:9003/api/health | tr -d '\n ')"
echo "node_exporter familia_* lines: $(curl -s -m5 localhost:9100/metrics | grep -c '^familia_' || true)"
curl -s 'localhost:9090/api/v1/targets?state=active' | python3 -c '
import json, sys
t = json.load(sys.stdin)["data"]["activeTargets"]
print("targets", len(t), "down:", [x["labels"].get("instance") for x in t if x["health"] != "up"])'
curl -s localhost:9090/api/v1/alertmanagers | python3 -c '
import json, sys
print("alertmanagers:", [a["url"] for a in json.load(sys.stdin)["data"]["activeAlertmanagers"]])'
curl -s localhost:9090/api/v1/rules | python3 -c '
import json, sys
print("rule groups:", [g["name"] for g in json.load(sys.stdin)["data"]["groups"]])'
curl -s 'localhost:9090/api/v1/query?query=count_over_time(up%7Bjob%3D%22prometheus%22%7D%5B3d%5D)' | python3 -c '
import json, sys
r = json.load(sys.stdin)["data"]["result"]
print("prometheus self-scrapes in the last 3 days (history kept):", r[0]["value"][1] if r else 0)'
