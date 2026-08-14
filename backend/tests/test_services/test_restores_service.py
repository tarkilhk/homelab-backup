from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.sidecar import write_backup_sidecar
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
