from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, cast

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.postgresql import (
    PostgreSQLTarget,
    authorize_postgresql_restore,
    probe_postgresql,
    publish_postgresql_artifact,
    restore_postgresql_archive,
    validate_postgresql_config,
    write_postgresql_archive,
)

BACKUP_BASE_PATH = "/backups"
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0
_LOG = logging.getLogger(__name__)


class PostgreSQLPlugin(BackupPlugin):
    """Strict PostgreSQL 16 named-database backup and restore adapter."""

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate one exact source or restore-destination configuration."""
        return validate_postgresql_config(config)

    async def test(self, config: Dict[str, Any]) -> bool:
        """Check connectivity through the same pinned PostgreSQL client toolchain."""
        if not await self.validate_config(config):
            raise ValueError("Invalid PostgreSQL source or restore-destination configuration")
        await probe_postgresql(PostgreSQLTarget.from_config(config))
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Publish one validated PostgreSQL 16 custom archive."""
        started = time.monotonic()
        _LOG.info(
            "postgresql_backup_start | job_id=%s target_id=%s",
            context.job_id,
            context.target_id,
        )
        try:
            async with asyncio.timeout(BACKUP_TIMEOUT_SECONDS):
                cfg = getattr(context, "config", {}) or {}
                if not await self.validate_config(cfg) or cfg.get("mode") != "source":
                    raise ValueError("PostgreSQL backup requires an exact source configuration")
                target = PostgreSQLTarget.from_config(cfg)
                identity = await probe_postgresql(target)

                with create_backup_artifact(
                    self,
                    context,
                    prefix="postgresql-dump",
                    suffix=".dump",
                    backup_root=BACKUP_BASE_PATH,
                ) as artifact:
                    evidence = await write_postgresql_archive(target, identity, artifact)
                    artifact.sidecar_metadata.update(
                        {
                            "postgresql_server_version": identity.server_version,
                            "postgresql_server_version_num": identity.server_version_num,
                            "server_encoding": identity.server_encoding,
                            "lc_collate": identity.lc_collate,
                            "lc_ctype": identity.lc_ctype,
                            "rls_table_count": len(
                                cast(list[object], identity.catalog["rls_tables"])
                            ),
                            "source_identity_sha256": evidence.source_identity_sha256,
                            "source_catalog_sha256": evidence.source_catalog_sha256,
                            "archive_catalog_sha256": evidence.archive_catalog_sha256,
                            "toc_sha256": evidence.toc_sha256,
                            "catalog_counts": dict(evidence.catalog_counts),
                            "validation": "postgresql-custom-v1",
                        }
                    )
                    await publish_postgresql_artifact(artifact, self, context)
                artifact_bytes = artifact.final_path.stat().st_size
        except TimeoutError as exc:
            _LOG.exception(
                "postgresql_backup_failed | job_id=%s target_id=%s duration_ms=%d",
                context.job_id,
                context.target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(
                f"PostgreSQL backup timed out after {BACKUP_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        except BaseException:
            _LOG.exception(
                "postgresql_backup_failed | job_id=%s target_id=%s duration_ms=%d",
                context.job_id,
                context.target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise

        _LOG.info(
            "postgresql_backup_success | job_id=%s target_id=%s artifact_path=%s "
            "bytes=%d duration_ms=%d",
            context.job_id,
            context.target_id,
            artifact.final_path,
            artifact_bytes,
            int((time.monotonic() - started) * 1000),
        )
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore one PostgreSQL custom archive transactionally."""
        started = time.monotonic()
        _LOG.info(
            "postgresql_restore_start | job_id=%s source=%s dest=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
        )
        try:
            async with asyncio.timeout(RESTORE_TIMEOUT_SECONDS):
                cfg = context.config or {}
                if not await self.validate_config(cfg) or cfg.get("mode") != "restore_destination":
                    raise ValueError(
                        "PostgreSQL restore requires an exact destination configuration"
                    )
                target = PostgreSQLTarget.from_config(cfg)
                authorize_postgresql_restore(
                    target,
                    source_identity=(context.metadata or {}).get("source_database_identity"),
                    source_target_id=str(context.source_target_id),
                    destination_target_id=str(context.destination_target_id),
                )
                artifact_path = context.artifact_path
                if not artifact_path:
                    raise FileNotFoundError("PostgreSQL restore artifact was not found")
                pre_restore_identity = await probe_postgresql(target)
                await restore_postgresql_archive(
                    target,
                    pre_restore_identity,
                    Path(artifact_path),
                    context.metadata or {},
                )
                artifact_bytes = int((context.metadata or {})["artifact_bytes"])
        except TimeoutError as exc:
            _LOG.exception(
                "postgresql_restore_failed | job_id=%s source=%s dest=%s duration_ms=%d",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(
                f"PostgreSQL restore timed out after {RESTORE_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        except BaseException:
            _LOG.exception(
                "postgresql_restore_failed | job_id=%s source=%s dest=%s duration_ms=%d",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise

        _LOG.info(
            "postgresql_restore_success | job_id=%s source=%s dest=%s bytes=%d duration_ms=%d",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            artifact_bytes,
            int((time.monotonic() - started) * 1000),
        )

        return {
            "status": "success",
            "artifact_path": artifact_path,
            "artifact_bytes": artifact_bytes,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return only identity evidence checked through the real PG16 probe."""
        config = context.config or {}
        if not await self.validate_config(config):
            return {
                "status": "error",
                "error": "Invalid PostgreSQL source or restore-destination configuration",
            }
        try:
            identity = await probe_postgresql(PostgreSQLTarget.from_config(config))
        except (ConnectionError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return {"status": "error", "error": str(exc)}
        except Exception:
            return {"status": "error", "error": "PostgreSQL status check failed"}
        return {
            "status": "ok",
            "server_version": identity.server_version,
            "database": identity.database,
            "server_encoding": identity.server_encoding,
            "lc_collate": identity.lc_collate,
            "lc_ctype": identity.lc_ctype,
        }
