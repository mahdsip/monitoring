#!/usr/bin/env bash
# One-off (2026-11-02): move pimonitor's pinned tags to `:latest` so the Monday
# updater manages Prometheus, Grafana and Alertmanager too.
#
# The updater pulls the tag the *container* was created with, so each
# container is recreated on `:latest` pointing at the image it runs now.
# Exception: Prometheus goes 2.51.1 -> 2.55.1 first. 2.55 is the oldest
# release that can read a TSDB written by 3.x, so the updater's rollback
# target (the image running before the 3.x update) stays usable.
#   sudo bash migrate_pimonitor_floating_tags.sh
set -euo pipefail
cd /home/pi/monitoring

docker pull -q prom/prometheus:v2.55.1 >/dev/null
docker tag prom/prometheus:v2.55.1 prom/prometheus:latest
docker tag "$(docker inspect -f '{{.Image}}' grafana)" grafana/grafana:latest
docker tag "$(docker inspect -f '{{.Image}}' alertmanager)" prom/alertmanager:latest
docker-compose config -q
docker-compose up -d --no-build prometheus grafana alertmanager

for i in $(seq 1 60); do
  curl -sf -m 5 localhost:9090/-/ready >/dev/null && break
  sleep 5
done
echo "prometheus: $(curl -s localhost:9090/api/v1/status/buildinfo | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["version"])') ready after ~$((i * 5)) s"
echo "grafana: $(curl -s -m 5 localhost:9003/api/health | tr -d '\n ')"
docker exec alertmanager wget -qO- http://127.0.0.1:9093/-/ready
docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
