#!/usr/bin/env python3
"""familIA host metrics: OS updates, Docker image updates and app versions.

Writes Prometheus metrics in the node_exporter "textfile" format, so the
existing node_exporter on each host publishes them. Nothing listens on the
network; nothing is updated. Run as root from a daily systemd timer.

Sections (enable per host with flags):
  --apt     pending apt upgrades, autoremove, reboot required, cache age.
            Same metric names as Debian's prometheus-node-exporter-collectors
            (apt_upgrades_pending{origin,arch}, ...), so dashboards treat all
            hosts alike.
  --docker  for each running container: is a newer image published for its
            tag? (compares the local repo digest with the registry's digest
            for the same tag; HEAD requests only, no pulls)
  --pihole  Pi-hole component versions (current vs latest from `pihole -v`)

Usage:
  familia_host_metrics.py --textfile-dir DIR [--apt] [--docker] [--pihole]

Compatible with Python 3.9+ (Raspberry Pi OS bullseye). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

OUTPUT_NAME = "familia_host.prom"
HTTP_TIMEOUT = 15
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


# --------------------------------------------------------------------------
# Prometheus text format helpers
# --------------------------------------------------------------------------
def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    def __init__(self) -> None:
        self._help: Dict[str, Tuple[str, str]] = {}
        self._samples: List[Tuple[str, Dict[str, str], float]] = []

    def declare(self, name: str, mtype: str, help_text: str) -> None:
        self._help.setdefault(name, (mtype, help_text))

    def add(self, name: str, value: float, **labels: str) -> None:
        self._samples.append((name, labels, float(value)))

    def render(self) -> str:
        out: List[str] = []
        for name, (mtype, help_text) in self._help.items():
            rows = [s for s in self._samples if s[0] == name]
            if not rows:
                continue
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {mtype}")
            for _, labels, value in rows:
                lbl = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
                val = str(int(value)) if value.is_integer() else repr(value)  # :g would round timestamps
                out.append(f"{name}{{{lbl}}} {val}" if lbl else f"{name} {val}")
        return "\n".join(out) + "\n"


def write_atomically(directory: str, content: str) -> str:
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".familia_host.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o644)
    path = os.path.join(directory, OUTPUT_NAME)
    os.replace(tmp, path)  # node_exporter never reads a half-written file
    return path


class CommandError(RuntimeError):
    pass


def run(cmd: List[str], timeout: int = 120, retries: int = 0, retry_delay: float = 30.0) -> str:
    """Run a command; on failure retry, then raise with its stderr tail."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    for attempt in range(retries + 1):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
        if res.returncode == 0:
            return res.stdout.decode("utf-8", "replace")
        if attempt < retries:
            time.sleep(retry_delay)
    err = res.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
    raise CommandError(f"{' '.join(cmd[:2])} exited {res.returncode}: {' | '.join(err)}")


# --------------------------------------------------------------------------
# apt
# --------------------------------------------------------------------------
# Inst libssl3 [3.0.14-1~deb12u1] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [arm64])
_INST_RE = re.compile(r"^Inst (\S+) (?:\[[^\]]*\] )?\((\S+) (.+?) \[([^\]]+)\]\)")
_REMV_RE = re.compile(r"^Remv \S+")


def parse_apt_simulation(text: str) -> Dict[Tuple[str, str], int]:
    """Count upgrades per (origin, arch) from `apt-get -s dist-upgrade`."""
    counts: Dict[Tuple[str, str], int] = {}
    for line in text.splitlines():
        m = _INST_RE.match(line)
        if not m:
            continue
        origin, arch = m.group(3).strip(), m.group(4).strip()
        counts[(origin, arch)] = counts.get((origin, arch), 0) + 1
    return counts


def count_autoremove(text: str) -> int:
    return sum(1 for line in text.splitlines() if _REMV_RE.match(line))


