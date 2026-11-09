#!/usr/bin/env python3
"""Run the generated dashboard queries against Prometheus and print a summary.

Usage (on pimonitor):  python3 check_queries.py casa.json [http://localhost:9090]
Reports, per panel query: number of series (or table rows) and a sample.
Exit code 1 if any query errors.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

dash = json.load(open(sys.argv[1], encoding="utf-8"))
base = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:9090"
errors = 0
for panel in dash["panels"]:
    for t in panel.get("targets", []):
        url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": t["expr"]})
        try:
            res = json.load(urllib.request.urlopen(url, timeout=20))["data"]["result"]
        except urllib.error.HTTPError as exc:
            errors += 1
            print(f"ERR {panel['title'][:28]:<28} {t['refId']} {exc.read()[:200]!r}")
            continue
        sample = []
        for s in res[:4]:
            m = s["metric"]
            key = m.get("fname") or m.get("device") or ""
            val = m.get("release") or m.get("pretty_name") or s["value"][1]
            sample.append(f"{key}={val}")
        flag = "  " if res else "∅ "
        print(f"{flag}{panel['title'][:28]:<28} {t['refId']} n={len(res):<3} {' | '.join(sample)[:150]}")
print("errors:", errors)
sys.exit(1 if errors else 0)
