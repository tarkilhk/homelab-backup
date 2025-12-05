from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.core.plugins.base import RestoreContext
from app.core.plugins.loader import get_plugin
from app.services.runs import _assign_display_fields
from app.domain.enums import (
    RunOperation,
    RunStatus,
    TargetRunOperation,
    TargetRunStatus,
)
from app.models import (
    Run as RunModel,
    Job as JobModel,
    Target as TargetModel,
    TargetRun as TargetRunModel,
)


class RestoreService:
    """Business logic for manual restore operations."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_source_target_run(self, target_run_id: int) -> Optional[TargetRunModel]:
        """Return target run with eager-loaded run and target."""
        return (
            self.db.query(TargetRunModel)
            .options(joinedload(TargetRunModel.run), joinedload(TargetRunModel.target))
            .filter(TargetRunModel.id == target_run_id)
            .first()
        )

    def restore_from_path(
        self,
        *,
        artifact_path: str,
        destination_target_id: int,
        source_target_run_id: Optional[int] = None,
        triggered_by: str = "manual_restore",
    ) -> RunModel:
        """Restore a backup artifact from a file path to a destination target.
        
        Args:
            artifact_path: Path to the backup artifact file
            destination_target_id: ID of the target to restore to
            source_target_run_id: Optional source target run ID for metadata
            triggered_by: Audit string for who initiated the restore
            
        Returns:
            RunModel representing the restore operation
        """
        # Validate artifact path exists
        if not artifact_path:
            raise ValueError("artifact_path_missing")
        if not os.path.exists(artifact_path):
            raise ValueError("artifact_path_not_found")

        # Get destination target
        dest_target = (
            self.db.query(TargetModel)
            .filter(TargetModel.id == destination_target_id)
            .options(joinedload(TargetModel.target_tags))
            .first()
        )
        if dest_target is None:
            raise KeyError("destination_target_not_found")

        # Get source target info if source_target_run_id is provided
        source_target = None
        source_run = None
        source_tr = None
        job_id_for_restore: Optional[int] = None
        
        if source_target_run_id is not None:
            source_tr = self.get_source_target_run(source_target_run_id)
            if source_tr is None:
                raise KeyError("source_target_run_not_found")
            source_run = source_tr.run
            if source_run is None:
                raise ValueError("source_run_not_found")
            source_target = source_tr.target
            if source_target is None:
                raise ValueError("source_target_not_found")
            
            # Use source run's job_id for backward compatibility
            job_id_for_restore = source_run.job_id
            
            # Validate plugin match if we have source target
            source_plugin = source_target.plugin_name
            dest_plugin = dest_target.plugin_name
            if not source_plugin or not dest_plugin:
                raise ValueError("plugin_missing")
            if source_plugin != dest_plugin:
                raise ValueError("plugin_mismatch")
        else:
            # For file-based restores, we need to determine plugin from destination
            dest_plugin = dest_target.plugin_name
            if not dest_plugin:
                raise ValueError("plugin_missing")
            # job_id_for_restore remains None for file-based restores

        try:
            plugin = get_plugin(dest_target.plugin_name)
        except KeyError as exc:
            raise ValueError("plugin_not_registered") from exc

        started_at = datetime.now(timezone.utc)
        run = RunModel(
            job_id=job_id_for_restore,  # None for file-based restores, source job_id for UI restores
            started_at=started_at,
            status=RunStatus.RUNNING.value,
            operation=RunOperation.RESTORE.value,
            message=f"Restore started (triggered_by={triggered_by})",
            logs_text=f"Restore started at {started_at.isoformat()} (triggered_by={triggered_by})",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        # Get artifact metadata from source if available, otherwise compute from file
        artifact_bytes = None
        artifact_sha256 = None
        if source_tr:
            artifact_bytes = source_tr.artifact_bytes
            artifact_sha256 = source_tr.sha256
        else:
            # Compute metadata from file
            try:
                artifact_bytes = int(os.path.getsize(artifact_path))
                digest = hashlib.sha256()
                with open(artifact_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                artifact_sha256 = digest.hexdigest()
            except Exception:
                pass  # Best effort

        target_run = TargetRunModel(
            run_id=run.id,
            target_id=destination_target_id,
            started_at=started_at,
            status=TargetRunStatus.RUNNING.value,
            operation=TargetRunOperation.RESTORE.value,
            message=(
                f"Restore started from target_run #{source_target_run_id}"
                if source_target_run_id
                else f"Restore started from file {artifact_path}"
            ),
            artifact_path=artifact_path,
            artifact_bytes=artifact_bytes,
            sha256=artifact_sha256,
            logs_text=(
                f"Restore started at {started_at.isoformat()} "
                f"using artifact {artifact_path}"
                + (f" from target_run #{source_target_run_id}" if source_target_run_id else "")
            ),
        )
        self.db.add(target_run)
        self.db.commit()
        self.db.refresh(target_run)

        dest_config: dict = {}
        if dest_target.plugin_config_json:
            try:
                dest_config = json.loads(dest_target.plugin_config_json)
            except Exception:
                dest_config = {}

        metadata = {
            "destination_target_slug": dest_target.slug,
        }
        if source_target_run_id:
            metadata["source_target_run_id"] = source_target_run_id
        if source_run:
            metadata["source_run_id"] = source_run.id
        if source_target:
            metadata["source_target_id"] = source_target.id
            metadata["source_target_slug"] = source_target.slug
        if artifact_bytes:
            metadata["artifact_bytes"] = artifact_bytes
        if artifact_sha256:
            metadata["artifact_sha256"] = artifact_sha256
        if source_tr:
            metadata["backup_started_at"] = source_tr.started_at.isoformat() if source_tr.started_at else None
            metadata["backup_finished_at"] = source_tr.finished_at.isoformat() if source_tr.finished_at else None

        # Use run.id as job_id placeholder for RestoreContext (only used for logging)
        context_job_id = str(run.id) if job_id_for_restore is None else str(job_id_for_restore)
        context = RestoreContext(
            job_id=context_job_id,
            source_target_id=str(source_target.id) if source_target else str(destination_target_id),
            destination_target_id=str(dest_target.id),
            config=dest_config,
            artifact_path=artifact_path,
            metadata=metadata,
        )

        result_container: dict[str, object] = {}

        def _runner() -> None:
            try:
                result_container["result"] = asyncio.run(plugin.restore(context))
            except Exception as exc:  # noqa: BLE001
                result_container["error"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()

        finished_at = datetime.now(timezone.utc)
        try:
            if "error" in result_container:
                raise result_container["error"]  # type: ignore[misc]

            plugin_result = result_container.get("result")
            status_value = TargetRunStatus.SUCCESS.value
            message_value: Optional[str] = None
            if isinstance(plugin_result, dict):
                status_candidate = plugin_result.get("status")
                if isinstance(status_candidate, str):
                    status_value = status_candidate
                message_candidate = plugin_result.get("message")
                if isinstance(message_candidate, str):
                    message_value = message_candidate
                restored_path = plugin_result.get("restored_path")
                if isinstance(restored_path, str) and restored_path:
                    target_run.artifact_path = restored_path
                restored_bytes = plugin_result.get("artifact_bytes")
                if isinstance(restored_bytes, int):
                    target_run.artifact_bytes = restored_bytes
                restored_sha = plugin_result.get("sha256")
                if isinstance(restored_sha, str):
                    target_run.sha256 = restored_sha

            if status_value not in {TargetRunStatus.SUCCESS.value, TargetRunStatus.FAILED.value}:
                status_value = TargetRunStatus.SUCCESS.value

            target_run.finished_at = finished_at
            target_run.status = status_value
            target_run.message = message_value or "Restore completed successfully"
            target_run.logs_text = (target_run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"
            self.db.add(target_run)

            run.finished_at = finished_at
            run.status = RunStatus.SUCCESS.value if status_value == TargetRunStatus.SUCCESS.value else RunStatus.FAILED.value
            run.message = target_run.message
            run.logs_text = (run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()} with status={run.status}"
            self.db.add(run)
            self.db.commit()
        except Exception as exc:  # noqa: BLE001
            target_run.finished_at = finished_at
            target_run.status = TargetRunStatus.FAILED.value
            target_run.message = f"Restore failed: {exc}"
            target_run.logs_text = (target_run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"
            self.db.add(target_run)

            run.finished_at = finished_at
            run.status = RunStatus.FAILED.value
            run.message = f"Restore failed: {exc}"
            run.logs_text = (run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"
            self.db.add(run)
            self.db.commit()
            raise

        # Populate artifact metadata if missing and file exists
        try:
            if target_run.artifact_path and os.path.exists(target_run.artifact_path):
                if target_run.artifact_bytes is None:
                    target_run.artifact_bytes = int(os.path.getsize(target_run.artifact_path))
                if not target_run.sha256:
                    digest = hashlib.sha256()
                    with open(target_run.artifact_path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            digest.update(chunk)
                    target_run.sha256 = digest.hexdigest()
                self.db.add(target_run)
                self.db.commit()
        except Exception:
            # Best-effort; metadata issues should not fail restore
            self.db.rollback()

        result_run = (
            self.db.query(RunModel)
            .options(
                joinedload(RunModel.job).joinedload(JobModel.tag),
                joinedload(RunModel.target_runs).joinedload(TargetRunModel.target),
            )
            .filter(RunModel.id == run.id)
            .first()
        ) or run
        _assign_display_fields(result_run)
        return result_run

    def restore(
        self,
        *,
        source_target_run_id: int,
        destination_target_id: int,
        triggered_by: str = "manual_restore",
    ) -> RunModel:
        """Restore a backup artifact captured by `source_target_run_id` to another target.
        
        This method is a convenience wrapper that gets the artifact_path from the source
        target run and calls restore_from_path().
        """
        source_tr = self.get_source_target_run(source_target_run_id)
        if source_tr is None:
            raise KeyError("source_target_run_not_found")
        
        artifact_path = source_tr.artifact_path
        if not artifact_path:
            raise ValueError("artifact_path_missing")
        
        # Call restore_from_path with the artifact_path and source_target_run_id
        return self.restore_from_path(
            artifact_path=artifact_path,
            destination_target_id=destination_target_id,
            source_target_run_id=source_target_run_id,
            triggered_by=triggered_by,
        )
