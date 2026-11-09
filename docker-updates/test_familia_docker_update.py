"""Tests for familia_docker_update.py (no Docker needed: Docker is faked;
the volume backup/restore tests use the real `tar` on a temp directory).

uv run -q --no-project -p 3.13 --with pytest==8.3.5 pytest -q test_familia_docker_update.py
"""

import gzip
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import familia_docker_update as fdu  # noqa: E402

HERE = os.path.dirname(__file__)


class FakeDocker:
    """Images by ref, one container per service; records every call."""

    def __init__(self, services, registry):
        self.services = {s: dict(c) for s, c in services.items()}  # svc -> {ref, image}
        self.registry = registry  # ref -> image id after a pull
        self.tags = {c["ref"]: c["image"] for c in services.values()}
        self.calls = []
        self.fail_pull = set()

    def container_id(self, service):
        return f"cid-{service}"

    def inspect(self, cid, fmt):
        c = self.services[cid[4:]]
        return {"{{.Config.Image}}": c["ref"], "{{.Image}}": c["image"]}[fmt]

    def image_id(self, ref):
        return self.tags[ref]

    def tag(self, image_id, ref):
        self.calls.append(("tag", image_id, ref))
        self.tags[ref] = image_id

    def pull(self, service):
        self.calls.append(("pull", service))
        if service in self.fail_pull:
            raise fdu.CommandError("network down")
        ref = self.services[service]["ref"]
        self.tags[ref] = self.registry.get(ref, self.tags[ref])

    def up(self, service):
        self.calls.append(("up", service))
        c = self.services[service]
        c["image"] = self.tags[c["ref"]]


def healthy_unless(bad_images, seen_kw=None):
    def wait(docker, service, checks, **kw):
        if seen_kw is not None:
            seen_kw.append(kw)
        return docker.services[service]["image"] not in bad_images, "test"
    return wait


SPEC = {"health": [{"http": "http://x"}]}


def test_split_ref():
    assert fdu.split_ref("grafana/grafana:13.2.5") == ("grafana/grafana", "13.2.5")
    assert fdu.split_ref("prom/prometheus") == ("prom/prometheus", "latest")
    assert fdu.split_ref("localhost:5000/app") == ("localhost:5000/app", "latest")
    assert fdu.split_ref("localhost:5000/app:v1@sha256:abc") == ("localhost:5000/app", "v1")
    assert fdu.split_ref("arm64v8/mariadb:11") == ("arm64v8/mariadb", "11")


def test_unchanged_does_not_recreate():
    d = FakeDocker({"blackbox": {"ref": "prom/bb:latest", "image": "sha256:a"}}, {})
    assert fdu.update_service(d, "blackbox", SPEC, wait=healthy_unless(set())) == "unchanged"
    assert d.calls == [("pull", "blackbox")]


def test_update_ok_keeps_rollback_tag():
    d = FakeDocker({"blackbox": {"ref": "prom/bb:latest", "image": "sha256:a"}}, {"prom/bb:latest": "sha256:b"})
    assert fdu.update_service(d, "blackbox", SPEC, wait=healthy_unless(set())) == "updated"
    assert d.services["blackbox"]["image"] == "sha256:b"
    assert d.tags["prom/bb:familia-rollback"] == "sha256:a"


def test_health_timeout_is_passed_through():
    seen = []
    d = FakeDocker({"prometheus": {"ref": "p:latest", "image": "sha256:a"}}, {"p:latest": "sha256:b"})
    fdu.update_service(d, "prometheus", dict(SPEC, health_timeout=600), wait=healthy_unless(set(), seen))
    assert seen == [{"timeout": 600}]


def test_unhealthy_update_rolls_back_to_previous_image():
    d = FakeDocker({"blackbox": {"ref": "prom/bb:latest", "image": "sha256:a"}}, {"prom/bb:latest": "sha256:b"})
    assert fdu.update_service(d, "blackbox", SPEC, wait=healthy_unless({"sha256:b"})) == "rolled_back"
    assert d.services["blackbox"]["image"] == "sha256:a"
    assert d.tags["prom/bb:latest"] == "sha256:a"  # next `compose up` keeps using the old image
    assert [c[0] for c in d.calls] == ["pull", "tag", "up", "tag", "up"]


def test_rollback_failed_is_reported():
    d = FakeDocker({"x": {"ref": "r:1", "image": "sha256:a"}}, {"r:1": "sha256:b"})
    assert fdu.update_service(d, "x", SPEC, wait=healthy_unless({"sha256:a", "sha256:b"})) == "rollback_failed"


def test_pull_failure_changes_nothing():
    d = FakeDocker({"x": {"ref": "r:1", "image": "sha256:a"}}, {"r:1": "sha256:b"})
    d.fail_pull.add("x")
    assert fdu.update_service(d, "x", SPEC, wait=healthy_unless(set())) == "pull_failed"
    assert d.services["x"]["image"] == "sha256:a" and ("up", "x") not in d.calls


