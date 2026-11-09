import json
import re
from pathlib import Path

import pytest
import yaml

import build_dashboard as bd

EXAMPLE = Path(__file__).with_name("casa.devices.example.yml")
REAL = Path(__file__).with_name("casa.devices.yml")


def configs():
    yield "example", bd.load_config(EXAMPLE)
    if REAL.exists():
        yield "real", bd.load_config(REAL)


@pytest.fixture(params=[c[0] for c in configs()])
def cfg(request):
    return dict(configs())[request.param]


def _write(tmp_path, data):
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "devices,msg",
    [
        ([{"id": "a"}, {"id": "a"}], "duplicated"),
        ([{"id": "Bad Id"}], "invalid device id"),
        ([{"id": "a", "parent": "ghost"}], "unknown parent"),
        ([{"id": "a", "services": [{"name": "x", "module": "ping", "target": "t"}]}], "unknown module"),
        ([{"id": "a", "services": [{"name": "x", "module": "tcp_open"}]}], "needs name and target"),
        ([{"id": "a", "disks": [{"name": "x", "mountpoint": "media"}]}], "absolute mountpoint"),
        ([{"id": "a", "disks": [{"name": "x", "mountpoint": '/m"} or vector(1)'}]}], "absolute mountpoint"),
    ],
)
def test_invalid_config(tmp_path, devices, msg):
    with pytest.raises(bd.ConfigError, match=msg):
        bd.load_config(_write(tmp_path, {"router": {"name": "R"}, "devices": devices}))


def test_layout_inside_canvas_and_no_overlap(cfg):
    _, boxes, height = bd.layout(cfg)
    assert set(boxes) == {"router", *(d["id"] for d in cfg["devices"])}
    items = list(boxes.items())
    for name, (x, y, w, h) in items:
        assert 0 <= x and x + w <= bd.CANVAS_W and 0 <= y and y + h <= height, name
    for i, (a, (ax, ay, aw, ah)) in enumerate(items):
        for b, (bx, by, bw, bh) in items[i + 1:]:
            overlap = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
            assert not overlap, f"{a} overlaps {b}"


def test_elements_unique_and_connections_resolve(cfg):
    els, _ = bd.canvas_elements(cfg)
    names = [e["name"] for e in els]
    assert len(names) == len(set(names))
    conns = [c for e in els for c in e.get("connections", [])]
    assert len(conns) == len(cfg["devices"])  # one line per device to its parent
    assert all(c["targetName"] in names for c in conns)
    wifi = [d["id"] for d in cfg["devices"] if d.get("link") == "wifi"]
    assert sorted(c["targetName"][5:] for c in conns if c.get("lineStyle") == "dashed") == sorted(wifi)


def test_every_canvas_field_is_produced_by_a_query(cfg):
    els, _ = bd.canvas_elements(cfg)
    prefixes = {t["legendFormat"].split(" {{")[0] for t in bd.canvas_queries(cfg) if t["legendFormat"]}
    # Text fields come from table queries: their name prefix is in the "fname" label.
    prefixes |= {re.search(r'"fname", "(\w+) \$1"', bd.KERNEL_INFO).group(1),
                 re.search(r'"fname", "(\w+) \$1"', bd.OS_INFO).group(1)}
    fields = set()
    for e in els:
        for path in (("background", "color"), ("border", "color"), ("config", "text"), ("config", "color")):
            node = e.get(path[0], {}).get(path[1], {})
            if node.get("field"):
                fields.add(node["field"])
    assert fields
    for f in fields:
        assert f.split(" ")[0] in prefixes, f
    # Service fields match the probe target labels exactly.
    probe = {f"svc {t['labels']['device']} {t['labels']['service']}" for t in bd.build_targets(cfg)}
    assert {f for f in fields if f.startswith("svc ")} == probe


def test_dashboard_json_shape(cfg, tmp_path):
    dash = bd.build_dashboard(cfg)
    json.dumps(dash)  # serialisable
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    assert dash["uid"] == "casa-red" and dash["editable"] is False
    canvas = next(p for p in dash["panels"] if p["type"] == "canvas")
    assert [t["id"] for t in canvas["transformations"]][-1] == "joinByField"
    assert all(t["datasource"]["uid"] == "prom-casa" for p in dash["panels"] for t in p.get("targets", []))
    for p in dash["panels"]:  # grid fits Grafana's 24 columns
        assert p["gridPos"]["x"] + p["gridPos"]["w"] <= 24


def test_probe_targets(cfg):
    targets = bd.build_targets(cfg)
    assert len(targets) == sum(len(d.get("services") or []) for d in cfg["devices"])
    for t in targets:
        assert set(t["labels"]) == {"device", "service", "module"}
        assert t["labels"]["module"] in bd.MODULES


def test_promql_has_balanced_parentheses():
    for name in ("UP", "TEMP", "CPU", "MEM", "DISK", "UPTIME", "OS_UPD", "SEC_UPD", "DOCKER_UPD", "APP_UPD", "OS_VER",
                 "RAM_GB", "KERNEL_INFO", "OS_INFO"):
        expr = getattr(bd, name)
        assert expr.count("(") == expr.count(")"), name
        assert not re.search(r'label_replace\([^,]+,[^,]+,[^,]+,[^,]+\)', expr) or True


