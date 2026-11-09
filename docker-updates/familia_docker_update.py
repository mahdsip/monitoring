#!/usr/bin/env python3
"""familIA weekly Docker image updater (one host, docker compose projects).

Run as root from a systemd timer (Monday early morning). For each service in
the config, in order: pull its tag; if the image changed, run the optional
backup, recreate only that service and health-check it. If it is unhealthy,
put the previous image back under the same tag (and restore the backup when
the backup type supports it), recreate again and re-check (rollback).

Results go to the node_exporter textfile directory
(familia_docker_update.prom) for Grafana. Nothing listens on the network.

Config (JSON, /etc/familia/docker-updates.json):
  {
    "textfile_dir": "/var/lib/node_exporter/textfile_collector",
    "projects": [
      {"dir": "/home/pi/monitoring", "file": "docker-compose.yml",
       "services": {
         "blackbox": {"health": [{"exec": ["wget", "-qO-", "http://127.0.0.1:9115/-/healthy"]}]},
         "grafana":  {"backup": {"type": "volume-tar", "volume": "grafana-data", "dir": "/srv/b", "keep": 2},
                      "health": [{"http": "http://127.0.0.1:9003/api/health"}]},
         "mariadb":  {"backup": {"type": "mariadb-dump", "dir": "/srv/b", "keep": 4},
                      "health": [{"exec": ["sh", "-c", "..."]}]}
       }}
    ]
  }
  health checks: {"http": URL} (from the host, 2xx/3xx) or {"exec": argv}
  (run inside the service's container). Every check must pass twice in a
  row; the container must also be running and not restarting.
  backup types:
    mariadb-dump  logical dump of all databases (kept for manual restore).
    volume-tar    stop the service, tar its volume; restored automatically
                  on rollback (the new version may have migrated the data).

Usage:
  familia_docker_update.py [--config PATH] [--dry-run] [--only SERVICE ...]

Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_CONFIG = "/etc/familia/docker-updates.json"
DEFAULT_TEXTFILE_DIR = "/var/lib/node_exporter/textfile_collector"
OUTPUT_NAME = "familia_docker_update.prom"
ROLLBACK_TAG = "familia-rollback"
HEALTH_TIMEOUT = 180
HEALTH_INTERVAL = 5
HTTP_TIMEOUT = 10

# Final state of a service after a run.
OK_RESULTS = ("unchanged", "updated")
RESULTS = OK_RESULTS + ("rolled_back", "rollback_failed", "pull_failed", "backup_failed", "error")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def stamp(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
class CommandError(RuntimeError):
    pass


def run(cmd: Sequence[str], timeout: int = 900, cwd: Optional[str] = None, retries: int = 0) -> str:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    for attempt in range(retries + 1):
        res = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=timeout, cwd=cwd, env=env)
        if res.returncode == 0:
            return res.stdout.decode("utf-8", "replace").strip()
        if attempt < retries:
            time.sleep(30)
    err = res.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
    raise CommandError(f"{' '.join(cmd[:3])} exited {res.returncode}: {' | '.join(err)}")


def detect_compose(runner=run) -> List[str]:
    """`docker compose` (v2 plugin) if present, else Debian's `docker-compose` (v1)."""
    try:
        runner(["docker", "compose", "version"], timeout=30)
        return ["docker", "compose"]
    except (CommandError, OSError):
        pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise CommandError("neither `docker compose` nor `docker-compose` is available")


def split_ref(ref: str) -> Tuple[str, str]:
    """'grafana/grafana:13.2.5' -> ('grafana/grafana', '13.2.5'); no tag -> 'latest'.
    A ':' before the last '/' belongs to a registry port, not to the tag."""
    ref = ref.split("@", 1)[0]
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1:]
    return ref, "latest"


