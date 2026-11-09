import email.message
import io
import json
import urllib.error

import pytest

import familia_host_metrics as fhm

APT_SIM = """\
NOTE: This is only a simulation!
Reading package lists...
Inst libssl3 [3.0.14-1~deb12u1] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [arm64])
Inst openssl [3.0.14-1~deb12u1] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [arm64])
Inst raspi-firmware [1:1.20240529] (1:1.20241008 Raspberry Pi Foundation:bookworm [all])
Inst newpkg (1.0 Debian:12.7/stable [arm64])
Conf libssl3 (3.0.15-1~deb12u1 Debian-Security:12/stable-security [arm64])
"""


def test_parse_apt_simulation():
    assert fhm.parse_apt_simulation(APT_SIM) == {
        ("Debian-Security:12/stable-security", "arm64"): 2,
        ("Raspberry Pi Foundation:bookworm", "all"): 1,
        ("Debian:12.7/stable", "arm64"): 1,
    }
    assert fhm.parse_apt_simulation("Reading package lists...\n") == {}


def test_count_autoremove():
    assert fhm.count_autoremove("Remv linux-image-6.1.0 [6.1.0]\nRemv foo [1]\nOther\n") == 2


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("grafana/grafana:13.2.5", ("registry-1.docker.io", "grafana/grafana", "13.2.5")),
        ("prom/prometheus", ("registry-1.docker.io", "prom/prometheus", "latest")),
        ("mariadb:11", ("registry-1.docker.io", "library/mariadb", "11")),
        ("quay.io/prometheus/node-exporter:latest", ("quay.io", "prometheus/node-exporter", "latest")),
        ("ghcr.io/org/app:v1@sha256:abc", ("ghcr.io", "org/app", "v1")),
        ("localhost:5000/app", ("localhost:5000", "app", "latest")),
        ("arm64v8/mariadb:10.11", ("registry-1.docker.io", "arm64v8/mariadb", "10.11")),
    ],
)
def test_parse_image_ref(ref, expected):
    assert fhm.parse_image_ref(ref) == expected


class _Resp(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v


def test_remote_digest_with_anonymous_token():
    calls = []

    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url, dict(req.header_items())))
        if req.full_url.startswith("https://auth.docker.io/token"):
            return _Resp(json.dumps({"token": "tkn"}).encode())
        if "Authorization" not in dict(req.header_items()):
            hdrs = email.message.Message()
            hdrs["WWW-Authenticate"] = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:grafana/grafana:pull"'
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs, None)
        return _Resp(headers={"Docker-Content-Digest": "sha256:remote"})

    assert fhm.remote_digest("registry-1.docker.io", "grafana/grafana", "13.2.5", opener) == "sha256:remote"
    assert [c[0] for c in calls] == ["HEAD", "GET", "HEAD"]  # HEAD only against the registry: no pulls
    assert "scope=repository%3Agrafana%2Fgrafana%3Apull" in calls[1][1]
    assert calls[2][2]["Authorization"] == "Bearer tkn"


def test_resolve_container_ref():
    tags = ["prom/prometheus:latest"]
    assert fhm.resolve_container_ref("sha256:75aa74e50f1e", tags) == "prom/prometheus:latest"
    assert fhm.resolve_container_ref("75aa74e50f1e9ec9", tags) == "prom/prometheus:latest"
    assert fhm.resolve_container_ref("prom/alertmanager@sha256:39d8", ["prom/alertmanager:v0.34.2"]) == "prom/alertmanager:v0.34.2"
    assert fhm.resolve_container_ref("grafana/grafana:13.2.5", tags) == "grafana/grafana:13.2.5"
    assert fhm.resolve_container_ref("sha256:abc", []) == "sha256:abc"


def test_image_update_status():
    digests = ["grafana/grafana@sha256:aaa"]
    assert fhm.image_update_status("grafana/grafana:13.2.5", digests, lambda *a: "sha256:aaa") == (0, "ok")
    assert fhm.image_update_status("grafana/grafana:13.2.5", digests, lambda *a: "sha256:bbb") == (1, "ok")
    assert fhm.image_update_status("local/app", [], lambda *a: "x")[0] is None

    def boom(*a):
        raise TimeoutError("slow")

    status, reason = fhm.image_update_status("grafana/grafana:1", digests, boom)
    assert status is None and "TimeoutError" in reason


def test_collect_docker_metrics_render():
    m = fhm.Metrics()
    containers = [
        {"container": "grafana", "ref": "grafana/grafana:13.2.5", "repo_digests": ["grafana/grafana@sha256:old"],
         "created": "2026-10-20T08:00:00.123Z"},
        {"container": "db", "ref": "mariadb:11", "repo_digests": ["mariadb@sha256:same"], "created": ""},
        {"container": "local", "ref": "myapp", "repo_digests": [], "created": ""},
    ]
    fhm.collect_docker(m, digest_lookup=lambda r, n, t: "sha256:same", containers=containers)
    text = m.render()
    assert 'familia_docker_image_update_available{container="grafana",image="grafana/grafana",tag="13.2.5"} 1' in text
    assert 'familia_docker_image_update_available{container="db",image="mariadb",tag="11"} 0' in text
    assert 'familia_docker_image_check_error{container="local",image="myapp",reason="no local repo digest (image built locally?)",tag="latest"} 1' in text
    assert "familia_docker_image_created_timestamp_seconds{" in text
    assert text.count("# TYPE familia_docker_image_update_available gauge") == 1


def test_parse_pihole_versions():
    out = "Core version is v6.0.6 (Latest: v6.5)\nWeb version is v6.1 (Latest: v6.6.1)\nFTL version is v6.1 (Latest: v6.1)\n"
    assert fhm.parse_pihole_versions(out) == [
        ("core", "v6.0.6", "v6.5"), ("web", "v6.1", "v6.6.1"), ("ftl", "v6.1", "v6.1")]


def test_label_escaping_and_atomic_write(tmp_path):
    m = fhm.Metrics()
    m.declare("x", "gauge", "h")
    m.add("x", 1, reason='bad "quote"\nline')
    path = fhm.write_atomically(str(tmp_path), m.render())
    content = open(path).read()
    assert 'reason="bad \\"quote\\"\\nline"' in content
    assert [p.name for p in tmp_path.iterdir()] == ["familia_host.prom"]


def test_run_retries_then_reports_stderr():
    import sys
    flaky = ["/bin/sh", "-c", 'echo "E: lists are being updated" >&2; exit 100']
    with pytest.raises(fhm.CommandError, match="exited 100: E: lists are being updated"):
        fhm.run(flaky, retries=1, retry_delay=0)
    assert fhm.run([sys.executable, "-c", "print('ok')"]).strip() == "ok"


def test_main_marks_failed_section(tmp_path, monkeypatch):
    def broken(_m):
        raise RuntimeError("no apt here")

    monkeypatch.setattr(fhm, "collect_apt", broken)
    rc = fhm.main(["--textfile-dir", str(tmp_path), "--apt"])
    text = (tmp_path / "familia_host.prom").read_text()
    assert rc == 1
    assert 'familia_host_metrics_section_success{section="apt"} 0' in text
    assert "familia_host_metrics_last_run_timestamp_seconds" in text