def test_backup_runs_before_recreate_and_failure_restarts_old_image():
    order = []
    d = FakeDocker({"mariadb": {"ref": "m:11", "image": "sha256:a"}}, {"m:11": "sha256:b"})
    spec = dict(SPEC, backup={"type": "fake"})

    def ok_backup(docker, service, b):
        order.append([c[0] for c in docker.calls])
        return "/tmp/x.sql.gz"

    assert fdu.update_service(d, "mariadb", spec, wait=healthy_unless(set()),
                              backups={"fake": (ok_backup, None)}) == "updated"
    assert order == [["pull", "tag"]]  # before any `up`

    d = FakeDocker({"mariadb": {"ref": "m:11", "image": "sha256:a"}}, {"m:11": "sha256:b"})

    def bad_backup(docker, service, b):
        raise fdu.CommandError("dump failed")

    assert fdu.update_service(d, "mariadb", spec, wait=healthy_unless(set()),
                              backups={"fake": (bad_backup, None)}) == "backup_failed"
    assert d.tags["m:11"] == "sha256:a"  # tag restored to the running image
    assert d.services["mariadb"]["image"] == "sha256:a"  # (re)started on the old image


def test_rollback_restores_backup_before_starting_old_image():
    events = []
    d = FakeDocker({"grafana": {"ref": "g:latest", "image": "sha256:a"}}, {"g:latest": "sha256:b"})
    spec = dict(SPEC, backup={"type": "vol"})

    def make(docker, service, b):
        return "/b/grafana-data.tar.gz"

    def restore(docker, service, b, path):
        events.append(("restore", path, docker.tags["g:latest"]))
        return "/v/_data.failed"

    real_up = d.up

    def up(service):
        events.append(("up", d.tags["g:latest"]))
        real_up(service)

    d.up = up
    assert fdu.update_service(d, "grafana", spec, wait=healthy_unless({"sha256:b"}),
                              backups={"vol": (make, restore)}) == "rolled_back"
    # new image up -> fails -> data restored (tag already back on old) -> old image up
    assert events == [("up", "sha256:b"), ("restore", "/b/grafana-data.tar.gz", "sha256:a"), ("up", "sha256:a")]


def test_no_restore_when_update_is_healthy():
    called = []
    d = FakeDocker({"grafana": {"ref": "g:latest", "image": "sha256:a"}}, {"g:latest": "sha256:b"})
    backups = {"vol": (lambda *a: "/b/x.tar.gz", lambda *a: called.append(a))}
    assert fdu.update_service(d, "grafana", dict(SPEC, backup={"type": "vol"}),
                              wait=healthy_unless(set()), backups=backups) == "updated"
    assert called == []


def test_dry_run_touches_nothing():
    d = FakeDocker({"x": {"ref": "r:1", "image": "sha256:a"}}, {"r:1": "sha256:b"})
    assert fdu.update_service(d, "x", SPEC, dry_run=True) == "unchanged"
    assert d.calls == []


class _HealthDocker:
    def __init__(self, states, exec_ok=True):
        self.states, self.exec_ok, self.n = list(states), exec_ok, 0

    def container_id(self, service):
        return "cid"

    def inspect(self, cid, fmt):
        self.n += 1
        return self.states[min(self.n - 1, len(self.states) - 1)]

    def exec(self, cid, argv, timeout=30):
        if not self.exec_ok:
            raise fdu.CommandError("exit 1")
        return ""


def _clock():
    t = [0.0]

    def sleep(s):
        t[0] += s

    return sleep, (lambda: t[0])


def test_wait_healthy_needs_two_consecutive_passes():
    sleep, clock = _clock()
    d = _HealthDocker(["running false", "restarting true", "running false", "running false"])
    ok, _ = fdu.wait_healthy(d, "s", [], timeout=60, interval=5, http=lambda u: True, sleep=sleep, clock=clock)
    assert ok and d.n == 4


def test_wait_healthy_times_out_on_failing_check():
    sleep, clock = _clock()
    d = _HealthDocker(["running false"], exec_ok=False)
    ok, reason = fdu.wait_healthy(d, "s", [{"exec": ["true"]}], timeout=20, interval=5,
                                  http=lambda u: True, sleep=sleep, clock=clock)
    assert not ok and "exec check failed" in reason


class _Proc:
    def __init__(self, out, rc=0, err=b""):
        self.stdout, self.returncode, self._err = io.BytesIO(out), rc, err

    def communicate(self, timeout=None):
        return b"", self._err


class _DumpDocker:
    def __init__(self, proc):
        self.proc, self.argv = proc, None

    def container_id(self, service):
        return "cid-mariadb"

    def popen(self, argv):
        self.argv = argv
        return self.proc


def test_mariadb_dump_writes_private_gzip_and_rotates(tmp_path):
    for i in range(5):
        (tmp_path / f"mariadb-2026010{i}-000000.sql.gz").write_bytes(b"old")
    dump = b"CREATE TABLE t (id int);\n-- Dump completed on 2026-11-02  2:30:00\n"
    d = _DumpDocker(_Proc(dump))
    path = fdu.mariadb_dump(d, "mariadb", {"dir": str(tmp_path), "keep": 3}, now=1793000000)
    assert gzip.open(path).read() == dump
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert len(list(tmp_path.glob("mariadb-*.sql.gz"))) == 3
    # The password comes from the container env, never from our argv.
    assert "MARIADB_ROOT_PASSWORD" in d.argv[-1] and "-p" not in d.argv[-1].split()