def collect_apt(metrics: Metrics) -> None:
    metrics.declare("apt_upgrades_pending", "gauge", "Apt packages pending upgrade, by origin and arch.")
    metrics.declare("apt_autoremove_pending", "gauge", "Apt packages pending autoremove.")
    metrics.declare("apt_package_cache_timestamp_seconds", "gauge", "Last successful apt cache update.")
    metrics.declare("node_reboot_required", "gauge", "1 if the node requires a reboot (/run/reboot-required).")
    flags = ["-o", "Debug::NoLocking=1", "-q"]
    # Without locking, a concurrent apt run (e.g. apt-daily refreshing the
    # lists) can make the simulation fail transiently: retry before giving up.
    upgrades = parse_apt_simulation(run(["apt-get", "-s", *flags, "dist-upgrade"], retries=2))
    if not upgrades:
        metrics.add("apt_upgrades_pending", 0, origin="", arch="")
    for (origin, arch), n in sorted(upgrades.items()):
        metrics.add("apt_upgrades_pending", n, origin=origin, arch=arch)
    metrics.add("apt_autoremove_pending", count_autoremove(run(["apt-get", "-s", *flags, "autoremove"], retries=2)))
    for stamp in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists/partial", "/var/lib/apt/lists"):
        if os.path.exists(stamp):
            metrics.add("apt_package_cache_timestamp_seconds", os.stat(stamp).st_mtime)
            break
    metrics.add("node_reboot_required", 1 if os.path.exists("/run/reboot-required") else 0)


# --------------------------------------------------------------------------
# Docker image updates
# --------------------------------------------------------------------------
def parse_image_ref(ref: str) -> Tuple[str, str, str]:
    """'grafana/grafana:13.2.5' -> ('registry-1.docker.io', 'grafana/grafana', '13.2.5')."""
    ref = ref.split("@", 1)[0]
    registry = "docker.io"
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, ref = first, rest
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in name:
            name = "library/" + name
    return registry, name, tag


def _bearer_challenge(header: str) -> Dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', header or ""))


