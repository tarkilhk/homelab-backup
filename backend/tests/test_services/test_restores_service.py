from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.sidecar import write_backup_sidecar
from app.core.target_locks import get_target_operation_lock
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService


class _RestorePlugin(BackupPlugin):
    restore_capability = "automatic"

    async def validate_config(self, config: dict[str, Any]) -> bool:
        return True

    async def test(self, config: dict[str, Any]) -> bool:
        return True

    async def backup(self, context: BackupContext) -> dict[str, Any]:
        raise NotImplementedError

    async def restore(self, context: RestoreContext) -> dict[str, Any]:
        return {"status": "success", "message": "Destination restored"}

    async def get_status(self, context: BackupContext) -> dict[str, Any]:
        return {"status": "ready"}


class _PartialRestorePlugin(_RestorePlugin):
    restore_capability = "partial"

    async def restore(self, context: RestoreContext) -> dict[str, Any]:
        return {"status": "partial", "message": "Restore accepted but not verified"}


def _target(db: Session, plugin_name: str = "test-plugin") -> Target:
    target = Target(
        name="Restore Destination",
        slug="restore-destination",
        plugin_name=plugin_name,
        plugin_config_json="{}",
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _artifact(tmp_path: Path, plugin: BackupPlugin, slug: str = "source") -> Path:
    directory = tmp_path / slug / "2026-08-14"
    directory.mkdir(parents=True)
    artifact = directory / "backup.bin"
    artifact.write_bytes(b"trusted backup")
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(job_id="1", target_id="1", config={}, metadata={"target_slug": slug}),
    )
    return artifact


def test_restore_validates_artifact_before_creating_run(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _RestorePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore_from_path(
        artifact_path=str(artifact),
        destination_target_id=target.id,
    )

    assert result.status == "success"
    assert result.target_runs[0].artifact_bytes == len(b"trusted backup")


def test_restore_uses_immutable_input_if_retention_removes_source(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_holder: dict[str, Path] = {}

    class RetentionRacePlugin(_RestorePlugin):
        async def restore(self, context: RestoreContext) -> dict[str, Any]:
            original = artifact_holder["path"]
            original.unlink()
            assert Path(context.artifact_path) != original
            assert Path(context.artifact_path).read_bytes() == b"trusted backup"
            return {"status": "success", "message": "Destination restored"}

    plugin = RetentionRacePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    artifact_holder["path"] = artifact
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore_from_path(
        artifact_path=str(artifact),
        destination_target_id=target.id,
    )

    assert result.status == "success"
    assert result.target_runs[0].artifact_path == str(artifact)


def test_manual_restore_plugin_is_not_reported_as_automatic_success(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _RestorePlugin("test-plugin")
    plugin.restore_capability = "manual"
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    with pytest.raises(ValueError, match="restore_not_automatic"):
        RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=target.id,
        )

    assert db_session.query(Run).count() == 0


def test_partial_restore_result_is_recorded_as_partial(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _PartialRestorePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore_from_path(
        artifact_path=str(artifact),
        destination_target_id=target.id,
    )

    assert result.status == "partial"
    assert result.target_runs[0].status == "partial"


def test_restore_overlap_is_recorded_as_skipped(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _RestorePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)
    lock = get_target_operation_lock(int(target.id))
    assert lock.acquire(blocking=False)
    try:
        result = RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=target.id,
        )
    finally:
        lock.release()

    assert result.status == "skipped"
    assert result.target_runs[0].status == "skipped"
    assert "already using this target" in result.message


def test_restore_rejects_tampered_recorded_artifact_before_plugin_runs(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _RestorePlugin("test-plugin")
    source = _target(db_session)
    source.name = "Restore Source"
    source.slug = "source"
    db_session.commit()
    destination = Target(
        name="Other Destination",
        slug="other-destination",
        plugin_name="test-plugin",
        plugin_config_json="{}",
    )
    db_session.add(destination)
    db_session.commit()
    artifact = _artifact(tmp_path, plugin, source.slug)
    run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    source_run = TargetRun(
        run_id=run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact.stat().st_size,
        sha256="0" * 64,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.commit()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    with pytest.raises(ValueError, match="hash does not match"):
        RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=destination.id,
            source_target_run_id=source_run.id,
        )

    assert db_session.query(Run).count() == 1
