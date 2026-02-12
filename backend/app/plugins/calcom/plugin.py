from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.sidecar import write_backup_sidecar


class CalcomPlugin(BackupPlugin):
    """Backup Cal.com by dumping its PostgreSQL database.

    Research notes:
    - Cal.com self-hosting runs on PostgreSQL (`DATABASE_URL`) and commonly
      uses Prisma, where `DATABASE_DIRECT_URL` may also be present for direct
      database access in pooled deployments.
    - `pg_dump` is the standard utility for logical SQL backups.
    - `pg_isready` only checks server readiness and does not reliably validate
      credentials/database access, so plugin `test()` uses `psql` with `SELECT 1`.
    """

    def __init__(self, name: str, version: str = "0.1.0", base_dir: str = "/backups") -> None:
        super().__init__(name=name, version=version)
        self.base_dir = base_dir
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        url = config.get("database_url")
        return isinstance(url, str) and bool(url)

    @staticmethod
    def _get_optional_str(config: Dict[str, Any], key: str) -> str | None:
        value = config.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None

    def _get_connection_url(self, config: Dict[str, Any], *, prefer_direct: bool = False) -> str:
        """Return the configured PostgreSQL URL.

        If `prefer_direct=True`, use `database_direct_url` first to support
        Cal.com/Prisma deployments that use a pooled `database_url`.
        """
        if prefer_direct:
            direct = config.get("database_direct_url")
            if isinstance(direct, str) and direct.strip():
                return direct.strip()
        primary = config.get("database_url")
        if isinstance(primary, str):
            return primary.strip()
        return ""

    def _extract_unsupported_settings(self, stderr_text: str) -> set[str]:
        settings = {
            match.group("name").lower()
            for match in re.finditer(
                r'unrecognized configuration parameter "(?P<name>[^"]+)"',
                stderr_text,
                flags=re.IGNORECASE,
            )
        }
        # Defensive fallback for known cross-version restore issue.
        if "transaction_timeout" in stderr_text.lower():
            settings.add("transaction_timeout")
        return settings

    def _quote_ident(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _should_retry_after_schema_reset(self, stderr_text: str) -> bool:
        lower = stderr_text.lower()
        return (
            "already exists" in lower
            or (
                "cannot drop constraint" in lower
                and "because other objects depend on it" in lower
            )
            or (
                "use drop ... cascade" in lower
                and "depend" in lower
            )
        )

    def _sanitize_restore_sql(self, artifact_path: str, unsupported_settings: set[str]) -> str:
        fd, temp_sql_path = tempfile.mkstemp(prefix="calcom-restore-", suffix=".sql")
        os.close(fd)

        removed_lines = 0
        set_line_pattern = re.compile(
            r"^\s*SET\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=",
            flags=re.IGNORECASE,
        )

        with open(artifact_path, "r", encoding="utf-8", errors="ignore") as src:
            with open(temp_sql_path, "w", encoding="utf-8") as dst:
                for line in src:
                    match = set_line_pattern.match(line)
                    if match and match.group(1).lower() in unsupported_settings:
                        removed_lines += 1
                        continue
                    dst.write(line)

        self._logger.warning(
            "calcom_restore_sanitized_sql | artifact=%s temp=%s settings=%s removed_lines=%s",
            artifact_path,
            temp_sql_path,
            sorted(unsupported_settings),
            removed_lines,
        )
        return temp_sql_path

    def _extract_schemas_from_dump(self, sql_path: str) -> list[str]:
        schemas: set[str] = set()
        system_schemas = {"pg_catalog", "information_schema", "pg_toast"}

        create_schema_re = re.compile(
            r'^\s*CREATE\s+SCHEMA(?:\s+IF\s+NOT\s+EXISTS)?\s+("(?P<q>[^"]+)"|(?P<u>[A-Za-z_][A-Za-z0-9_]*))',
            flags=re.IGNORECASE,
        )
        search_path_re = re.compile(
            r'^\s*SET\s+search_path\s*=\s*("(?P<q>[^"]+)"|(?P<u>[A-Za-z_][A-Za-z0-9_]*))',
            flags=re.IGNORECASE,
        )
        # Restrict schema extraction to DDL/COPY lines to avoid false positives
        # from quoted table/column references and data values.
        object_ddl_schema_re = re.compile(
            r'^\s*(?:CREATE|ALTER|DROP|TRUNCATE|COMMENT\s+ON|COPY)\s+'
            r'(?:TABLE|TYPE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|FUNCTION|INDEX|TRIGGER|POLICY|RULE|DOMAIN|TEXT\s+SEARCH\s+CONFIGURATION|TEXT\s+SEARCH\s+DICTIONARY)?'
            r'.*?\b(?:ON\s+|TABLE\s+|TYPE\s+|VIEW\s+|SEQUENCE\s+|FUNCTION\s+|DOMAIN\s+|COPY\s+)?'
            r'("(?P<qschema>[^"]+)"|(?P<uschema>[A-Za-z_][A-Za-z0-9_]*))\s*\.\s*(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
            flags=re.IGNORECASE,
        )

        with open(sql_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                for pattern in (create_schema_re, search_path_re):
                    match = pattern.search(line)
                    if match:
                        schema = (match.group("q") or match.group("u") or "").strip()
                        if schema:
                            schemas.add(schema)
                match = object_ddl_schema_re.search(line)
                if match:
                    schema = (match.group("qschema") or match.group("uschema") or "").strip()
                    if schema:
                        schemas.add(schema)

        filtered = [
            s for s in schemas
            if s.lower() not in system_schemas and not s.lower().startswith("pg_")
        ]
        if not filtered:
            filtered = ["public"]
        return sorted(set(filtered))

    async def _run_psql_restore(self, db_url: str, sql_path: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "psql",
            db_url,
            "-X",
            "--set",
            "ON_ERROR_STOP=on",
            "-f",
            sql_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await proc.communicate()
        err = stderr_data.decode(errors="ignore").strip()
        return proc.returncode, err

    async def _reset_public_schema(self, db_url: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "psql",
            db_url,
            "-X",
            "--set",
            "ON_ERROR_STOP=on",
            "-c",
            "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await proc.communicate()
        err = stderr_data.decode(errors="ignore").strip()
        return proc.returncode, err

    async def _grant_permissions_to_role(self, db_url: str, role_name: str, schemas: list[str]) -> tuple[int, str]:
        quoted_role = self._quote_ident(role_name)
        statements: list[str] = []
        for schema_name in schemas:
            quoted_schema = self._quote_ident(schema_name)
            statements.extend(
                [
                    f"GRANT USAGE, CREATE ON SCHEMA {quoted_schema} TO {quoted_role};",
                    f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {quoted_schema} TO {quoted_role};",
                    f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {quoted_role};",
                    f"GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {quoted_schema} TO {quoted_role};",
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} GRANT ALL PRIVILEGES ON TABLES TO {quoted_role};",
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} GRANT ALL PRIVILEGES ON SEQUENCES TO {quoted_role};",
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} GRANT ALL PRIVILEGES ON FUNCTIONS TO {quoted_role};",
                ]
            )
        sql = "".join(statements)
        proc = await asyncio.create_subprocess_exec(
            "psql",
            db_url,
            "-X",
            "--set",
            "ON_ERROR_STOP=on",
            "-c",
            sql,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await proc.communicate()
        err = stderr_data.decode(errors="ignore").strip()
        return proc.returncode, err

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: database_url is required")
        db_url = self._get_connection_url(config, prefer_direct=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                "psql",
                db_url,
                "-X",
                "--set",
                "ON_ERROR_STOP=on",
                "-tA",
                "-c",
                "SELECT 1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="ignore").strip()
                raise ConnectionError(f"Failed to connect to PostgreSQL database: {err or 'unknown error'}")
            if stdout.decode(errors="ignore").strip() != "1":
                raise ConnectionError("Failed to validate PostgreSQL connection: unexpected test query result")
            return True
        except FileNotFoundError:
            self._logger.warning("psql_not_found")
            raise FileNotFoundError("psql command not found. Please ensure PostgreSQL client tools are installed.")
        except ConnectionError:
            raise

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        db_url = self._get_connection_url(cfg, prefer_direct=True)
        if not db_url:
            raise ValueError("database_url is required")

        meta = context.metadata or {}
        slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        base_dir = Path(self.base_dir) / slug / today
        base_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        artifact_path = base_dir / f"calcom-db-{timestamp}.sql"

        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            "--clean",
            "--if-exists",
            db_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self._logger.error(
                "calcom_pg_dump_failed | code=%s stderr=%s", proc.returncode, stderr.decode()
            )
            raise RuntimeError("pg_dump failed")

        with open(artifact_path, "wb") as f:
            f.write(stdout)

        write_backup_sidecar(str(artifact_path), self, context, logger=self._logger)

        return {"artifact_path": str(artifact_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a Cal.com PostgreSQL database from a SQL dump file using psql."""
        cfg = context.config or {}
        db_url = self._get_connection_url(cfg, prefer_direct=True)
        grant_role = self._get_optional_str(cfg, "restore_grant_role")
        
        if not db_url:
            raise ValueError("database_url is required for restore")
        
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        
        self._logger.info(
            "calcom_restore_start | job_id=%s source=%s dest=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            artifact_path,
        )
        
        temp_sql_path: str | None = None
        restore_sql_path = artifact_path

        try:
            returncode, err = await self._run_psql_restore(db_url, restore_sql_path)
            if returncode != 0:
                unsupported_settings = self._extract_unsupported_settings(err)
                if unsupported_settings:
                    temp_sql_path = self._sanitize_restore_sql(artifact_path, unsupported_settings)
                    restore_sql_path = temp_sql_path
                    self._logger.warning(
                        "calcom_restore_retry_without_unsupported_settings | artifact=%s settings=%s",
                        artifact_path,
                        sorted(unsupported_settings),
                    )
                    returncode, err = await self._run_psql_restore(db_url, restore_sql_path)

            if returncode != 0 and self._should_retry_after_schema_reset(err):
                self._logger.warning(
                    "calcom_restore_retry_after_schema_reset | artifact=%s",
                    artifact_path,
                )
                reset_code, reset_err = await self._reset_public_schema(db_url)
                if reset_code != 0:
                    raise RuntimeError(f"psql schema reset failed: {reset_err}")
                returncode, err = await self._run_psql_restore(db_url, restore_sql_path)
        except OSError as exc:
            self._logger.error(
                "calcom_restore_exec_error | job_id=%s source=%s dest=%s error=%s",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                exc,
            )
            raise
        finally:
            if temp_sql_path and os.path.exists(temp_sql_path):
                try:
                    os.remove(temp_sql_path)
                except OSError as exc:
                    self._logger.warning(
                        "calcom_restore_temp_cleanup_failed | path=%s error=%s",
                        temp_sql_path,
                        exc,
                    )
        
        if returncode != 0:
            raise RuntimeError(f"psql restore failed: {err}")

        if grant_role:
            schemas = self._extract_schemas_from_dump(artifact_path)
            grant_code, grant_err = await self._grant_permissions_to_role(db_url, grant_role, schemas)
            if grant_code != 0:
                raise RuntimeError(
                    f"psql grant for app role '{grant_role}' failed: {grant_err}"
                )
            self._logger.info(
                "calcom_restore_permissions_granted | app_role=%s schemas=%s",
                grant_role,
                schemas,
            )
        
        artifact_bytes = os.path.getsize(artifact_path)
        
        self._logger.info(
            "calcom_restore_success | job_id=%s source=%s dest=%s artifact=%s bytes=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            artifact_path,
            artifact_bytes,
        )
        
        return {
            "status": "success",
            "artifact_path": artifact_path,
            "artifact_bytes": artifact_bytes,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "not implemented"}
