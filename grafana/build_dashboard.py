#!/usr/bin/env python3
"""Generate the "Casa · red y dispositivos" Grafana dashboard + probe targets.

Input : grafana/casa.devices.yml   (gitignored; see the .example)
Output: grafana/dashboards/casa.json        (Grafana file provisioning)
        prometheus/servicios/servicios.yml  (Prometheus file_sd targets)

The network map is a Canvas panel laid out from the device tree (router ->
devices, reverse proxy -> what it publishes). Every value on it comes from one
row of instant queries joined by time, one field per datum, e.g.
"temp photos", "estado photos", "svc photos PhotoPrism".

Run:  uv run --no-project --with pyyaml==6.0.3 python grafana/build_dashboard.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
DS = {"type": "prometheus", "uid": "prom-casa"}
DASH_UID = "casa-red"
MODULES = {"http_up", "http_auth", "https_lan", "https_public", "tcp_open", "dns_resolve"}

# Canvas geometry (px)
CANVAS_W, CARD_W, COL_GAP = 1360, 200, 16
C_BG, C_CARD, C_TEXT, C_MUTED, C_LINE = "#181b1f", "#22252b", "#e5e5e5", "#8e8e9e", "#5794f2"
GREEN, YELLOW, ORANGE, RED = "#73bf69", "#fade2a", "#ff9830", "#f2495c"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class ConfigError(ValueError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    devices = cfg.get("devices") or []
    ids = [d["id"] for d in devices]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicated device id")
    for d in devices:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", d["id"]):
            raise ConfigError(f"invalid device id {d['id']!r}")
        if d.get("parent", "router") not in ("router", *ids):
            raise ConfigError(f"{d['id']}: unknown parent {d.get('parent')!r}")
        for disk in d.get("disks") or []:
            # The mountpoint ends up inside a PromQL regex: keep it to a safe charset.
            if not disk.get("name") or not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(disk.get("mountpoint", ""))):
                raise ConfigError(f"{d['id']}: disk needs a name and an absolute mountpoint")
        for s in d.get("services") or []:
            if s.get("module") not in MODULES:
                raise ConfigError(f"{d['id']}/{s.get('name')}: unknown module {s.get('module')!r}")
            if not s.get("name") or not s.get("target"):
                raise ConfigError(f"{d['id']}: service needs name and target")
    return cfg


def dev_expr(expr: str, by: str = "device", agg: str = "max") -> str:
    """Aggregate a node metric per device (instance without port)."""
    return f'{agg} by ({by}) (label_replace({expr}, "device", "$1", "instance", "([^:]+).*"))'


def q(ref: str, expr: str, legend: str = "", fmt: str = "time_series") -> dict[str, Any]:
    return {"refId": ref, "datasource": DS, "expr": expr, "instant": True, "range": False,
            "legendFormat": legend, "format": fmt}


# PromQL building blocks shared by the map and the tables
UP = dev_expr('up{job="node-exporter"}')
TEMP = dev_expr("node_thermal_zone_temp")
CPU = '100 * (1 - ' + dev_expr('rate(node_cpu_seconds_total{mode="idle"}[5m])', agg="avg") + ")"
MEM = "100 * (1 - " + dev_expr("node_memory_MemAvailable_bytes") + " / " + dev_expr("node_memory_MemTotal_bytes") + ")"
DISK = dev_expr('100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})')
UPTIME = "(time() - " + dev_expr("node_boot_time_seconds") + ") / 86400"
# Debian's apt collector omits apt_upgrades_pending when nothing is pending, so
# hosts that publish any apt metric but no upgrades count as 0 ("al día").
OS_UPD = (dev_expr("apt_upgrades_pending", agg="sum")
          + " or 0 * " + dev_expr("apt_autoremove_pending")
          + ' or label_replace(sum(homeassistant_update_state), "device", "homeassistant", "", "")')
SEC_UPD = (dev_expr('apt_upgrades_pending{origin=~"(?i).*secur.*"}', agg="sum")
           + " or 0 * " + dev_expr("apt_autoremove_pending"))
REBOOT = dev_expr("node_reboot_required")
DOCKER_UPD = dev_expr("familia_docker_image_update_available", agg="sum")
APP_UPD = dev_expr("familia_app_update_available", agg="sum")
OS_VER = dev_expr('node_os_version{id=~"debian|raspbian"}')
SVC = 'max by (device, service) (probe_success{job="servicios"})'
# Nominal RAM in GB: MemTotal (GiB) rounded up to a power of two
# (3.7 GiB -> 4, 0.9 GiB -> 1, 7.8 GiB -> 8).
# (PromQL's ln() only takes vectors, so ln(2) is written as a constant.)
RAM_GB = "2 ^ ceil(ln(" + dev_expr('node_memory_MemTotal_bytes{job="node-exporter"}') + " / 2^30) / 0.6931471805599453)"


def _text_info(metric: str, prefix: str, label: str) -> str:
    """One row per device: fname="<prefix> <device>" plus the text label."""
    return (f'max by (fname, {label}) (label_replace({metric}{{job="node-exporter"}}, '
            f'"fname", "{prefix} $1", "instance", "([^:]+).*"))')


KERNEL_INFO = _text_info("node_uname_info", "kernel", "release")
OS_INFO = _text_info("node_os_info", "os", "pretty_name")


def extra_disks_expr(cfg: dict[str, Any]) -> str | None:
    """Usage % of the configured extra disks, per device and mountpoint."""
    mps = sorted({disk["mountpoint"] for d in cfg["devices"] for disk in d.get("disks") or []})
    if not mps:
        return None
    rx = "|".join(m.replace(".", "\\\\.") for m in mps)  # PromQL string escape of "\."
    sel = f'{{mountpoint=~"{rx}"}}'
    return dev_expr(f"100 * (1 - node_filesystem_avail_bytes{sel} / node_filesystem_size_bytes{sel})",
                    by="device, mountpoint")


# --------------------------------------------------------------------------
# Field config helpers
# --------------------------------------------------------------------------
def thresholds(*steps: tuple[float | None, str]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def override(regex: str, **props: Any) -> dict[str, Any]:
    # Grafana anchors a byRegexp pattern as ^...$ unless it is written /.../
    # (stringToJsRegex), so always pass the slash form to keep "^fondo " a prefix.
    return {"matcher": {"id": "byRegexp", "options": f"/{regex}/"},
            "properties": [{"id": k.replace("__", "."), "value": v} for k, v in props.items()]}


def value_map(mapping: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(mapping.items())}}]


def range_map(frm: float, to: float, text: str, color: str) -> dict[str, Any]:
    return {"type": "range", "options": {"from": frm, "to": to, "result": {"text": text, "color": color}}}


TEMP_STEPS = thresholds((None, GREEN), (70, YELLOW), (80, ORANGE), (85, RED))
PCT_STEPS = thresholds((None, GREEN), (80, YELLOW), (90, RED))
UPD_STEPS = thresholds((None, GREEN), (1, YELLOW))


def details_link(cfg: dict[str, Any], node_expr: str) -> list[dict[str, Any]]:
    dd = cfg.get("details_dashboard")
    if not dd:
        return []
    return [{"title": "Detalle del equipo",
             "url": f"/d/{dd['uid']}/{dd.get('slug', '')}?var-job={dd.get('job', 'node-exporter')}&var-node={node_expr}"}]


# --------------------------------------------------------------------------
# Canvas network map
# --------------------------------------------------------------------------
def card_height(d: dict[str, Any]) -> int:
    extra = 1 if (d.get("docker") or d.get("apps") or d.get("os_check")) else 0
    # +30: OS name and kernel lines under the update status.
    return 78 + 30 + 16 * extra + 16 * len(d.get("disks") or []) + 16 * len(d.get("services") or [])


def layout(cfg: dict[str, Any]) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int, int, int]], int]:
    """Positions: router top centre; side columns; proxy children in a row below."""
    devices = cfg["devices"]
    children: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        children.setdefault(d.get("parent", "router"), []).append(d)
    boxes: dict[str, tuple[int, int, int, int]] = {}
    router = (CANVAS_W // 2 - 90, 12, 180, 52)
    boxes["router"] = router
    col_y = {"left": 96, "right": 96}
    centre_y = 112
    for d in children.get("router", []):
        side = d.get("side") or ("center" if children.get(d["id"]) else "left")
        h = card_height(d)
        if side == "center":
            boxes[d["id"]] = (CANVAS_W // 2 - CARD_W // 2, centre_y, CARD_W, h)
            centre_y += h + 60
        else:
            x = 16 if side == "left" else CANVAS_W - CARD_W - 16
            boxes[d["id"]] = (x, col_y[side], CARD_W, h)
            col_y[side] += h + COL_GAP
    # Children of non-router parents: a centred row under the parent.
    for parent_id, kids in children.items():
        if parent_id == "router" or parent_id not in boxes:
            continue
        px, py, pw, ph = boxes[parent_id]
        total = len(kids) * CARD_W + (len(kids) - 1) * 24
        x0 = px + pw // 2 - total // 2
        for i, d in enumerate(kids):
            boxes[d["id"]] = (x0 + i * (CARD_W + 24), py + ph + 70, CARD_W, card_height(d))
    height = max(y + h for _, y, _, h in boxes.values()) + 16
    return {}, boxes, height


def _el(etype: str, name: str, box: tuple[int, int, int, int], **kw: Any) -> dict[str, Any]:
    x, y, w, h = box
    el = {"type": etype, "name": name, "constraint": {"horizontal": "left", "vertical": "top"},
          "placement": {"left": x, "top": y, "width": w, "height": h},
          "background": {"color": {"fixed": "transparent"}}, "border": {"color": {"fixed": "transparent"}}}
    el.update(kw)
    return el


def _text(name, box, text=None, field=None, size=11, color=C_TEXT, color_field=None, align="left"):
    cfg = {"align": align, "valign": "middle", "size": size,
           "color": {"fixed": color, **({"field": color_field} if color_field else {})},
           "text": {"mode": "field", "field": field, "fixed": ""} if field else {"mode": "fixed", "fixed": text}}
    return _el("metric-value" if field else "text", name, box, config=cfg)


BUS_OFFSET = 28  # px between a side column and its vertical trunk line


def _point(box: tuple[int, int, int, int], anchor: dict) -> tuple[float, float]:
    """Scene point of an anchor (x right, y UP, in half-extents of the element)."""
    x, y, w, h = box
    return x + w / 2 + anchor["x"] * w / 2, y + h / 2 - anchor["y"] * h / 2


def _route(src: tuple[int, int, int, int], dst: tuple[int, int, int, int], side: str) -> tuple[dict, dict, list[dict]]:
    """Orthogonal route: side columns hang from a vertical trunk outside the
    cards (so no line crosses a card); devices below get a tree elbow.

    Canvas vertices are fractions of (target - source) on each axis.
    """
    sx, sy, sw, sh = src
    dx, dy, dw, dh = dst
    # The layout decides the route (not the geometry): side columns hang from
    # their trunk; everything else is a tree below its parent.
    if side == "left":     # source left edge -> trunk -> card right edge
        a, b, bus = {"x": -1, "y": 0}, {"x": 1, "y": 0}, dx + dw + BUS_OFFSET
    elif side == "right":
        a, b, bus = {"x": 1, "y": 0}, {"x": -1, "y": 0}, dx - BUS_OFFSET
    else:                  # below: bottom -> top, elbow at mid height when not aligned
        a, b = {"x": 0, "y": -1}, {"x": 0, "y": 1}
        (xs, _), (xe, _) = _point(src, a), _point(dst, b)
        return a, b, ([] if abs(xe - xs) < 1 else [{"x": 0, "y": 0.5}, {"x": 1, "y": 0.5}])
    (xs, _), (xe, _) = _point(src, a), _point(dst, b)
    fx = (bus - xs) / (xe - xs)
    return a, b, [{"x": round(fx, 4), "y": 0}, {"x": round(fx, 4), "y": 1}]


def canvas_elements(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    _, boxes, height = layout(cfg)
    devices = {d["id"]: d for d in cfg["devices"]}
    els: list[dict[str, Any]] = []
    cards: dict[str, dict[str, Any]] = {}

    rx, ry, rw, rh = boxes["router"]
    router = _el("rectangle", "card-router", boxes["router"],
                 background={"color": {"fixed": C_CARD}},
                 border={"color": {"fixed": C_LINE}, "width": 2, "radius": 26},
                 config={"align": "center", "valign": "middle", "color": {"fixed": C_TEXT},
                         "text": {"mode": "fixed", "fixed": ""}}, connections=[])
    els.append(router)
    els.append(_text("router-name", (rx, ry + 6, rw, 20), cfg["router"].get("name", "Router"), size=14, align="center"))
    els.append(_text("router-ip", (rx, ry + 26, rw, 18), cfg["router"].get("ip", ""), size=11, color=C_MUTED, align="center"))
    cards["router"] = router

    for did, d in devices.items():
        x, y, w, h = boxes[did]
        card = _el("rectangle", f"card-{did}", boxes[did],
                   background={"color": {"fixed": C_CARD, "field": f"fondo {did}"}},
                   border={"color": {"fixed": GREEN, "field": f"temp {did}"}, "width": 2, "radius": 8},
                   config={"align": "center", "valign": "middle", "color": {"fixed": C_TEXT},
                           "text": {"mode": "fixed", "fixed": ""}}, connections=[])
        cards[did] = card
        els.append(card)
        els.append(_text(f"{did}-name", (x + 10, y + 5, 110, 20), did, size=14))
        # Hardware next to the name: model from the config, RAM from Prometheus.
        if d.get("hw"):
            els.append(_text(f"{did}-hw", (x + 96, y + 7, 58, 16), f"{d['hw']} ·", size=10, color="#c7d0d9", align="right"))
            els.append(_text(f"{did}-ram", (x + 157, y + 7, 37, 16), field=f"ram {did}", size=10, color="#c7d0d9"))
        ip = "." + d.get("ip", "").rsplit(".", 1)[-1] + (" ⌔ wifi" if d.get("link") == "wifi" else "")
        role = " · ".join(p for p in (d.get("role", ""), ip) if p)
        els.append(_text(f"{did}-role", (x + 10, y + 24, w - 20, 16), role, size=10, color=C_MUTED))
        row = y + 42
        els.append(_text(f"{did}-temp", (x + 10, row, 52, 16), field=f"temp {did}", color_field=f"temp {did}"))
        els.append(_text(f"{did}-cpu", (x + 62, row, 66, 16), field=f"cpu {did}"))
        if d.get("root_disk", True):  # e.g. Home Assistant OS has no "/" filesystem
            els.append(_text(f"{did}-disk", (x + 128, row, 66, 16), field=f"disco {did}", color_field=f"disco {did}"))
        row += 18
        els.append(_text(f"{did}-os", (x + 10, row, w - 20, 16), field=f"act {did}", color_field=f"act {did}"))
        row += 16
        # OS name (from node_os_info, or fixed in the config) and, under it, the kernel.
        if d.get("os_name"):
            els.append(_text(f"{did}-osname", (x + 10, row, w - 20, 15), d["os_name"], size=10, color="#c7d0d9"))
        else:
            els.append(_text(f"{did}-osname", (x + 10, row, w - 20, 15), field=f"os {did}", size=10, color="#c7d0d9"))
        row += 15
        els.append(_text(f"{did}-kernel", (x + 10, row, w - 20, 15), field=f"kernel {did}", size=10, color=C_MUTED))
        row += 15
        extras = []
        if d.get("docker"):
            extras.append(("docker", f"docker {did}"))
        if d.get("apps"):
            extras.append(("apps", f"apps {did}"))
        if d.get("os_check"):
            extras.append(("osver", f"so {did}"))
        if extras:
            slot = (w - 20) // len(extras)
            for i, (kind, field) in enumerate(extras):
                els.append(_text(f"{did}-{kind}", (x + 10 + i * slot, row, slot, 16), field=field, size=10, color_field=field))
            row += 16
        for i, disk in enumerate(d.get("disks") or []):
            field = f"hdd {did} {disk['mountpoint']}"
            els.append(_text(f"{did}-hdd{i}-name", (x + 10, row, 124, 16), f"💾 {disk['name']}", size=10, color="#c7d0d9"))
            els.append(_text(f"{did}-hdd{i}", (x + 134, row, 56, 16), field=field, size=11, color_field=field, align="right"))
            row += 16
        for i, s in enumerate(d.get("services") or []):
            sy = row + 2 + i * 16
            els.append(_el("ellipse", f"{did}-svc{i}-dot", (x + 12, sy + 3, 9, 9),
                           background={"color": {"fixed": C_MUTED, "field": f"svc {did} {s['name']}"}},
                           config={"align": "center", "valign": "middle", "color": {"fixed": C_TEXT},
                                   "text": {"mode": "fixed", "fixed": ""}}))
            els.append(_text(f"{did}-svc{i}", (x + 26, sy, w - 34, 15), s["name"], size=10, color="#c7d0d9"))

    for did, d in devices.items():
        parent = d.get("parent", "router")
        side = d.get("side", "left") if parent == "router" and d.get("side") != "center" else "below"
        src, dst, vertices = _route(boxes[parent], boxes[did], side)
        conn = {"source": src, "target": dst, "targetName": f"card-{did}", "path": "straight",
                "color": {"fixed": C_LINE}, "size": {"fixed": 2, "min": 1, "max": 10},
                # A network has no direction: no arrowheads.
                "direction": {"mode": "fixed", "fixed": "none"},
                "radius": {"fixed": 10, "min": 0, "max": 200}}
        if vertices:
            conn["vertices"] = vertices
        if d.get("link") == "wifi":
            conn["lineStyle"] = "dashed"
        cards[parent]["connections"].append(conn)
    return els, height


def canvas_queries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    extra = extra_disks_expr(cfg)
    return [*([q("J", extra, "hdd {{device}} {{mountpoint}}")] if extra else []),
        q("A", UP, "fondo {{device}}"),
        q("B", TEMP, "temp {{device}}"),
        q("C", CPU, "cpu {{device}}"),
        q("D", DISK, "disco {{device}}"),
        q("E", OS_UPD, "act {{device}}"),
        q("F", DOCKER_UPD, "docker {{device}}"),
        q("G", APP_UPD, "apps {{device}}"),
        q("H", OS_VER, "so {{device}}"),
        q("I", SVC, "svc {{device}} {{service}}"),
        q("M", RAM_GB, "ram {{device}}"),
        # Text values (kernel release, OS name) arrive as labels: table queries
        # turned into one string field per device by rowsToFields below.
        q("K", KERNEL_INFO, fmt="table"),
        q("L", OS_INFO, fmt="table"),
    ]


# refIds of the numeric map queries (joined by time); K/L stay separate frames.
NUMERIC_REFS = "/^[A-JM]$/"


def canvas_transformations() -> list[dict[str, Any]]:
    def rows_to_fields(ref: str, value_col: str) -> dict[str, Any]:
        return {"id": "rowsToFields", "filter": {"id": "byRefId", "options": ref},
                "options": {"mappings": [{"fieldName": "fname", "handlerKey": "field.name"},
                                         {"fieldName": value_col, "handlerKey": "field.value"},
                                         {"fieldName": "Value", "handlerKey": "__ignore"},
                                         {"fieldName": "Time", "handlerKey": "__ignore"}]}}
    return [
        rows_to_fields("K", "release"),
        rows_to_fields("L", "pretty_name"),
        # Only the numeric queries are joined; filtered-out frames are kept as they are.
        {"id": "joinByField", "filter": {"id": "byRefId", "options": NUMERIC_REFS},
         "options": {"byField": "Time", "mode": "outer"}},
    ]


def canvas_field_config(cfg: dict[str, Any]) -> dict[str, Any]:
    apps_label = {d["id"]: d.get("apps") for d in cfg["devices"] if d.get("apps")}
    overrides = [
        # Canvas takes element colours from the field's colour mode (not from
        # value-mapping colours), so every field used as a colour is driven by
        # thresholds. Card background: dark red when down, dark green when up.
        override("^fondo ", thresholds=thresholds((None, "#4a1f24"), (1, "#1f3b2a")),
                 mappings=value_map({"1": ("en línea", "#1f3b2a"), "0": ("caído", "#4a1f24")}),
                 links=details_link(cfg, "${__field.labels.device}:9100"), **{"color": {"mode": "thresholds"}}),
        override("^temp ", unit="celsius", decimals=0, thresholds=TEMP_STEPS, **{"color": {"mode": "thresholds"}}),
        override("^cpu ", unit="suffix:% CPU", decimals=0),
        override("^disco ", unit="suffix:% disco", decimals=0, thresholds=PCT_STEPS, **{"color": {"mode": "thresholds"}}),
        override("^hdd ", unit="percent", decimals=0, thresholds=PCT_STEPS, **{"color": {"mode": "thresholds"}}),
        override("^ram ", unit="suffix: GB", decimals=0),
        override("^kernel ", **{"color": {"mode": "fixed", "fixedColor": C_MUTED}}),
        override("^os ", **{"color": {"mode": "fixed", "fixedColor": "#c7d0d9"}}),
        override("^act ", unit="suffix: act. SO pendientes", decimals=0, thresholds=UPD_STEPS,
                 mappings=value_map({"0": ("✓ sistema al día", GREEN)}), **{"color": {"mode": "thresholds"}}),
        override("^docker ", unit="suffix: 🐳 nuevas", decimals=0, thresholds=UPD_STEPS,
                 mappings=value_map({"0": ("🐳 al día", GREEN)}), **{"color": {"mode": "thresholds"}}),
        override("^so ", decimals=0, thresholds=thresholds((None, RED), (12, C_MUTED)),
                 # The OS name is shown in full below; this only flags unsupported releases.
                 mappings=[range_map(0, 11.99, "⚠ SO sin soporte", RED),
                           range_map(12, 99, " ", C_MUTED)], **{"color": {"mode": "thresholds"}}),
        override("^svc ", thresholds=thresholds((None, RED), (1, GREEN)),
                 mappings=value_map({"1": ("ok", GREEN), "0": ("caído", RED)}), **{"color": {"mode": "thresholds"}}),
    ]
    for did, label in apps_label.items():
        overrides.append(override(f"^apps {re.escape(did)}$", unit=f"suffix: act. {label}", decimals=0, thresholds=UPD_STEPS,
                                  mappings=value_map({"0": (f"{label} al día", GREEN)}), **{"color": {"mode": "thresholds"}}))
    return {"defaults": {"color": {"mode": "fixed", "fixedColor": C_TEXT}, "thresholds": thresholds((None, C_TEXT)),
                         "noValue": "—"},
            "overrides": overrides}


# --------------------------------------------------------------------------
# Classic panels
# --------------------------------------------------------------------------
def stat(pid, title, expr, x, w, steps, unit="none", desc="", no_value="0"):
    return {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": 0, "w": w, "h": 4},
            "targets": [q("A", expr)],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "noValue": no_value, "thresholds": steps,
                                         "color": {"mode": "thresholds"}}, "overrides": []}}


def table(pid, title, grid, targets, renames, overrides, sort_by):
    exclude = {"Time": True, **{k: True for k, v in renames.items() if v is None}}
    return {"id": pid, "type": "table", "title": title, "datasource": DS, "gridPos": grid, "targets": targets,
            "transformations": [
                {"id": "merge", "options": {}},
                {"id": "organize", "options": {"excludeByName": exclude,
                                               "renameByName": {k: v for k, v in renames.items() if v},
                                               "indexByName": {v: i for i, v in enumerate(n for n in renames.values() if n)}}},
                {"id": "sortBy", "options": {"sort": [{"field": sort_by, "desc": True}]}},
            ],
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "noValue": "—"},
                            "overrides": overrides}}


def col(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k.replace("__", "."), "value": v} for k, v in props.items()]}


def color_cell(kind="color-text"):
    return {"type": kind}


def build_dashboard(cfg: dict[str, Any]) -> dict[str, Any]:
    elements, height = canvas_elements(cfg)
    map_rows = max(10, (height + 40) // 30 + 1)
    panels: list[dict[str, Any]] = [
        stat(1, "Equipos caídos", 'count(up{job="node-exporter"} == 0) or vector(0)', 0, 4,
             thresholds((None, GREEN), (1, RED))),
        stat(2, "Alertas activas", 'count(ALERTS{alertstate="firing"}) or vector(0)', 4, 4,
             thresholds((None, GREEN), (1, RED))),
        stat(3, "Servicios caídos", 'count(probe_success{job="servicios"} == 0) or vector(0)', 8, 4,
             thresholds((None, GREEN), (1, RED))),
        stat(4, "Equipos con actualizaciones de SO",
             f"count(({OS_UPD}) > 0) or vector(0)", 12, 4, thresholds((None, GREEN), (1, YELLOW))),
        stat(5, "Imágenes Docker nuevas", "sum(familia_docker_image_update_available) or vector(0)", 16, 4,
             thresholds((None, GREEN), (1, YELLOW))),
        stat(6, "Certificado público más próximo a caducar",
             'min((probe_ssl_earliest_cert_expiry{module="https_public"} - time()) / 86400)', 20, 4,
             thresholds((None, RED), (14, YELLOW), (30, GREEN)), unit="suffix: días", no_value="—"),
        {"id": 10, "type": "canvas", "title": "Mapa de red", "datasource": DS,
         "description": "Fondo: en línea / caído · borde y temperatura: verde < 70 °C < amarillo < 80 °C < naranja < 85 °C < rojo · "
                        "puntos: servicios (verde ok, rojo caído) · línea discontinua: Wi-Fi. Clic en una tarjeta: detalle del equipo.",
         "gridPos": {"x": 0, "y": 4, "w": 24, "h": map_rows},
         "targets": canvas_queries(cfg),
         "transformations": canvas_transformations(),
         "options": {"inlineEditing": False, "showAdvancedTypes": True, "panZoom": True, "infinitePan": False,
                     "root": {"type": "frame", "name": "root", "background": {"color": {"fixed": "transparent"}},
                              "border": {"color": {"fixed": "transparent"}},
                              "constraint": {"horizontal": "left", "vertical": "top"},
                              "placement": {"left": 0, "top": 0, "width": 100, "height": 100},
                              "elements": elements}},
         "fieldConfig": canvas_field_config(cfg)},
    ]
    y = 4 + map_rows
    panels.append({
        "id": 20, "type": "timeseries", "title": "Temperatura CPU", "datasource": DS,
        "gridPos": {"x": 0, "y": y, "w": 14, "h": 9},
        "targets": [dict(q("A", TEMP, "{{device}}"), instant=False, range=True)],
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {"unit": "celsius", "decimals": 1, "min": 30,
                                     "thresholds": thresholds((None, "transparent"), (80, ORANGE), (85, RED)),
                                     "custom": {"thresholdsStyle": {"mode": "dashed"}, "lineWidth": 1, "fillOpacity": 0}},
                        "overrides": []}})
    panels.append({
        "id": 21, "type": "bargauge", "title": "Uso de discos", "datasource": DS,
        "gridPos": {"x": 14, "y": y, "w": 10, "h": 9},
        "targets": [q("A", f"sort_desc({DISK})", "{{device}} /"),
                    *([q("B", f"sort_desc({extra_disks_expr(cfg)})", "{{device}} {{mountpoint}}")]
                      if extra_disks_expr(cfg) else [])],
        "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "decimals": 0,
                                     "thresholds": thresholds((None, GREEN), (70, YELLOW), (85, RED)),
                                     "color": {"mode": "thresholds"}}, "overrides": []}})
    y += 9
    status_targets = [
        q("A", UP, fmt="table"), q("B", TEMP, fmt="table"), q("C", CPU, fmt="table"), q("D", MEM, fmt="table"),
        q("E", DISK, fmt="table"), q("F", UPTIME, fmt="table"),
        q("G", dev_expr("node_os_info", by="device, pretty_name"), fmt="table"),
        q("H", OS_UPD, fmt="table"), q("I", SEC_UPD, fmt="table"), q("J", REBOOT, fmt="table"),
        q("K", DOCKER_UPD, fmt="table"), q("L", APP_UPD, fmt="table"),
    ]
    renames = {"device": "Equipo", "Value #A": "Estado", "Value #B": "Temp", "Value #C": "CPU", "Value #D": "Memoria",
               "Value #E": "Disco", "Value #F": "Encendido", "pretty_name": "Sistema", "Value #G": None,
               "Value #H": "Act. SO", "Value #I": "Seguridad", "Value #J": "Reinicio", "Value #K": "Docker nuevas",
               "Value #L": "Apps nuevas"}
    upd = dict(thresholds=UPD_STEPS, custom__cellOptions=color_cell(), color={"mode": "thresholds"}, decimals=0)
    status_overrides = [
        col("Equipo", links=details_link(cfg, "${__value.raw}:9100")),
        col("Estado", mappings=value_map({"1": ("● en línea", GREEN), "0": ("● caído", RED)}), custom__cellOptions=color_cell()),
        col("Temp", unit="celsius", decimals=0, thresholds=TEMP_STEPS, color={"mode": "thresholds"}, custom__cellOptions=color_cell()),
        col("CPU", unit="percent", decimals=0, thresholds=PCT_STEPS, color={"mode": "thresholds"}, custom__cellOptions=color_cell()),
        col("Memoria", unit="percent", decimals=0, thresholds=PCT_STEPS, color={"mode": "thresholds"}, custom__cellOptions=color_cell()),
        col("Disco", unit="percent", decimals=0, thresholds=PCT_STEPS, color={"mode": "thresholds"},
            custom__cellOptions={"type": "gauge", "mode": "basic"}),
        col("Encendido", unit="d", decimals=1),
        col("Sistema", custom__width=230),
        col("Act. SO", **upd), col("Docker nuevas", **upd), col("Apps nuevas", **upd),
        col("Seguridad", decimals=0, thresholds=thresholds((None, GREEN), (1, RED)), color={"mode": "thresholds"},
            custom__cellOptions=color_cell()),
        col("Reinicio", mappings=value_map({"1": ("necesario", ORANGE), "0": ("no", GREEN)}), custom__cellOptions=color_cell()),
    ]
    panels.append(table(30, "Estado y mantenimiento", {"x": 0, "y": y, "w": 24, "h": 11},
                        status_targets, renames, status_overrides, "Act. SO"))
    y += 11
    svc_targets = [
        q("A", 'max by (device, service, module) (probe_success{job="servicios"})', fmt="table"),
        q("B", 'max by (device, service) (probe_duration_seconds{job="servicios"})', fmt="table"),
        q("C", 'max by (device, service) ((probe_ssl_earliest_cert_expiry{job="servicios", module="https_public"} - time()) / 86400)', fmt="table"),
    ]
    panels.append(table(40, "Servicios", {"x": 0, "y": y, "w": 12, "h": 10}, svc_targets,
                        {"device": "Equipo", "service": "Servicio", "module": "Tipo", "Value #A": "Estado",
                         "Value #B": "Respuesta", "Value #C": "Certificado (días)"},
                        [col("Estado", mappings=value_map({"1": ("● ok", GREEN), "0": ("● caído", RED)}), custom__cellOptions=color_cell()),
                         col("Respuesta", unit="s", decimals=2, thresholds=thresholds((None, GREEN), (1, YELLOW), (3, RED)),
                             color={"mode": "thresholds"}, custom__cellOptions=color_cell()),
                         col("Certificado (días)", decimals=0, thresholds=thresholds((None, RED), (14, YELLOW), (30, GREEN)),
                             color={"mode": "thresholds"}, custom__cellOptions=color_cell())],
                        "Respuesta"))
    docker_targets = [
        q("A", dev_expr("familia_docker_image_update_available", by="device, container, image, tag"), fmt="table"),
        q("B", "(time() - " + dev_expr("familia_docker_image_created_timestamp_seconds", by="device, container, image, tag") + ") / 86400", fmt="table"),
    ]
    panels.append(table(41, "Contenedores Docker", {"x": 12, "y": y, "w": 12, "h": 10}, docker_targets,
                        {"device": "Equipo", "container": "Contenedor", "image": "Imagen", "tag": "Etiqueta",
                         "Value #A": "¿Versión nueva?", "Value #B": "Antigüedad imagen"},
                        [col("¿Versión nueva?", mappings=value_map({"1": ("sí", YELLOW), "0": ("no", GREEN)}), custom__cellOptions=color_cell()),
                         col("Antigüedad imagen", unit="d", decimals=0, thresholds=thresholds((None, GREEN), (180, YELLOW), (365, ORANGE)),
                             color={"mode": "thresholds"}, custom__cellOptions=color_cell())],
                        "¿Versión nueva?"))
    return {
        "uid": DASH_UID, "title": "Casa · red y dispositivos", "tags": ["casa", "familia"],
        "timezone": "browser", "refresh": "30s", "time": {"from": "now-24h", "to": "now"},
        "schemaVersion": 41, "editable": False, "graphTooltip": 1,
        "annotations": {"list": []}, "templating": {"list": []}, "links": [], "panels": panels,
    }


def build_targets(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for d in cfg["devices"]:
        for s in d.get("services") or []:
            out.append({"targets": [s["target"]],
                        "labels": {"device": d["id"], "service": s["name"], "module": s["module"]}})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, default=HERE / "casa.devices.yml")
    ap.add_argument("--dashboard-out", type=Path, default=HERE / "dashboards" / "casa.json")
    ap.add_argument("--targets-out", type=Path, default=HERE.parent / "prometheus" / "servicios" / "servicios.yml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    args.dashboard_out.parent.mkdir(parents=True, exist_ok=True)
    args.targets_out.parent.mkdir(parents=True, exist_ok=True)
    args.dashboard_out.write_text(json.dumps(build_dashboard(cfg), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    header = "# GENERATED by grafana/build_dashboard.py from casa.devices.yml. Do not edit.\n"
    args.targets_out.write_text(header + yaml.safe_dump(build_targets(cfg), allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"wrote {args.dashboard_out} and {args.targets_out} ({sum(len(d.get('services') or []) for d in cfg['devices'])} probes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