# --------------------------------------------------------------------------
# Docker / compose operations (thin wrappers, injectable for tests)
# --------------------------------------------------------------------------
class Docker:
    def __init__(self, project_dir: str, compose_file: str, compose_cmd: List[str], runner=run) -> None:
        self.dir = project_dir
        self.compose = compose_cmd + ["-f", os.path.join(project_dir, compose_file)]
        self.run = runner

    def container_id(self, service: str) -> str:
        cid = self.run(self.compose + ["ps", "-q", service], cwd=self.dir, timeout=60)
        if not cid:
            raise CommandError(f"service {service} has no container")
        return cid.splitlines()[0]

    def inspect(self, cid: str, fmt: str) -> str:
        return self.run(["docker", "inspect", "-f", fmt, cid], timeout=60)

    def image_id(self, ref: str) -> str:
        return self.run(["docker", "image", "inspect", "-f", "{{.Id}}", ref], timeout=60)

    def volume_mountpoint(self, volume: str) -> str:
        return self.run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", volume], timeout=60)

    def tag(self, image_id: str, ref: str) -> None:
        self.run(["docker", "tag", image_id, ref], timeout=60)

    def pull(self, service: str) -> None:
        self.run(self.compose + ["pull", service], cwd=self.dir, timeout=1800, retries=2)

    def stop(self, service: str) -> None:
        self.run(self.compose + ["stop", "-t", "30", service], cwd=self.dir, timeout=120)

    def up(self, service: str) -> None:
        self.run(self.compose + ["up", "-d", "--no-deps", "--force-recreate", service], cwd=self.dir, timeout=600)

    def exec(self, cid: str, argv: Sequence[str], timeout: int = 30) -> str:
        return self.run(["docker", "exec", cid] + list(argv), timeout=timeout)

    def popen(self, argv: Sequence[str]) -> subprocess.Popen:
        return subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def prune_dangling(self) -> str:
        return self.run(["docker", "image", "prune", "-f"], timeout=600)


def http_ok(url: str, opener=urllib.request.urlopen) -> bool:
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": "familia-docker-update"}),
                    timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def healthy_once(docker: Docker, service: str, checks: List[Dict[str, object]], http=http_ok) -> Tuple[bool, str]:
    try:
        cid = docker.container_id(service)
        state = docker.inspect(cid, "{{.State.Status}} {{.State.Restarting}}")
    except CommandError as exc:
        return False, str(exc)
    if state != "running false":
        return False, f"container state: {state}"
    for check in checks:
        if "http" in check:
            if not http(str(check["http"])):
                return False, f"http {check['http']} failed"
        elif "exec" in check:
            try:
                docker.exec(cid, [str(a) for a in check["exec"]])  # type: ignore[union-attr]
            except (CommandError, subprocess.TimeoutExpired) as exc:
                return False, f"exec check failed: {exc}"
        else:
            return False, f"unknown health check {check}"
    return True, "healthy"