def remote_digest(registry: str, name: str, tag: str, opener=urllib.request.urlopen) -> str:
    """Digest the registry currently serves for name:tag (anonymous)."""
    url = f"https://{registry}/v2/{name}/manifests/{urllib.parse.quote(tag)}"
    headers = {"Accept": MANIFEST_ACCEPT}

    def head(hdrs: Dict[str, str]):
        return opener(urllib.request.Request(url, headers=hdrs, method="HEAD"), timeout=HTTP_TIMEOUT)

    try:
        resp = head(headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        ch = _bearer_challenge(exc.headers.get("WWW-Authenticate", ""))
        if "realm" not in ch:
            raise
        query = {"service": ch.get("service", ""), "scope": ch.get("scope", f"repository:{name}:pull")}
        token_url = ch["realm"] + "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
        with opener(urllib.request.Request(token_url), timeout=HTTP_TIMEOUT) as tresp:
            body = json.loads(tresp.read().decode("utf-8"))
        token = body.get("token") or body.get("access_token")
        resp = head(dict(headers, Authorization=f"Bearer {token}"))
    with resp:
        digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError("registry returned no digest")
    return digest


def resolve_container_ref(ref: str, repo_tags: List[str]) -> str:
    """Human image reference for a container.

    A container started by digest (repo@sha256:...) or by bare image ID
    (sha256:...) carries no tag; use the tag the image was pulled as.
    """
    if ("@" in ref or ref.startswith("sha256:") or re.fullmatch(r"[0-9a-f]{12,64}", ref)) and repo_tags:
        return repo_tags[0]
    return ref


def docker_containers() -> List[Dict[str, object]]:
    out = []
    ids = run(["docker", "ps", "-q", "--no-trunc"]).split()
    if not ids:
        return out
    for c in json.loads(run(["docker", "inspect", *ids])):
        image_id = c.get("Image", "")
        img = json.loads(run(["docker", "image", "inspect", image_id]))[0]
        ref = c.get("Config", {}).get("Image", "")
        tags = img.get("RepoTags") or []
        ref = resolve_container_ref(ref, tags)
        out.append(
            {
                "container": c.get("Name", "").lstrip("/"),
                "ref": ref,
                "repo_digests": img.get("RepoDigests") or [],
                "created": img.get("Created", ""),
            }
        )
    return out


def _iso_to_epoch(value: str) -> Optional[float]:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", value or "")
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")) - time.timezone


def image_update_status(
    ref: str, repo_digests: Iterable[str], digest_lookup=remote_digest
) -> Tuple[Optional[int], str]:
    """(1 update available | 0 up to date | None unknown, reason)."""
    registry, name, tag = parse_image_ref(ref)
    local = {d.split("@", 1)[1] for d in repo_digests if "@" in d}
    if not local:
        return None, "no local repo digest (image built locally?)"
    try:
        remote = digest_lookup(registry, name, tag)
    except Exception as exc:  # noqa: BLE001 - record and continue with other images
        return None, f"{type(exc).__name__}: {exc}"[:120]
    return (0 if remote in local else 1), "ok"


def collect_docker(metrics: Metrics, digest_lookup=remote_digest, containers=None) -> None:
    metrics.declare("familia_docker_image_update_available", "gauge",
                    "1 if the registry has a newer image for the container's tag, 0 if up to date.")
    metrics.declare("familia_docker_image_check_error", "gauge", "1 if the update check failed for this container.")
    metrics.declare("familia_docker_image_created_timestamp_seconds", "gauge", "Creation time of the running image.")
    for c in containers if containers is not None else docker_containers():
        _, name, tag = parse_image_ref(str(c["ref"]))
        image = name.replace("library/", "", 1)
        labels = {"container": str(c["container"]), "image": image, "tag": tag}
        status, reason = image_update_status(str(c["ref"]), c["repo_digests"], digest_lookup)  # type: ignore[arg-type]
        if status is None:
            metrics.add("familia_docker_image_check_error", 1, reason=reason, **labels)
        else:
            metrics.add("familia_docker_image_update_available", status, **labels)
            metrics.add("familia_docker_image_check_error", 0, reason="", **labels)
        created = _iso_to_epoch(str(c.get("created", "")))
        if created:
            metrics.add("familia_docker_image_created_timestamp_seconds", created, **labels)


# --------------------------------------------------------------------------
# Pi-hole
# --------------------------------------------------------------------------
_PIHOLE_RE = re.compile(r"^(\w+) version is (\S+)(?: \(Latest: (\S+)\))?", re.MULTILINE)


def parse_pihole_versions(text: str) -> List[Tuple[str, str, str]]:
    return [(m.group(1).lower(), m.group(2), m.group(3) or m.group(2)) for m in _PIHOLE_RE.finditer(text)]


def collect_pihole(metrics: Metrics) -> None:
    metrics.declare("familia_app_update_available", "gauge", "1 if a newer version of the app component exists.")
    metrics.declare("familia_app_version_info", "gauge", "Current and latest version of an app component.")
    for component, current, latest in parse_pihole_versions(run(["pihole", "-v"])):
        app = f"pihole-{component}"
        metrics.add("familia_app_version_info", 1, app=app, current=current, latest=latest)
        metrics.add("familia_app_update_available", 0 if current == latest else 1, app=app)


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--textfile-dir", required=True)
    ap.add_argument("--apt", action="store_true")
    ap.add_argument("--docker", action="store_true")
    ap.add_argument("--pihole", action="store_true")
    args = ap.parse_args(argv)

    metrics = Metrics()
    metrics.declare("familia_host_metrics_section_success", "gauge", "1 if the section collected successfully.")
    metrics.declare("familia_host_metrics_last_run_timestamp_seconds", "gauge", "When this collector last ran.")
    failed = False
    for enabled, section, fn in ((args.apt, "apt", collect_apt), (args.docker, "docker", collect_docker),
                                 (args.pihole, "pihole", collect_pihole)):
        if not enabled:
            continue
        try:
            fn(metrics)
            metrics.add("familia_host_metrics_section_success", 1, section=section)
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            failed = True
            print(f"section {section} failed: {exc}", file=sys.stderr)
            metrics.add("familia_host_metrics_section_success", 0, section=section)
    metrics.add("familia_host_metrics_last_run_timestamp_seconds", time.time())
    path = write_atomically(args.textfile_dir, metrics.render())
    print(f"wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
