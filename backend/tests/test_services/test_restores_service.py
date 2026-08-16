from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
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


class _CancelledRestorePlugin(_RestorePlugin):
    async def restore(self, context: RestoreContext) -> dict[str, Any]:
        raise asyncio.CancelledError


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


def test_cancelled_restore_is_audited_as_failed_before_propagation(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _CancelledRestorePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    with pytest.raises(asyncio.CancelledError):
        RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=target.id,
        )

    run = db_session.query(Run).one()
    target_run = db_session.query(TargetRun).one()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert target_run.status == "failed"
    assert target_run.finished_at is not None


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


def test_restore_stages_verified_artifact_and_supplies_source_database_identity(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class ProvenancePlugin(_RestorePlugin):
        async def restore(self, context: RestoreContext) -> dict[str, Any]:
            observed["artifact_path"] = context.artifact_path
            observed["metadata"] = context.metadata
            observed["artifact_inode"] = Path(context.artifact_path).stat().st_ino
            assert Path(context.artifact_path).read_bytes() == b"trusted backup"
            return {"status": "success", "message": "Destination restored"}

    plugin = ProvenancePlugin("test-plugin")
    source_config = {
        "host": "source-db.internal",
        "port": 5432,
        "database": "source_database",
        "user": "source_backup",
        "password": "not-forwarded",
    }
    source = _target(db_session)
    source.name = "Restore Source"
    source.slug = "source"
    source.plugin_config_json = json.dumps(source_config)
    destination = Target(
        name="Other Destination",
        slug="other-destination",
        plugin_name="test-plugin",
        plugin_config_json="{}",
    )
    db_session.add(destination)
    db_session.commit()
    artifact = _artifact(tmp_path, plugin, source.slug)
    source_run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.commit()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact.stat().st_size,
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        source_identity_json=json.dumps(
            {
                "host": "source-db.internal",
                "port": 5432,
                "database": "source_database",
                "user": "source_backup",
            }
        ),
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(source_target_run)
    db_session.commit()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination.id,
    )

    assert result.status == "success"
    staged_path = Path(str(observed["artifact_path"]))
    assert staged_path != artifact
    assert observed["artifact_inode"] != artifact.stat().st_ino
    assert not staged_path.exists()
    metadata = observed["metadata"]
    assert metadata["source_database_identity"] == {
        "host": "source-db.internal",
        "port": 5432,
        "database": "source_database",
        "user": "source_backup",
    }
    assert "password" not in metadata["source_database_identity"]


@pytest.mark.parametrize("replacement", ("fifo", "growing-file"))
def test_restore_refuses_artifact_replaced_after_validation(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    plugin_called = False

    class GuardedPlugin(_RestorePlugin):
        async def restore(self, context: RestoreContext) -> dict[str, Any]:
            nonlocal plugin_called
            plugin_called = True
            return {"status": "success", "message": "must not run"}

    plugin = GuardedPlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)
    from app.services import restores as restores_module

    real_validate = restores_module.validate_restore_artifact

    def replace_after_validation(*args: Any, **kwargs: Any) -> Any:
        validated = real_validate(*args, **kwargs)
        if replacement == "fifo":
            artifact.unlink()
            os.mkfifo(artifact)
        else:
            with artifact.open("ab") as artifact_file:
                artifact_file.write(b"unexpected growth")
        return validated

    monkeypatch.setattr(restores_module, "validate_restore_artifact", replace_after_validation)

    with pytest.raises(ValueError, match="changed while preparing"):
        RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=target.id,
        )

    assert plugin_called is False


def test_restore_records_critical_cleanup_warning_without_reclassifying_commit(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = _RestorePlugin("test-plugin")
    target = _target(db_session)
    artifact = _artifact(tmp_path, plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)
    staging_directories: list[Path] = []
    real_rmtree = shutil.rmtree

    def fail_cleanup(path: str | Path) -> None:
        staging_directories.append(Path(path))
        raise PermissionError("synthetic staging cleanup refusal")

    monkeypatch.setattr("app.services.restores.shutil.rmtree", fail_cleanup)
    try:
        result = RestoreService(db_session).restore_from_path(
            artifact_path=str(artifact),
            destination_target_id=target.id,
        )
    finally:
        for directory in staging_directories:
            real_rmtree(directory)

    assert result.status == "success"
    assert "CRITICAL: private staging cleanup was not confirmed" in result.logs_text
    assert "restore_staging_cleanup_failed" in caplog.text