def wait_healthy(docker: Docker, service: str, checks: List[Dict[str, object]], timeout: int = HEALTH_TIMEOUT,
                 interval: int = HEALTH_INTERVAL, http=http_ok, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Tuple[bool, str]:
    """Healthy = all checks pass twice in a row (catches crash loops that start, then die)."""
    deadline = clock() + timeout
    passes, reason = 0, "not checked"
    while True:
        ok, reason = healthy_once(docker, service, checks, http=http)
        passes = passes + 1 if ok else 0
        if passes >= 2:
            return True, reason
        if clock() >= deadline:
            return False, reason
        sleep(interval)


# --------------------------------------------------------------------------
# Backups before an update
# --------------------------------------------------------------------------
def _private_file(path: str) -> None:
    """Create/truncate `path` with mode 600 before anything is written to it."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))


def _rotate(out_dir: str, prefix: str, suffix: str, keep: int) -> None:
    for old in sorted(glob.glob(os.path.join(out_dir, f"{prefix}-*{suffix}")))[:-keep]:
        os.remove(old)


def mariadb_dump(docker: Docker, service: str, spec: Dict[str, object], now: Optional[float] = None) -> str:
    """Full logical dump from inside the container. The root password is read
    from the container's own MARIADB_ROOT_PASSWORD env and passed via
    MYSQL_PWD, so it never appears in a command line or in this config."""
    out_dir = str(spec["dir"])
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = os.path.join(out_dir, f"{service}-{stamp(now)}.sql.gz")
    cid = docker.container_id(service)
    script = ('MYSQL_PWD="${MARIADB_ROOT_PASSWORD:?}" exec mariadb-dump -uroot --all-databases '
              '--single-transaction --routines --events --triggers')
    tmp = path + ".part"
    tail = b""
    _private_file(tmp)
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        proc = docker.popen(["docker", "exec", cid, "sh", "-c", script])
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            gz.write(chunk)
            tail = (tail + chunk)[-300:]
        _, err = proc.communicate(timeout=3600)
    if proc.returncode != 0:
        os.remove(tmp)
        raise CommandError(f"mariadb-dump exited {proc.returncode}: {err.decode('utf-8', 'replace').strip()[-300:]}")
    if b"-- Dump completed" not in tail:  # a complete dump ends with this marker
        os.remove(tmp)
        raise CommandError("mariadb-dump output is incomplete")
    os.replace(tmp, path)
    _rotate(out_dir, service, ".sql.gz", int(spec.get("keep", 4)))  # type: ignore[arg-type]
    return path


def volume_tar(docker: Docker, service: str, spec: Dict[str, object], now: Optional[float] = None) -> str:
    """Stop the service (consistent copy, e.g. Grafana's SQLite) and tar its
    volume with numeric owners and permissions."""
    out_dir = str(spec["dir"])
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    mount = docker.volume_mountpoint(str(spec["volume"]))
    path = os.path.join(out_dir, f"{spec['volume']}-{stamp(now)}.tar.gz")
    tmp = path + ".part"
    docker.stop(service)
    _private_file(tmp)
    try:
        docker.run(["tar", "--numeric-owner", "-C", mount, "-czpf", tmp, "."], timeout=1800)
    except CommandError:
        os.remove(tmp)
        raise
    os.replace(tmp, path)
    _rotate(out_dir, str(spec["volume"]), ".tar.gz", int(spec.get("keep", 2)))  # type: ignore[arg-type]
    return path


def volume_restore(docker: Docker, service: str, spec: Dict[str, object], path: str,
                   now: Optional[float] = None) -> str:
    """Put the volume back as it was before the update. The data the new
    version left is kept next to it (<mountpoint>.failed-<stamp>), not deleted."""
    mount = docker.volume_mountpoint(str(spec["volume"]))
    docker.stop(service)
    restored = f"{mount}.restore-{stamp(now)}"
    failed = f"{mount}.failed-{stamp(now)}"
    os.makedirs(restored)
    docker.run(["tar", "--numeric-owner", "-xzpf", path, "-C", restored], timeout=1800)
    os.rename(mount, failed)
    os.rename(restored, mount)
    return failed


# type -> (backup, restore-on-rollback or None)
BACKUPS: Dict[str, Tuple[Callable, Optional[Callable]]] = {
    "mariadb-dump": (mariadb_dump, None),
    "volume-tar": (volume_tar, volume_restore),
}


# --------------------------------------------------------------------------
# Per-service logic
# --------------------------------------------------------------------------
def update_service(docker: Docker, service: str, spec: Dict[str, object], dry_run: bool = False,
                   wait=wait_healthy, backups=BACKUPS) -> str:
    """Returns one of RESULTS."""
    cid = docker.container_id(service)
    ref = docker.inspect(cid, "{{.Config.Image}}")
    old_id = docker.inspect(cid, "{{.Image}}")
    repo, _ = split_ref(ref)
    checks = list(spec.get("health", []))  # type: ignore[arg-type]
    # Slow starters (Prometheus replaying its WAL) can ask for more time.
    wait_kw = {"timeout": int(spec["health_timeout"])} if spec.get("health_timeout") else {}  # type: ignore[arg-type]
    if dry_run:
        log(f"{service}: would pull {ref} and recreate if changed (now {old_id[:19]})")
        return "unchanged"

    try:
        docker.pull(service)
    except CommandError as exc:
        log(f"{service}: pull failed: {exc}")
        return "pull_failed"
    new_id = docker.image_id(ref)
    if new_id == old_id:
        log(f"{service}: {ref} is up to date")
        return "unchanged"
    log(f"{service}: {ref} {old_id[:19]} -> {new_id[:19]}")

    # Keep the running image reachable by name: rollback target, safe from prune.
    docker.tag(old_id, f"{repo}:{ROLLBACK_TAG}")

    backup: Optional[Dict[str, object]] = spec.get("backup")  # type: ignore[assignment]
    backup_path, restore = None, None
    if backup:
        make, restore = backups[str(backup["type"])]
        try:
            backup_path = make(docker, service, backup)
            log(f"{service}: backup written to {backup_path}")
        except (CommandError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
            log(f"{service}: backup failed, NOT updating: {exc}")
            docker.tag(old_id, ref)  # the tag points to the running image again
            try:
                docker.up(service)  # the backup may have stopped it
            except CommandError as up_exc:
                log(f"{service}: could not restart the old container: {up_exc}")
                return "rollback_failed"
            return "backup_failed"

    try:
        docker.up(service)
        ok, reason = wait(docker, service, checks, **wait_kw)
    except CommandError as exc:
        ok, reason = False, str(exc)
    if ok:
        log(f"{service}: updated and healthy")
        return "updated"

    log(f"{service}: unhealthy after update ({reason}) -> rolling back to {old_id[:19]}")
    try:
        docker.tag(old_id, ref)
        if restore and backup_path:
            kept = restore(docker, service, backup, backup_path)
            log(f"{service}: volume restored from {backup_path} (new version's data kept at {kept})")
        docker.up(service)
        ok, reason = wait(docker, service, checks, **wait_kw)
    except (CommandError, OSError) as exc:
        ok, reason = False, str(exc)
    if ok:
        log(f"{service}: rolled back and healthy")
        return "rolled_back"
    log(f"{service}: ROLLBACK FAILED ({reason})")
    return "rollback_failed"


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_metrics(results: Dict[str, str], finished: float) -> str:
    def line(name: str, value: float, **labels: str) -> str:
        lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        val = str(int(value)) if float(value).is_integer() else repr(float(value))  # no 1.79e+09 rounding
        return f"{name}{{{lbl}}} {val}" if lbl else f"{name} {val}"

    out = [
        "# HELP familia_docker_update_last_run_timestamp_seconds End of the last weekly Docker update run.",
        "# TYPE familia_docker_update_last_run_timestamp_seconds gauge",
        line("familia_docker_update_last_run_timestamp_seconds", round(finished)),
        "# HELP familia_docker_update_run_success 1 if every service ended unchanged or updated.",
        "# TYPE familia_docker_update_run_success gauge",
        line("familia_docker_update_run_success", int(all(r in OK_RESULTS for r in results.values()))),
    ]
    if results:
        out += ["# HELP familia_docker_update_result Outcome of the last run per container (1 = this result).",
                "# TYPE familia_docker_update_result gauge"]
        out += [line("familia_docker_update_result", 1, container=c, result=r) for c, r in results.items()]
    return "\n".join(out) + "\n"


def write_atomically(directory: str, content: str) -> str:
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".familia_docker_update.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o644)
    path = os.path.join(directory, OUTPUT_NAME)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def validate_config(cfg: Dict[str, object]) -> None:
    projects = cfg.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("config needs a non-empty 'projects' list")
    for p in projects:
        if not os.path.isabs(str(p.get("dir", ""))):
            raise ValueError(f"project dir must be absolute: {p.get('dir')}")
        for name, spec in p.get("services", {}).items():
            if not spec.get("health"):
                raise ValueError(f"{name}: needs at least one health check")
            backup = spec.get("backup")
            if backup:
                if backup.get("type") not in BACKUPS:
                    raise ValueError(f"{name}: unknown backup type {backup.get('type')}")
                if not os.path.isabs(str(backup.get("dir", ""))):
                    raise ValueError(f"{name}: backup dir must be absolute")
                if backup["type"] == "volume-tar" and not backup.get("volume"):
                    raise ValueError(f"{name}: volume-tar backup needs 'volume'")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="no pulls, no recreation, no metrics file")
    ap.add_argument("--only", nargs="+", metavar="SERVICE", help="limit to these services")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    validate_config(cfg)
    compose_cmd = detect_compose()
    log(f"start ({'dry run, ' if args.dry_run else ''}compose: {' '.join(compose_cmd)})")

    results: Dict[str, str] = {}
    for project in cfg["projects"]:
        docker = Docker(project["dir"], project.get("file", "docker-compose.yml"), compose_cmd)
        for service, spec in project["services"].items():
            if args.only and service not in args.only:
                continue
            try:
                results[service] = update_service(docker, service, spec, dry_run=args.dry_run)
            except (CommandError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                log(f"{service}: error: {exc}")
                results[service] = "error"
        if not args.dry_run and any(r == "updated" for r in results.values()):
            try:
                log("prune dangling images: " + docker.prune_dangling().splitlines()[-1])
            except (CommandError, IndexError) as exc:
                log(f"prune failed: {exc}")

    ok = all(r in OK_RESULTS for r in results.values())
    if not args.dry_run:
        path = write_atomically(cfg.get("textfile_dir", DEFAULT_TEXTFILE_DIR), render_metrics(results, time.time()))
        log(f"metrics written to {path}")
    log(f"RESULT={'ok' if ok else 'needs-review'} " + " ".join(f"{s}={r}" for s, r in results.items()))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
