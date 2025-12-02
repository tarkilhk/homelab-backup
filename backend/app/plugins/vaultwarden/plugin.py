from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
import logging

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
DEFAULT_DATA_PATH = "/data"
DEFAULT_CLI = "docker"


class VaultWardenPlugin(BackupPlugin):
    """Vaultwarden backup plugin executed via container tar/copy commands.

    Research summary (Vaultwarden wiki: Backing up your vault):
    - Simplest backup: `docker exec <container> /vaultwarden backup` and copy
      resulting archive, or tar the critical data directly.
    - We back up exactly: `/data/db.sqlite3`, `/data/config.json`, and
      `/data/attachments` (if present).
    - Restores are performed by extracting those files back into `/data` and
      restarting the service.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        container = config.get("container_name")
        if not container or not isinstance(container, str) or not container.strip():
            return False
        data_path = config.get("data_path", DEFAULT_DATA_PATH)
        if not isinstance(data_path, str) or not data_path.strip():
            return False
        docker_cli = config.get("docker_cli", DEFAULT_CLI)
        if docker_cli is not None and (not isinstance(docker_cli, str) or not docker_cli.strip()):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Verify the container is reachable and core data files exist."""
        if not await self.validate_config(config):
            return False
        cli = str(config.get("docker_cli", DEFAULT_CLI) or DEFAULT_CLI)
        container = str(config.get("container_name")).strip()
        data_path = str(config.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"
        check_cmd = [
            cli,
            "exec",
            container,
            "sh",
            "-c",
            f'test -x /vaultwarden && test -d "{data_path}" && test -f "{data_path}/db.sqlite3"',
        ]
        try:
            code, _, stderr = await self._run_cmd(check_cmd, timeout=20)
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.warning("vaultwarden_test_error | container=%s error=%s", container, exc)
            return False
        if code == 0:
            return True
        err_msg = stderr.decode(errors="ignore").strip()
        self._logger.warning(
            "vaultwarden_test_failed | container=%s code=%s err=%s", container, code, err_msg
        )
        return False

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        if not await self.validate_config(cfg):
            raise ValueError("vaultwarden config invalid; container_name and data_path required")
        cli = str(cfg.get("docker_cli", DEFAULT_CLI) or DEFAULT_CLI)
        container = str(cfg.get("container_name")).strip()
        data_path = str(cfg.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(BACKUP_BASE_PATH, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"vaultwarden-backup-{timestamp}.tar.gz")
        container_tmp = f"/tmp/vaultwarden-backup-{timestamp}.tar.gz"

        tar_script_parts = [
            f'cd "{data_path}" || exit 1',
            'if [ ! -f "db.sqlite3" ]; then echo "db.sqlite3 missing" >&2; exit 1; fi',
            'if [ ! -f "config.json" ]; then echo "config.json missing" >&2; exit 1; fi',
            'files="db.sqlite3 config.json"',
            'if [ -d "attachments" ]; then files="$files attachments"; fi',
        ]
        tar_script_parts.append(f'tar -czf "{container_tmp}" $files')
        tar_cmd = " ; ".join(tar_script_parts)

        self._logger.info(
            "vaultwarden_backup_start | job_id=%s target_id=%s container=%s data_path=%s artifact=%s",
            context.job_id,
            context.target_id,
            container,
            data_path,
            artifact_path,
        )

        code, _, stderr = await self._run_cmd(
            [cli, "exec", container, "sh", "-c", tar_cmd],
            timeout=120,
        )
        if code != 0:
            err_msg = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"vaultwarden backup failed to create archive: {err_msg}")

        code, _, stderr = await self._run_cmd(
            [cli, "cp", f"{container}:{container_tmp}", artifact_path],
            timeout=60,
        )
        if code != 0:
            err_msg = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"vaultwarden backup copy failed: {err_msg}")
        if not os.path.exists(artifact_path):
            raise RuntimeError("vaultwarden backup did not produce artifact")

        await self._run_cmd(
            [cli, "exec", container, "rm", "-f", container_tmp],
            timeout=20,
            swallow_errors=True,
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("vaultwarden config invalid; container_name and data_path required")
        cli = str(cfg.get("docker_cli", DEFAULT_CLI) or DEFAULT_CLI)
        container = str(cfg.get("container_name")).strip()
        data_path = str(cfg.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"

        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        container_tmp = f"/tmp/vaultwarden-restore-{timestamp}.tar.gz"

        self._logger.info(
            "vaultwarden_restore_start | job_id=%s source=%s dest=%s container=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            container,
            artifact_path,
        )

        code, _, stderr = await self._run_cmd(
            [cli, "cp", artifact_path, f"{container}:{container_tmp}"],
            timeout=60,
        )
        if code != 0:
            err_msg = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"vaultwarden restore copy failed: {err_msg}")

        restore_script = (
            f'mkdir -p "{data_path}" && cd "{data_path}" || exit 1; '
            f'tar -xzf "{container_tmp}" ; '
            f'rm -f "{container_tmp}"'
        )
        code, _, stderr = await self._run_cmd(
            [cli, "exec", container, "sh", "-c", restore_script],
            timeout=120,
        )
        if code != 0:
            err_msg = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"vaultwarden restore failed to extract archive: {err_msg}")

        return {
            "status": "success",
            "message": f"Restored vaultwarden data into {container}:{data_path}",
            "restored_path": f"{container}:{data_path}",
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}

    async def _run_cmd(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        swallow_errors: bool = False,
    ) -> Tuple[int, bytes, bytes]:
        """Run a subprocess command and capture output."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout, stderr
        except asyncio.TimeoutError:
            proc.kill()
            if swallow_errors:
                return 1, b"", b"timeout"
            raise
        except Exception:
            proc.kill()
            if swallow_errors:
                return 1, b"", b"error"
            raise
