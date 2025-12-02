import asyncio
import os
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.vaultwarden import VaultWardenPlugin


class DummyProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> Tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_validate_config() -> None:
    plugin = VaultWardenPlugin(name="vaultwarden")
    assert await plugin.validate_config(
        {"container_name": "vaultwarden", "docker_cli": "docker", "data_path": "/data"}
    )
    assert await plugin.validate_config({"container_name": "vw"}) is True
    assert await plugin.validate_config({"container_name": ""}) is False
    assert await plugin.validate_config({"container_name": 123}) is False  # type: ignore[arg-type]
    assert await plugin.validate_config({"container_name": "vw", "data_path": ""}) is False


@pytest.mark.asyncio
async def test_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Tuple[Any, ...]] = []

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        calls.append(cmd)
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = VaultWardenPlugin(name="vaultwarden")
    ok = await plugin.test({"container_name": "vw", "docker_cli": "docker"})
    assert ok is True
    # Ensure the command targeted the configured container
    assert any("vw" in c for call in calls for c in call)


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Tuple[Any, ...]] = []

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        calls.append(cmd)
        # Simulate docker cp writing the artifact to host
        if len(cmd) >= 2 and cmd[1] == "cp":
            dest = cmd[-1]
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"vault-backup")
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = BackupContext(
        job_id="job-1",
        target_id="target-1",
        config={"container_name": "vw"},
        metadata={"target_slug": "vw-slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert Path(artifact_path).exists()
    assert Path(artifact_path).read_bytes() == b"vault-backup"
    # Ensure tar and cp commands were invoked
    assert any("tar -czf" in call[-1] and "config.json" in call[-1] for call in calls if call and call[0] == "docker")
    assert any(call[1] == "cp" for call in calls)


@pytest.mark.asyncio
async def test_restore_extracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifact = tmp_path / "vaultwarden-backup.tar.gz"
    artifact.write_bytes(b"vault-backup")

    calls: List[Tuple[Any, ...]] = []

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        calls.append(cmd)
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = RestoreContext(
        job_id="job-1",
        source_target_id="src",
        destination_target_id="dest",
        config={"container_name": "vw"},
        artifact_path=str(artifact),
    )
    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    assert any(call[1] == "cp" for call in calls)
    assert any("tar -xzf" in call[-1] for call in calls if call and call[0] == "docker")
