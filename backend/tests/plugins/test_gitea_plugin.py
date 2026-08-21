from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import warnings
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import ANY, AsyncMock

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app
from app.plugins.gitea import plugin as gitea_module
from app.plugins.gitea.plugin import GiteaPlugin


def _docker_file_archive(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _gitea_dump_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr("gitea-db.sql", "CREATE TABLE marker (value TEXT);\n")
        archive.writestr("app.ini", "[database]\nDB_TYPE = sqlite3\n")
        archive.writestr("data/conf/app.ini", "[database]\nDB_TYPE = sqlite3\n")
        archive.writestr("repos/example/project.git/HEAD", "ref: refs/heads/main\n")
        archive.writestr("data/packages/example/blob", b"package-marker")
    return buffer.getvalue()


def _gitea_dump_with_member(member: zipfile.ZipInfo, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr("gitea-db.sql", "CREATE TABLE marker (value TEXT);\n")
        archive.writestr("app.ini", "[database]\nDB_TYPE = sqlite3\n")
        archive.writestr("data/conf/app.ini", "[database]\nDB_TYPE = sqlite3\n")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr(member, content)
    return buffer.getvalue()


def _health_exec_response(request: httpx.Request) -> httpx.Response | None:
    if request.method == "POST" and request.url.path.endswith("/exec"):
        if request.url.path == "/containers/restore-helper/exec":
            return None
        body = json.loads(request.content)
        assert body["Cmd"] == [
            "/usr/bin/timeout",
            "-s",
            "KILL",
            "10",
            "curl",
            "-fsS",
            "http://127.0.0.1:3000/api/healthz",
        ]
        return httpx.Response(201, json={"Id": "health-exec"})
    if request.method == "POST" and request.url.path == "/exec/health-exec/start":
        assert json.loads(request.content) == {"Detach": False, "Tty": False}
        return httpx.Response(200)
    if request.method == "GET" and request.url.path == "/exec/health-exec/json":
        return httpx.Response(200, json={"Running": False, "ExitCode": 0})
    return None


@pytest.mark.asyncio
async def test_gitea_discovery_schema_and_configuration_contract() -> None:
    plugin = get_plugin("gitea")

    assert isinstance(plugin, GiteaPlugin)
    assert plugin.restore_capability == "automatic"
    assert any(item["key"] == "gitea" for item in list_plugins())

    schema_path = get_plugin_schema_path("gitea")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["required"] == ["container_name", "allow_service_stop"]
    assert set(schema["properties"]) == {
        "container_name",
        "allow_service_stop",
        "timeout_seconds",
    }

    assert await plugin.validate_config(
        {
            "container_name": "gitea-local",
            "allow_service_stop": False,
            "timeout_seconds": 600,
        }
    )
    invalid_configs: tuple[dict[str, object], ...] = (
        {},
        {"container_name": "gitea-local"},
        {"container_name": "../gitea", "allow_service_stop": True},
        {"container_name": "gitea-local", "allow_service_stop": "yes"},
        {
            "container_name": "gitea-local",
            "allow_service_stop": True,
            "timeout_seconds": 5,
        },
        {
            "container_name": "gitea-local",
            "allow_service_stop": True,
            "timeout_seconds": 7200,
        },
    )
    for config in invalid_configs:
        assert not await plugin.validate_config(config)


@pytest.mark.anyio
async def test_gitea_api_lists_schema_and_tests_real_plugin(
    monkeypatch: Any,
    anyio_backend: tuple[str, dict[str, bool]],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        if request.method == "GET" and request.url.path == "/containers/gitea-local/json":
            return httpx.Response(
                200,
                json={
                    "Config": {"Image": "gitea/gitea:1.27.1"},
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/archive"):
            return httpx.Response(
                200,
                content=_docker_file_archive(
                    "app.ini",
                    b"[database]\nDB_TYPE = sqlite3\n",
                ),
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/gitea/schema")
        test_response = await client.post(
            "/api/v1/plugins/gitea/test",
            json={
                "container_name": "gitea-local",
                "allow_service_stop": False,
                "timeout_seconds": 600,
            },
        )

    assert plugins_response.status_code == 200
    assert any(item["key"] == "gitea" for item in plugins_response.json())
    assert schema_response.status_code == 200
    assert schema_response.json()["required"] == ["container_name", "allow_service_stop"]
    assert test_response.json() == {"ok": True}


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    """Use uvloop only for the route test on this dev VM's broken default loop."""
    return ("asyncio", {"use_uvloop": True})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_method", "failure", "error_type", "message"),
    [
        (
            "_inspect_container",
            FileNotFoundError("Gitea container was not found"),
            FileNotFoundError,
            "not found",
        ),
        (
            "_validate_sqlite_configuration",
            RuntimeError("Gitea must use SQLite"),
            RuntimeError,
            "SQLite",
        ),
        (
            "_run_health_check",
            RuntimeError("Gitea health endpoint check failed"),
            RuntimeError,
            "health endpoint",
        ),
    ],
)
async def test_connectivity_failures_are_specific(
    monkeypatch: Any,
    failure_method: str,
    failure: Exception,
    error_type: type[Exception],
    message: str,
) -> None:
    plugin = GiteaPlugin(name="gitea")
    details = {
        "Config": {"Image": "gitea/gitea:1.27.1"},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Mounts": [{"Destination": "/data", "RW": True}],
    }
    monkeypatch.setattr(plugin, "_inspect_container", AsyncMock(return_value=details))
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_run_health_check", AsyncMock())
    monkeypatch.setattr(plugin, failure_method, AsyncMock(side_effect=failure))
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(error_type, match=message):
        await plugin.test(
            {
                "container_name": "gitea-local",
                "allow_service_stop": False,
                "timeout_seconds": 600,
            }
        )


@pytest.mark.asyncio
async def test_test_inspects_exact_healthy_sqlite_container(monkeypatch: Any) -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/gitea-local/json":
            return httpx.Response(
                200,
                json={
                    "Config": {
                        "Image": "gitea/gitea:1.27.1",
                        "Env": ["SECRET=hidden"],
                    },
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        if request.method == "GET" and request.url.path == "/containers/gitea-local/archive":
            assert request.url.params["path"] == "/data/gitea/conf/app.ini"
            return httpx.Response(
                200,
                content=_docker_file_archive(
                    "app.ini",
                    b"APP_NAME = Gitea: Git with a cup of tea\n"
                    b"RUN_USER = git\n"
                    b"[database]\nDB_TYPE = sqlite3\nPATH = /data/gitea/gitea.db\n",
                ),
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    plugin = GiteaPlugin(name="gitea")

    assert await plugin.test(
        {
            "container_name": "gitea-local",
            "allow_service_stop": False,
            "timeout_seconds": 600,
        }
    )
    assert requests == [
        ("GET", "/containers/gitea-local/json"),
        ("GET", "/containers/gitea-local/archive"),
    ]


@pytest.mark.asyncio
async def test_backup_stops_dumps_streams_and_restarts_gitea(
    tmp_path: Path, monkeypatch: Any
) -> None:
    requests: list[tuple[str, str]] = []
    running = True
    expected_dump = _gitea_dump_zip()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/gitea-local/json":
            return httpx.Response(
                200,
                json={
                    "Config": {"Image": "gitea/gitea:1.27.1", "Env": []},
                    "State": {
                        "Running": running,
                        "Health": {"Status": "healthy" if running else "none"},
                    },
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        if request.method == "HEAD" and request.url.path.endswith("/archive"):
            return httpx.Response(
                200
                if request.url.params["path"] in {"/data/git/repositories", "/data/gitea/packages"}
                else 404
            )
        if request.method == "GET" and request.url.path.endswith("/archive"):
            path = request.url.params["path"]
            if path == "/data/gitea/conf/app.ini":
                return httpx.Response(
                    200,
                    content=_docker_file_archive("app.ini", b"[database]\nDB_TYPE = sqlite3\n"),
                )
            if path == "/tmp/gitea-dump.zip":
                return httpx.Response(
                    200,
                    content=_docker_file_archive("gitea-dump.zip", expected_dump),
                )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            running = False
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            body = json.loads(request.content)
            assert request.url.params["name"].startswith("homelab-backup-gitea-")
            assert body["Image"] == "gitea/gitea:1.27.1"
            assert body["User"] == "1000:1000"
            assert body["WorkingDir"] == "/tmp"
            assert body["Entrypoint"] == ["/usr/local/bin/gitea"]
            assert body["Cmd"] == [
                "--config",
                "/data/gitea/conf/app.ini",
                "dump",
                "--file",
                "/tmp/gitea-dump.zip",
                "--tempdir",
                "/tmp",
                "--skip-log",
            ]
            assert body["HostConfig"]["VolumesFrom"] == ["gitea-local:ro"]
            assert body["HostConfig"]["NetworkMode"] == "none"
            assert body["HostConfig"]["CapDrop"] == ["ALL"]
            assert body["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
            return httpx.Response(201, json={"Id": "helper-1"})
        if request.method == "POST" and request.url.path == "/containers/helper-1/start":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/helper-1/wait":
            assert request.url.params["condition"] == "not-running"
            return httpx.Response(200, json={"StatusCode": 0})
        if request.method == "DELETE" and request.url.path == "/containers/helper-1":
            assert dict(request.url.params) == {"force": "true", "v": "true"}
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/gitea-local/start":
            running = True
            return httpx.Response(204)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    plugin = GiteaPlugin(name="gitea")
    context = BackupContext(
        job_id="job-1",
        target_id="target-1",
        config={
            "container_name": "gitea-local",
            "allow_service_stop": True,
            "timeout_seconds": 600,
        },
        metadata={"target_slug": "gitea-local"},
    )

    result = await plugin.backup(context)

    artifact_path = Path(result["artifact_path"])
    assert artifact_path.is_file()
    assert artifact_path.read_bytes() == expected_dump
    sidecar = read_backup_sidecar(str(artifact_path))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "gitea"
    assert sidecar["target_slug"] == "gitea-local"
    assert running is True
    assert requests.index(("POST", "/containers/gitea-local/stop")) < requests.index(
        ("POST", "/containers/create")
    )
    assert requests.index(("DELETE", "/containers/helper-1")) < requests.index(
        ("POST", "/containers/gitea-local/start")
    )


@pytest.mark.asyncio
async def test_backup_restarts_when_stop_confirmation_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    inspection_count = 0
    restarted = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inspection_count, restarted
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        if request.method == "GET" and request.url.path == "/containers/gitea-local/json":
            inspection_count += 1
            if inspection_count == 1 or restarted:
                return httpx.Response(
                    200,
                    json={
                        "Config": {"Image": "gitea/gitea:1.27.1", "Env": []},
                        "State": {"Running": True, "Health": {"Status": "healthy"}},
                        "Mounts": [{"Destination": "/data", "RW": True}],
                    },
                )
            return httpx.Response(500)
        if request.method == "HEAD" and request.url.path.endswith("/archive"):
            return httpx.Response(404)
        if request.method == "GET" and request.url.path.endswith("/archive"):
            return httpx.Response(
                200,
                content=_docker_file_archive("app.ini", b"[database]\nDB_TYPE = sqlite3\n"),
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            return httpx.Response(204)
        if request.method == "POST" and request.url.path.endswith("/start"):
            restarted = True
            return httpx.Response(204)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    plugin = GiteaPlugin(name="gitea")

    with pytest.raises(RuntimeError, match="inspect"):
        await plugin.backup(
            BackupContext(
                job_id="job-1",
                target_id="target-1",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                metadata={"target_slug": "gitea-local"},
            )
        )

    assert restarted is True


@pytest.mark.asyncio
async def test_backup_restart_failure_publishes_no_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    plugin = GiteaPlugin(name="gitea")
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(
        plugin,
        "_inspect_container",
        AsyncMock(
            return_value={
                "Config": {"Image": "gitea/gitea:1.27.1"},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": [{"Destination": "/data", "RW": True}],
            }
        ),
    )
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_source_content_layout", AsyncMock(return_value=set()))
    monkeypatch.setattr(plugin, "_stop_container", AsyncMock())
    monkeypatch.setattr(plugin, "_confirm_stopped", AsyncMock())
    monkeypatch.setattr(plugin, "_create_dump_helper", AsyncMock(return_value="helper"))
    monkeypatch.setattr(plugin, "_start_container", AsyncMock())
    monkeypatch.setattr(plugin, "_wait_for_helper", AsyncMock())
    monkeypatch.setattr(plugin, "_remove_helper", AsyncMock())
    monkeypatch.setattr(
        plugin,
        "_wait_for_readiness",
        AsyncMock(side_effect=RuntimeError("not ready")),
    )

    async def write_dump(
        client: Any, helper_id: str, destination: Path, timeout_seconds: int
    ) -> None:
        destination.write_bytes(_gitea_dump_zip())

    monkeypatch.setattr(plugin, "_download_dump", write_dump)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(RuntimeError, match="could not safely restart"):
        await plugin.backup(
            BackupContext(
                job_id="job-1",
                target_id="target-1",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                metadata={"target_slug": "gitea-local"},
            )
        )

    assert list(tmp_path.rglob("*.zip")) == []
    assert list(tmp_path.rglob("*.meta.json")) == []


@pytest.mark.asyncio
async def test_backup_cleanup_failure_restarts_source_and_publishes_nothing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    plugin = GiteaPlugin(name="gitea")
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(
        plugin,
        "_inspect_container",
        AsyncMock(
            return_value={
                "Config": {"Image": "gitea/gitea:1.27.1"},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": [{"Destination": "/data", "RW": True}],
            }
        ),
    )
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_source_content_layout", AsyncMock(return_value=set()))
    monkeypatch.setattr(plugin, "_stop_container", AsyncMock())
    monkeypatch.setattr(plugin, "_confirm_stopped", AsyncMock())
    monkeypatch.setattr(plugin, "_create_dump_helper", AsyncMock(return_value="helper"))
    start_container = AsyncMock()
    monkeypatch.setattr(plugin, "_start_container", start_container)
    monkeypatch.setattr(plugin, "_wait_for_helper", AsyncMock())
    monkeypatch.setattr(plugin, "_wait_for_readiness", AsyncMock())

    async def fail_cleanup(
        client: Any,
        helper_id: str,
        *,
        strict: bool = False,
    ) -> None:
        if strict:
            raise RuntimeError("helper still present")

    async def write_dump(
        client: Any, helper_id: str, destination: Path, timeout_seconds: int
    ) -> None:
        destination.write_bytes(_gitea_dump_zip())

    monkeypatch.setattr(plugin, "_remove_helper", fail_cleanup)
    monkeypatch.setattr(plugin, "_download_dump", write_dump)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(RuntimeError, match="confirm helper cleanup"):
        await plugin.backup(
            BackupContext(
                job_id="job-1",
                target_id="target-1",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                metadata={"target_slug": "gitea-local"},
            )
        )

    start_container.assert_any_await(ANY, "gitea-local")
    assert list(tmp_path.rglob("*.zip")) == []
    assert list(tmp_path.rglob("*.meta.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["dump", "download", "validation"])
async def test_backup_failure_boundaries_restart_and_publish_nothing(
    tmp_path: Path,
    monkeypatch: Any,
    failure_stage: str,
) -> None:
    plugin = GiteaPlugin(name="gitea")
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(
        plugin,
        "_inspect_container",
        AsyncMock(
            return_value={
                "Config": {"Image": "gitea/gitea:1.27.1"},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": [{"Destination": "/data", "RW": True}],
            }
        ),
    )
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_source_content_layout", AsyncMock(return_value=set()))
    monkeypatch.setattr(plugin, "_stop_container", AsyncMock())
    monkeypatch.setattr(plugin, "_confirm_stopped", AsyncMock())
    monkeypatch.setattr(plugin, "_create_dump_helper", AsyncMock(return_value="helper"))
    start = AsyncMock()
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_wait_for_readiness", AsyncMock())
    monkeypatch.setattr(plugin, "_remove_helper", AsyncMock())
    if failure_stage == "dump":
        monkeypatch.setattr(
            plugin,
            "_wait_for_helper",
            AsyncMock(side_effect=RuntimeError("dump helper failed")),
        )
    else:
        monkeypatch.setattr(plugin, "_wait_for_helper", AsyncMock())

    async def download(
        client: Any, helper_id: str, destination: Path, timeout_seconds: int
    ) -> None:
        if failure_stage == "download":
            raise RuntimeError("dump download failed")
        if failure_stage == "validation":
            destination.write_bytes(b"invalid archive")

    monkeypatch.setattr(plugin, "_download_dump", download)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(RuntimeError):
        await plugin.backup(
            BackupContext(
                job_id=failure_stage,
                target_id="source",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                metadata={"target_slug": "gitea-local"},
            )
        )

    start.assert_any_await(ANY, "gitea-local")
    assert list(tmp_path.rglob("*.zip")) == []
    assert list(tmp_path.rglob("*.meta.json")) == []


@pytest.mark.asyncio
async def test_slow_dump_stream_hits_absolute_deadline_and_restarts_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            await asyncio.sleep(5)
            yield b"never reached"

    plugin = GiteaPlugin(name="gitea")
    monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(plugin, "validate_config", AsyncMock(return_value=True))
    monkeypatch.setattr(
        plugin,
        "_inspect_container",
        AsyncMock(
            return_value={
                "Config": {"Image": "gitea/gitea:1.27.1"},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": [{"Destination": "/data", "RW": True}],
            }
        ),
    )
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_source_content_layout", AsyncMock(return_value=set()))
    monkeypatch.setattr(plugin, "_stop_container", AsyncMock())
    monkeypatch.setattr(plugin, "_confirm_stopped", AsyncMock())
    monkeypatch.setattr(plugin, "_create_dump_helper", AsyncMock(return_value="helper"))
    start = AsyncMock()
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_wait_for_helper", AsyncMock())
    monkeypatch.setattr(plugin, "_wait_for_readiness", AsyncMock())
    monkeypatch.setattr(plugin, "_remove_helper", AsyncMock())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/helper/archive":
            return httpx.Response(200, stream=SlowStream())
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(TimeoutError):
        await plugin.backup(
            BackupContext(
                job_id="slow-stream",
                target_id="source",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 1,
                },
                metadata={"target_slug": "gitea-local"},
            )
        )

    start.assert_any_await(ANY, "gitea-local")
    assert list(tmp_path.rglob("*.zip")) == []
    assert list(tmp_path.rglob("*.meta.json")) == []


@pytest.mark.asyncio
async def test_restore_replaces_labeled_isolated_destination_and_proves_health(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact_path = tmp_path / "gitea-dump.zip"
    artifact_path.write_bytes(_gitea_dump_zip())
    running = True
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/gitea-restore/json":
            return httpx.Response(
                200,
                json={
                    "Config": {
                        "Image": "gitea/gitea:1.27.1",
                        "Env": [],
                        "Labels": {"asia.hollinger.homelab-backup.restore-destination": "true"},
                    },
                    "State": {
                        "Running": running,
                        "Health": {"Status": "healthy" if running else "none"},
                    },
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/archive"):
            path = request.url.params["path"]
            if path == "/data/gitea/conf/app.ini":
                return httpx.Response(
                    200,
                    content=_docker_file_archive("app.ini", b"[database]\nDB_TYPE = sqlite3\n"),
                )
            if path == "/data":
                return httpx.Response(
                    200,
                    content=_docker_file_archive("data/marker-before", b"before"),
                )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            running = False
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            body = json.loads(request.content)
            assert body["Image"] == "gitea/gitea:1.27.1"
            assert body["User"] == "0:0"
            assert body["Volumes"] == {"/tmp": {}}
            assert body["HostConfig"]["VolumesFrom"] == ["gitea-restore:rw"]
            assert body["HostConfig"]["NetworkMode"] == "none"
            assert body["HostConfig"]["CapDrop"] == ["ALL"]
            assert body["HostConfig"]["CapAdd"] == [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "SETGID",
                "SETUID",
            ]
            return httpx.Response(201, json={"Id": "restore-helper"})
        if request.method == "POST" and request.url.path == "/containers/restore-helper/start":
            return httpx.Response(204)
        if request.method == "PUT" and request.url.path.endswith("/archive"):
            assert request.url.params["path"] == "/tmp"
            assert b"gitea-dump.zip" in request.content
            return httpx.Response(200)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            body = json.loads(request.content)
            if request.url.path == "/containers/restore-helper/exec":
                assert body["Cmd"][0:3] == ["/usr/bin/timeout", "-s", "KILL"]
                script = body["Cmd"][-1]
                assert "sqlite3 -safe -bail -batch -noinit" in script
                assert "PRAGMA quick_check" in script
                assert "chown -R 1000:1000 /data" in script
                assert "su-exec git gitea" in script
                assert "admin regenerate hooks" in script
                assert "git --git-dir='{}' fsck --no-dangling" in script
                assert "find /data/gitea/packages -type f" in script
                return httpx.Response(201, json={"Id": "restore-exec"})
        if request.method == "POST" and request.url.path == "/exec/restore-exec/start":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/exec/restore-exec/json":
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        if request.method == "POST" and request.url.path == "/containers/gitea-restore/start":
            running = True
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path == "/containers/restore-helper":
            return httpx.Response(204)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    plugin = GiteaPlugin(name="gitea")

    result = await plugin.restore(
        RestoreContext(
            job_id="restore-1",
            source_target_id="source-1",
            destination_target_id="destination-1",
            config={
                "container_name": "gitea-restore",
                "allow_service_stop": True,
                "timeout_seconds": 600,
            },
            artifact_path=str(artifact_path),
            metadata={"destination_target_slug": "gitea-restore"},
        )
    )

    assert result["status"] == "success"
    assert running is True
    assert requests.index(("GET", "/containers/gitea-restore/archive")) < requests.index(
        ("PUT", "/containers/restore-helper/archive")
    )


@pytest.mark.asyncio
async def test_restore_refuses_unlabeled_destination_before_stopping(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact_path = tmp_path / "gitea-dump.zip"
    artifact_path.write_bytes(_gitea_dump_zip())
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/gitea-prod/json":
            return httpx.Response(
                200,
                json={
                    "Config": {
                        "Image": "gitea/gitea:1.27.1",
                        "Labels": {},
                    },
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(RuntimeError, match="explicitly labeled"):
        await GiteaPlugin(name="gitea").restore(
            RestoreContext(
                job_id="restore-1",
                source_target_id="source-1",
                destination_target_id="destination-1",
                config={
                    "container_name": "gitea-prod",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                artifact_path=str(artifact_path),
            )
        )

    assert requests == [("GET", "/containers/gitea-prod/json")]


@pytest.mark.parametrize(
    ("member", "message"),
    [
        (zipfile.ZipInfo("../outside"), "unsafe member"),
        (zipfile.ZipInfo("data/conf/app.ini"), "duplicate member"),
    ],
)
def test_restore_rejects_ambiguous_dump_members_before_docker_contact(
    tmp_path: Path, member: zipfile.ZipInfo, message: str
) -> None:
    artifact = tmp_path / "unsafe.zip"
    artifact.write_bytes(_gitea_dump_with_member(member, b"unsafe"))

    with pytest.raises(RuntimeError, match=message):
        GiteaPlugin(name="gitea")._validate_dump(artifact)


def test_restore_rejects_links_and_excessive_dump_members(tmp_path: Path, monkeypatch: Any) -> None:
    link = zipfile.ZipInfo("data/link")
    link.create_system = 3
    link.external_attr = (0o120777 << 16) | 0xA000
    link_artifact = tmp_path / "link.zip"
    link_artifact.write_bytes(_gitea_dump_with_member(link, b"../../outside"))

    with pytest.raises(RuntimeError, match="unsafe member"):
        GiteaPlugin(name="gitea")._validate_dump(link_artifact)

    monkeypatch.setattr(gitea_module, "_MAX_DUMP_MEMBERS", 3)
    oversized = tmp_path / "too-many.zip"
    oversized.write_bytes(_gitea_dump_zip())
    with pytest.raises(RuntimeError, match="too many members"):
        GiteaPlugin(name="gitea")._validate_dump(oversized)


@pytest.mark.parametrize("case", ["empty", "malformed", "incomplete"])
def test_restore_rejects_unusable_dump_shapes(tmp_path: Path, case: str) -> None:
    artifact = tmp_path / f"{case}.zip"
    if case == "malformed":
        artifact.write_bytes(b"not-a-zip")
    else:
        with zipfile.ZipFile(artifact, mode="w") as archive:
            if case == "incomplete":
                archive.writestr("app.ini", "[database]\nDB_TYPE = sqlite3\n")

    with pytest.raises(RuntimeError, match="valid ZIP|missing required"):
        GiteaPlugin(name="gitea")._validate_dump(artifact)


@pytest.mark.asyncio
async def test_health_start_timeout_still_confirms_exec_stopped() -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/containers/gitea-local/exec":
            return httpx.Response(201, json={"Id": "health-exec"})
        if request.method == "POST" and request.url.path == "/exec/health-exec/start":
            raise httpx.ReadTimeout("health deadline", request=request)
        if request.method == "GET" and request.url.path == "/exec/health-exec/json":
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://docker",
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            await GiteaPlugin(name="gitea")._run_health_check(client, "gitea-local")

    assert requests[-1] == ("GET", "/exec/health-exec/json")


@pytest.mark.asyncio
async def test_health_cancellation_waits_for_attached_exec_to_stop() -> None:
    start_seen = asyncio.Event()
    release_start = asyncio.Event()
    inspected = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inspected
        if request.method == "POST" and request.url.path == "/containers/gitea-local/exec":
            return httpx.Response(201, json={"Id": "health-exec"})
        if request.method == "POST" and request.url.path == "/exec/health-exec/start":
            start_seen.set()
            await release_start.wait()
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/exec/health-exec/json":
            inspected = True
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://docker",
    ) as client:
        task = asyncio.create_task(
            GiteaPlugin(name="gitea")._run_health_check(client, "gitea-local")
        )
        await start_seen.wait()
        task.cancel()
        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert inspected is True


@pytest.mark.asyncio
async def test_backup_cancellation_after_uncertain_stop_restarts_source(
    monkeypatch: Any,
) -> None:
    plugin = GiteaPlugin(name="gitea")
    details = {
        "Config": {"Image": "gitea/gitea:1.27.1"},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Mounts": [{"Destination": "/data", "RW": True}],
    }
    monkeypatch.setattr(plugin, "_inspect_container", AsyncMock(return_value=details))
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    monkeypatch.setattr(plugin, "_source_content_layout", AsyncMock(return_value=set()))
    monkeypatch.setattr(plugin, "_stop_container", AsyncMock(side_effect=asyncio.CancelledError()))
    start = AsyncMock()
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_wait_for_readiness", AsyncMock())
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(asyncio.CancelledError):
        await plugin.backup(
            BackupContext(
                job_id="cancelled",
                target_id="source",
                config={
                    "container_name": "gitea-local",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
            )
        )

    start.assert_awaited_once_with(ANY, "gitea-local")


def _mock_restore_transaction(
    plugin: GiteaPlugin,
    monkeypatch: Any,
    *,
    artifact_path: Path,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    details = {
        "Config": {
            "Image": "gitea/gitea:1.27.1",
            "Labels": {"asia.hollinger.homelab-backup.restore-destination": "true"},
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Mounts": [{"Destination": "/data", "RW": True}],
    }
    monkeypatch.setattr(plugin, "_inspect_container", AsyncMock(return_value=details))
    monkeypatch.setattr(plugin, "_validate_sqlite_configuration", AsyncMock())
    stop = AsyncMock()
    start = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(plugin, "_stop_container", stop)
    monkeypatch.setattr(plugin, "_confirm_stopped", AsyncMock())

    async def capture(
        client: Any,
        container_name: str,
        destination: Path,
        timeout_seconds: int,
    ) -> None:
        destination.write_bytes(b"rollback")

    monkeypatch.setattr(plugin, "_capture_data_archive", capture)
    monkeypatch.setattr(plugin, "_create_restore_helper", AsyncMock(return_value="helper"))
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_remove_helper", remove)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(
        plugin,
        "_docker_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )
    assert artifact_path.is_file()
    return stop, start, remove


def _restore_context(artifact_path: Path) -> RestoreContext:
    return RestoreContext(
        job_id="restore-boundary",
        source_target_id="source",
        destination_target_id="destination",
        config={
            "container_name": "gitea-restore",
            "allow_service_stop": True,
            "timeout_seconds": 600,
        },
        artifact_path=str(artifact_path),
    )


@pytest.mark.asyncio
async def test_cancellation_during_rollback_upload_leaves_destination_stopped(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifact = tmp_path / "gitea.zip"
    artifact.write_bytes(_gitea_dump_zip())
    plugin = GiteaPlugin(name="gitea")
    stop, start, remove = _mock_restore_transaction(
        plugin,
        monkeypatch,
        artifact_path=artifact,
    )
    upload = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(plugin, "_upload_file", upload)
    monkeypatch.setattr(
        plugin,
        "_apply_restore",
        AsyncMock(side_effect=RuntimeError("restore failed")),
    )

    with pytest.raises(asyncio.CancelledError):
        await plugin.restore(_restore_context(artifact))

    assert stop.await_count == 1
    start.assert_awaited_once_with(ANY, "helper")
    remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_execution_failure_leaves_destination_stopped(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifact = tmp_path / "gitea.zip"
    artifact.write_bytes(_gitea_dump_zip())
    plugin = GiteaPlugin(name="gitea")
    stop, start, _ = _mock_restore_transaction(
        plugin,
        monkeypatch,
        artifact_path=artifact,
    )
    monkeypatch.setattr(plugin, "_upload_file", AsyncMock())
    monkeypatch.setattr(
        plugin,
        "_apply_restore",
        AsyncMock(side_effect=RuntimeError("restore failed")),
    )
    monkeypatch.setattr(
        plugin,
        "_apply_rollback",
        AsyncMock(side_effect=RuntimeError("rollback failed")),
    )

    with pytest.raises(RuntimeError, match="destination was left stopped"):
        await plugin.restore(_restore_context(artifact))

    assert stop.await_count == 1
    start.assert_awaited_once_with(ANY, "helper")


@pytest.mark.asyncio
async def test_post_rollback_readiness_failure_re_stops_destination(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifact = tmp_path / "gitea.zip"
    artifact.write_bytes(_gitea_dump_zip())
    plugin = GiteaPlugin(name="gitea")
    stop, start, _ = _mock_restore_transaction(
        plugin,
        monkeypatch,
        artifact_path=artifact,
    )
    monkeypatch.setattr(plugin, "_upload_file", AsyncMock())
    monkeypatch.setattr(
        plugin,
        "_apply_restore",
        AsyncMock(side_effect=RuntimeError("restore failed")),
    )
    monkeypatch.setattr(plugin, "_apply_rollback", AsyncMock())
    monkeypatch.setattr(
        plugin,
        "_wait_for_readiness",
        AsyncMock(side_effect=RuntimeError("not ready")),
    )

    with pytest.raises(RuntimeError, match="destination was left stopped"):
        await plugin.restore(_restore_context(artifact))

    assert stop.await_count == 2
    assert start.await_args_list[-1].args[1] == "gitea-restore"


@pytest.mark.asyncio
async def test_same_container_operations_are_rejected_while_busy(monkeypatch: Any) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    plugin = GiteaPlugin(name="gitea")

    async def hold_backup(context: BackupContext) -> dict[str, str]:
        first_started.set()
        await release_first.wait()
        return {"artifact_path": "/backups/local.zip"}

    monkeypatch.setattr(plugin, "_backup_transaction", hold_backup)
    context = BackupContext(
        job_id="first",
        target_id="source",
        config={
            "container_name": "gitea-local",
            "allow_service_stop": True,
            "timeout_seconds": 600,
        },
    )
    first = asyncio.create_task(plugin.backup(context))
    await first_started.wait()
    try:
        with pytest.raises(RuntimeError, match="already has a backup or restore"):
            await GiteaPlugin(name="gitea").backup(context)
    finally:
        release_first.set()
    assert await first == {"artifact_path": "/backups/local.zip"}


@pytest.mark.asyncio
async def test_status_and_failure_logs_do_not_disclose_extra_config_secrets(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = GiteaPlugin(name="gitea")
    test_method = AsyncMock(return_value=True)
    monkeypatch.setattr(plugin, "test", test_method)
    context = BackupContext(
        job_id="status",
        target_id="source",
        config={
            "container_name": "gitea-local",
            "allow_service_stop": True,
            "timeout_seconds": 600,
        },
    )
    assert await plugin.get_status(context) == {"status": "ok"}

    secret = "never-log-this-token"
    context.config["password"] = secret
    monkeypatch.setattr(
        plugin,
        "_backup_transaction",
        AsyncMock(side_effect=RuntimeError("bounded failure")),
    )
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="bounded failure"):
        await plugin.backup(context)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_uncertain_restore_command_is_terminated_before_failure() -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/containers/helper/exec":
            return httpx.Response(201, json={"Id": "restore-exec"})
        if request.method == "POST" and request.url.path == "/exec/restore-exec/start":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/exec/restore-exec/json":
            raise httpx.ReadError("lost Docker response", request=request)
        if request.method == "DELETE" and request.url.path == "/containers/helper":
            return httpx.Response(204)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://docker",
    ) as client:
        with pytest.raises(RuntimeError, match="helper was terminated"):
            await GiteaPlugin(name="gitea")._run_helper_script(
                client,
                "helper",
                "true",
                600,
            )

    assert requests[-1] == ("DELETE", "/containers/helper")


@pytest.mark.asyncio
async def test_failed_restore_rolls_back_and_verifies_destination_health(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact_path = tmp_path / "gitea-dump.zip"
    artifact_path.write_bytes(_gitea_dump_zip())
    running = True
    uploaded_names: list[str] = []
    scripts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running
        health_response = _health_exec_response(request)
        if health_response is not None:
            return health_response
        if request.method == "GET" and request.url.path == "/containers/gitea-restore/json":
            return httpx.Response(
                200,
                json={
                    "Config": {
                        "Image": "gitea/gitea:1.27.1",
                        "Labels": {"asia.hollinger.homelab-backup.restore-destination": "true"},
                    },
                    "State": {
                        "Running": running,
                        "Health": {"Status": "healthy" if running else "none"},
                    },
                    "Mounts": [{"Destination": "/data", "RW": True}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/archive"):
            if request.url.params["path"] == "/data/gitea/conf/app.ini":
                return httpx.Response(
                    200,
                    content=_docker_file_archive("app.ini", b"[database]\nDB_TYPE = sqlite3\n"),
                )
            return httpx.Response(
                200,
                content=_docker_file_archive("data/gitea/gitea.db", b"old-db"),
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            running = False
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            return httpx.Response(201, json={"Id": "restore-helper"})
        if request.method == "POST" and request.url.path == "/containers/restore-helper/start":
            return httpx.Response(204)
        if request.method == "PUT" and request.url.path.endswith("/archive"):
            with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:") as archive:
                uploaded_names.extend(member.name for member in archive.getmembers())
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/containers/restore-helper/exec":
            script = json.loads(request.content)["Cmd"][-1]
            scripts.append(script)
            return httpx.Response(
                201,
                json={"Id": "restore-exec" if len(scripts) == 1 else "rollback-exec"},
            )
        if request.method == "POST" and request.url.path.startswith("/exec/"):
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/exec/restore-exec/json":
            return httpx.Response(200, json={"Running": False, "ExitCode": 7})
        if request.method == "GET" and request.url.path == "/exec/rollback-exec/json":
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        if request.method == "POST" and request.url.path == "/containers/gitea-restore/start":
            running = True
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path == "/containers/restore-helper":
            return httpx.Response(204)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        GiteaPlugin,
        "_docker_client",
        lambda self: httpx.AsyncClient(transport=transport, base_url="http://docker"),
    )

    with pytest.raises(RuntimeError, match="previous destination data was restored"):
        await GiteaPlugin(name="gitea").restore(
            RestoreContext(
                job_id="restore-1",
                source_target_id="source-1",
                destination_target_id="destination-1",
                config={
                    "container_name": "gitea-restore",
                    "allow_service_stop": True,
                    "timeout_seconds": 600,
                },
                artifact_path=str(artifact_path),
            )
        )

    assert uploaded_names == ["gitea-dump.zip", "gitea-data-before.tar"]
    assert "tar -xf /tmp/gitea-data-before.tar -C /" in scripts[1]
    assert running is True
