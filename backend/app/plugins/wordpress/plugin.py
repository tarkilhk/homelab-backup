from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.subprocesses import run_process_with_timeout

CONNECT_TIMEOUT_SECONDS = 30.0
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0


class WordPressPlugin(BackupPlugin):
    restore_capability = "automatic"
    """WordPress backup via WP-CLI.

    WordPress documentation notes that a full backup requires both the
    database and the site files.
    WP-CLI provides a ``db export`` command to dump the database to a
    file for backups.
    This plugin uses WP-CLI to export the database and then archives the
    site directory along with the dump into ``tar.gz``.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        site_path = config.get("site_path")
        if not site_path or not isinstance(site_path, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: site_path is required")
        site_path = str(config.get("site_path"))
        wp_path = str(config.get("wp_path", "wp"))
        try:
            proc = await asyncio.create_subprocess_exec(
                wp_path,
                f"--path={site_path}",
                "--allow-root",
                "core",
                "is-installed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await run_process_with_timeout(
                proc,
                proc.communicate(),
                operation="WordPress installation check",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                detail = stderr.decode(errors="ignore").strip()
                raise RuntimeError(
                    "WordPress installation check failed "
                    f"(return code {proc.returncode}): {detail or 'unknown error'}"
                )
            return True
        except FileNotFoundError as exc:
            self._logger.warning("wordpress_test_error | error=%s", exc)
            raise FileNotFoundError(
                f"WP-CLI not found at '{wp_path}'. Please ensure WP-CLI is installed and in PATH."
            ) from exc
        except RuntimeError:
            raise
        except OSError as exc:
            self._logger.warning("wordpress_test_error | error=%s", exc)
            raise ConnectionError(f"Failed to execute WP-CLI command: {exc}") from exc

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        # Determine the durable backup base directory. Permission failures are
        # fatal; silently moving a backup to temporary storage is unsafe.
        cfg = getattr(context, "config", {}) or {}
        backup_base = str(
            cfg.get("backup_base_path") or os.environ.get("BACKUP_BASE_PATH") or "/backups"
        )

        site_path = str(cfg.get("site_path", ""))
        wp_path = str(cfg.get("wp_path", "wp"))
        if not site_path:
            raise ValueError("WordPress config must include site_path")

        self._logger.info(
            "wordpress_backup_start | job_id=%s target_id=%s site_path=%s artifact=%s",
            context.job_id,
            context.target_id,
            site_path,
            "<pending>",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "db.sql")
            proc = await asyncio.create_subprocess_exec(
                wp_path,
                f"--path={site_path}",
                "--allow-root",
                "db",
                "export",
                db_path,
                "--single-transaction",
                "--quick",
                "--skip-lock-tables",
                "--routines",
                "--events",
                "--triggers",
                "--hex-blob",
                "--set-gtid-purged=OFF",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await run_process_with_timeout(
                proc,
                proc.communicate(),
                operation="WordPress database export",
                timeout_seconds=BACKUP_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                self._logger.error(
                    "wordpress_db_export_failed | code=%s stdout=%s stderr=%s",
                    proc.returncode,
                    stdout.decode(errors="ignore"),
                    stderr.decode(errors="ignore"),
                )
                raise RuntimeError("wp db export failed")

            with create_backup_artifact(
                self,
                context,
                prefix="wordpress-backup",
                suffix=".tar.gz",
                backup_root=backup_base,
            ) as artifact:
                with tarfile.open(artifact.temporary_path, "w:gz") as tar:

                    def reject_links(member: tarfile.TarInfo) -> tarfile.TarInfo:
                        if member.issym() or member.islnk():
                            raise RuntimeError(
                                f"WordPress site contains unsupported link: {member.name}"
                            )
                        return member

                    tar.add(site_path, arcname="site", filter=reject_links)
                    tar.add(db_path, arcname="db.sql")

        artifact_path = str(artifact.final_path)

        self._logger.info(
            "wordpress_backup_success | job_id=%s target_id=%s artifact=%s",
            context.job_id,
            context.target_id,
            artifact_path,
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        site_path_value = str(cfg.get("site_path", ""))
        wp_path = str(cfg.get("wp_path", "wp"))

        if not site_path_value:
            raise ValueError("WordPress config must include site_path")
        site_path = Path(site_path_value)

        artifact_path = Path(context.artifact_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        self._logger.info(
            "wordpress_restore_start | job_id=%s source=%s dest=%s site_path=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            site_path,
            artifact_path,
        )

        with tempfile.TemporaryDirectory(prefix="wordpress-restore-") as tmpdir:
            extract_root = Path(tmpdir)
            try:
                with tarfile.open(artifact_path, "r:gz") as archive:
                    members = archive.getmembers()
                    for member in members:
                        member_path = Path(member.name)
                        if (
                            member_path.is_absolute()
                            or ".." in member_path.parts
                            or member.issym()
                            or member.islnk()
                            or not (member.isdir() or member.isreg())
                        ):
                            raise RuntimeError(
                                f"WordPress backup contains unsafe path: {member.name}"
                            )
                    archive.extractall(extract_root, members=members, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise RuntimeError("WordPress backup archive is invalid") from exc

            restored_site = extract_root / "site"
            db_file = extract_root / "db.sql"
            if not restored_site.is_dir() or not (restored_site / "wp-config.php").is_file():
                raise RuntimeError("WordPress backup is missing a valid site tree")
            if not db_file.is_file() or db_file.stat().st_size <= 0:
                raise RuntimeError("WordPress backup is missing db.sql")

            self._validate_restore_destination(site_path, artifact_path)
            rollback_site = extract_root / "rollback-site"
            rollback_database = extract_root / "rollback-db.sql"
            shutil.copytree(site_path, rollback_site, symlinks=True)
            await self._run_wp(
                wp_path,
                site_path,
                ("db", "export", str(rollback_database)),
            )
            if not rollback_database.is_file() or rollback_database.stat().st_size <= 0:
                raise RuntimeError("wp db export did not create a rollback database")

            try:
                self._replace_directory_contents(restored_site, site_path)
                # The destination's credentials/network identity must remain authoritative.
                shutil.copy2(rollback_site / "wp-config.php", site_path / "wp-config.php")
                await self._run_wp(wp_path, site_path, ("db", "reset", "--yes"))
                await self._run_wp(wp_path, site_path, ("db", "import", str(db_file)))
                await self._run_wp(wp_path, site_path, ("core", "is-installed"))
                await self._run_wp(wp_path, site_path, ("db", "check"))
            except Exception as restore_error:
                try:
                    self._replace_directory_contents(rollback_site, site_path)
                    await self._run_wp(wp_path, site_path, ("db", "reset", "--yes"))
                    await self._run_wp(
                        wp_path,
                        site_path,
                        ("db", "import", str(rollback_database)),
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "WordPress restore failed and rollback also failed: "
                        f"restore={restore_error}; rollback={rollback_error}"
                    ) from restore_error
                raise

        return {
            "status": "success",
            "artifact_path": str(artifact_path),
            "artifact_bytes": artifact_path.stat().st_size,
            "message": "WordPress files and database restored and verified",
        }

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}

    def _validate_restore_destination(self, site_path: Path, artifact_path: Path) -> None:
        if not site_path.is_absolute() or site_path.is_symlink():
            raise ValueError("unsafe WordPress destination: path must be an absolute directory")
        try:
            resolved = site_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("unsafe WordPress destination: path does not exist") from exc
        forbidden = {
            Path("/"),
            Path("/app"),
            Path("/backups"),
            Path("/data"),
            Path("/etc"),
            Path("/usr"),
            Path("/var"),
        }
        backup_root = Path(os.environ.get("BACKUP_BASE_PATH", "/backups")).resolve()
        artifact = artifact_path.resolve()
        overlaps_backup_root = (
            resolved == backup_root
            or resolved in backup_root.parents
            or backup_root in resolved.parents
        )
        contains_artifact = resolved == artifact or resolved in artifact.parents
        if (
            not resolved.is_dir()
            or resolved in forbidden
            or overlaps_backup_root
            or contains_artifact
        ):
            raise ValueError("unsafe WordPress destination")
        if not (resolved / "wp-config.php").is_file():
            raise ValueError("unsafe WordPress destination: existing wp-config.php is required")

    def _replace_directory_contents(self, source: Path, destination: Path) -> None:
        for child in destination.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in source.iterdir():
            target = destination / child.name
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, target, symlinks=True)
            elif child.is_symlink():
                target.symlink_to(os.readlink(child))
            else:
                shutil.copy2(child, target)

    async def _run_wp(
        self,
        wp_path: str,
        site_path: Path,
        arguments: tuple[str, ...],
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            wp_path,
            f"--path={site_path}",
            "--allow-root",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await run_process_with_timeout(
            process,
            process.communicate(),
            operation=f"WordPress {' '.join(arguments[:2])}",
            timeout_seconds=RESTORE_TIMEOUT_SECONDS,
        )
        if process.returncode != 0:
            detail = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"wp {' '.join(arguments[:2])} failed: {detail or 'unknown error'}")