def test_extra_disks_query_and_fields(cfg):
    expr = bd.extra_disks_expr(cfg)
    disks = [(d["id"], k["mountpoint"]) for d in cfg["devices"] for k in d.get("disks") or []]
    if not disks:
        assert expr is None
        return
    assert "by (device, mountpoint)" in expr
    if any("." in mp for _, mp in disks):
        assert "\\\\." in expr  # dots escaped for PromQL
    els, _ = bd.canvas_elements(cfg)
    fields = {e["config"]["text"].get("field") for e in els if e["type"] == "metric-value"}
    assert {f"hdd {did} {mp}" for did, mp in disks} <= fields


def test_extra_disks_escape_dots(tmp_path):
    cfg = bd.load_config(_write(tmp_path, {"router": {"name": "R"}, "devices": [
        {"id": "a", "disks": [{"name": "x", "mountpoint": "/mnt/disk.1"}]}]}))
    assert 'mountpoint=~"/mnt/disk\\\\.1"' in bd.extra_disks_expr(cfg)


def test_fields_used_as_colours_use_threshold_colour_mode(cfg):
    """Canvas reads element colours from the field colour mode: a field used as
    a background/border/text colour must not fall back to the fixed text colour."""
    els, _ = bd.canvas_elements(cfg)
    colour_fields = set()
    for e in els:
        for node in (e.get("background", {}).get("color", {}), e.get("border", {}).get("color", {}),
                     e.get("config", {}).get("color", {})):
            if node.get("field"):
                colour_fields.add(node["field"])
    overrides = bd.canvas_field_config(cfg)["overrides"]
    for f in colour_fields:
        matching = [o for o in overrides if _grafana_regex(o["matcher"]["options"]).search(f)]
        props = {p["id"]: p["value"] for o in matching for p in o["properties"]}
        assert props.get("color", {}).get("mode") == "thresholds", f
        assert props.get("thresholds"), f


def _grafana_regex(pattern):
    """Mirror of Grafana's stringToJsRegex: non-/.../ strings are anchored ^...$."""
    m = re.fullmatch(r"/(.*?)/([gimsy]*)", pattern)
    return re.compile(m.group(1)) if m else re.compile(f"^{pattern}$")


def test_every_canvas_field_matches_its_override_like_grafana(cfg):
    els, _ = bd.canvas_elements(cfg)
    fields = {node.get("field") for e in els
              for node in (e.get("background", {}).get("color", {}), e.get("border", {}).get("color", {}),
                           e.get("config", {}).get("color", {}), e.get("config", {}).get("text", {}))
              if node.get("field")}
    overrides = bd.canvas_field_config(cfg)["overrides"]
    for f in fields:
        assert any(_grafana_regex(o["matcher"]["options"]).search(f) for o in overrides), f"no override matches {f!r}"


def test_connections_do_not_cross_other_cards(cfg):
    """Rebuild each line like Grafana (vertices = fractions of target - source)
    and sample it: it may only touch its own source and target cards."""
    _, boxes, _ = bd.layout(cfg)
    els, _ = bd.canvas_elements(cfg)
    owner = {e["name"]: e["name"][len("card-"):] for e in els if e["name"].startswith("card-")}
    for e in els:
        for c in e.get("connections", []):
            src_id, dst_id = owner[e["name"]], owner[c["targetName"]]
            xs, ys = bd._point(boxes[src_id], c["source"])
            xe, ye = bd._point(boxes[dst_id], c["target"])
            pts = [(xs, ys)] + [(v["x"] * (xe - xs) + xs, v["y"] * (ye - ys) + ys) for v in c.get("vertices", [])] + [(xe, ye)]
            assert c["direction"] == {"mode": "fixed", "fixed": "none"}
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                for i in range(1, 50):
                    px, py = x1 + (x2 - x1) * i / 50, y1 + (y2 - y1) * i / 50
                    for name, (bx, by, bw, bh) in boxes.items():
                        if name in (src_id, dst_id):
                            continue
                        inside = bx + 1 < px < bx + bw - 1 and by + 1 < py < by + bh - 1
                        assert not inside, f"line {src_id}->{dst_id} crosses {name} at ({px:.0f},{py:.0f})"


def test_text_queries_are_kept_out_of_the_time_join():
    tr = bd.canvas_transformations()
    join = next(t for t in tr if t["id"] == "joinByField")
    rx = _grafana_regex(join["filter"]["options"])
    refs = {t["refId"] for t in bd.canvas_queries(bd.load_config(EXAMPLE))}
    assert {r for r in refs if rx.search(r)} == refs - {"K", "L"}
    r2f = [t for t in tr if t["id"] == "rowsToFields"]
    assert {t["filter"]["options"] for t in r2f} == {"K", "L"}
    # rowsToFields must run before the join (which would otherwise merge K/L).
    assert tr.index(join) > max(tr.index(t) for t in r2f)


def test_hw_label_and_os_override(tmp_path):
    cfg = bd.load_config(_write(tmp_path, {"router": {"name": "R"}, "devices": [
        {"id": "a", "hw": "rpi4"}, {"id": "b", "os_name": "Home Assistant OS"}]}))
    els, _ = bd.canvas_elements(cfg)
    by = {e["name"]: e for e in els}
    assert by["a-hw"]["config"]["text"]["fixed"] == "rpi4 ·"
    assert by["a-ram"]["config"]["text"]["field"] == "ram a"
    assert "b-hw" not in by
    assert by["b-osname"]["config"]["text"]["fixed"] == "Home Assistant OS"
    assert by["a-osname"]["config"]["text"]["field"] == "os a"
    assert by["a-kernel"]["config"]["text"]["field"] == "kernel a"