@pytest.mark.parametrize("proc", [_Proc(b"partial", rc=0), _Proc(b"", rc=2, err=b"Access denied")])
def test_mariadb_dump_rejects_failed_or_incomplete(tmp_path, proc):
    with pytest.raises(fdu.CommandError):
        fdu.mariadb_dump(_DumpDocker(proc), "mariadb", {"dir": str(tmp_path)})
    assert list(tmp_path.iterdir()) == []


class _VolumeDocker:
    """Real `tar` through fdu.run; the volume is a temp directory."""

    def __init__(self, mount):
        self.mount, self.stopped = str(mount), 0
        self.run = fdu.run

    def volume_mountpoint(self, volume):
        return self.mount

    def stop(self, service):
        self.stopped += 1


def test_volume_tar_backup_and_restore_roundtrip(tmp_path):
    vol = tmp_path / "volumes" / "grafana-data" / "_data"
    (vol / "plugins").mkdir(parents=True)
    (vol / "grafana.db").write_bytes(b"v13.2")
    (vol / "plugins" / "p.txt").write_text("plugin")
    os.chmod(vol / "grafana.db", 0o640)
    d = _VolumeDocker(vol)
    spec = {"volume": "grafana-data", "dir": str(tmp_path / "bk"), "keep": 2}

    path = fdu.volume_tar(d, "grafana", spec, now=1793000000)
    assert d.stopped == 1  # consistent copy: service stopped first
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    # the new version migrates the data, then turns out unhealthy
    (vol / "grafana.db").write_bytes(b"v13.3-migrated")
    (vol / "new-file").write_text("x")

    failed = fdu.volume_restore(d, "grafana", spec, path, now=1793000100)
    assert (vol / "grafana.db").read_bytes() == b"v13.2"
    assert oct(os.stat(vol / "grafana.db").st_mode & 0o777) == "0o640"
    assert (vol / "plugins" / "p.txt").read_text() == "plugin"
    assert not (vol / "new-file").exists()
    assert open(os.path.join(failed, "grafana.db"), "rb").read() == b"v13.3-migrated"  # kept, not deleted


def test_volume_tar_rotates(tmp_path):
    vol = tmp_path / "_data"
    vol.mkdir()
    (vol / "f").write_text("x")
    spec = {"volume": "grafana-data", "dir": str(tmp_path / "bk"), "keep": 2}
    for i in range(4):
        fdu.volume_tar(_VolumeDocker(vol), "grafana", spec, now=1793000000 + i)
    assert len(list((tmp_path / "bk").glob("grafana-data-*.tar.gz"))) == 2


def test_render_metrics():
    text = fdu.render_metrics({"blackbox": "updated", "node-exporter": "rolled_back"}, 1793000000.4)
    assert 'familia_docker_update_result{container="node-exporter",result="rolled_back"} 1' in text
    assert 'familia_docker_update_result{container="blackbox",result="updated"} 1' in text
    assert "familia_docker_update_run_success 0" in text
    assert "familia_docker_update_last_run_timestamp_seconds 1793000000" in text
    assert "familia_docker_update_run_success 1" in fdu.render_metrics({"a": "unchanged"}, 0)


@pytest.mark.parametrize("name", ["pimonitor.json", "photos.json"])
def test_shipped_configs_are_valid(name):
    with open(os.path.join(HERE, name)) as fh:
        fdu.validate_config(json.load(fh))


def test_compose_file_uses_floating_tags_for_every_configured_service():
    with open(os.path.join(HERE, "pimonitor.json")) as fh:
        services = json.load(fh)["projects"][0]["services"]
    with open(os.path.join(HERE, "..", "docker-compose.yml")) as fh:
        images = [line.split("image:", 1)[1].strip() for line in fh if line.strip().startswith("image:")]
    assert len(images) == len(services)
    assert all(img.endswith(":latest") for img in images), images


@pytest.mark.parametrize(
    "cfg",
    [{}, {"projects": [{"dir": "rel", "services": {}}]},
     {"projects": [{"dir": "/a", "services": {"x": {}}}]},
     {"projects": [{"dir": "/a", "services": {"x": {"health": [{"http": "h"}], "backup": {"type": "zip", "dir": "/b"}}}}]},
     {"projects": [{"dir": "/a", "services": {"x": {"health": [{"http": "h"}], "backup": {"type": "volume-tar", "dir": "/b"}}}}]},
     {"projects": [{"dir": "/a", "services": {"x": {"health": [{"http": "h"}], "backup": {"type": "mariadb-dump", "dir": "b"}}}}]}],
)
def test_invalid_configs(cfg):
    with pytest.raises(ValueError):
        fdu.validate_config(cfg)
