"""Strict Oracle MySQL 8.4 identity and recovery primitives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence, cast

from app.core.subprocesses import run_process_with_timeout

MYSQL = "/usr/local/bin/mysql"
MYSQLSH = "/usr/bin/mysqlsh"
CONNECT_TIMEOUT_SECONDS = 30.0
MAX_PROBE_BYTES = 8 * 1024 * 1024
MySQLMode = Literal["source", "restore_destination"]
_SERVER_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_CATALOG_KEYS = (
    "tables",
    "columns",
    "views",
    "routines",
    "triggers",
    "events",
    "partitions",
    "constraints",
    "constraint_columns",
    "indexes",
    "generated_columns",
)
_SOURCE_SCHEMA_PRIVILEGES = frozenset({"EVENT", "LOCK TABLES", "SELECT", "SHOW VIEW", "TRIGGER"})


@dataclass(frozen=True)
class MySQLTarget:
    """One exact MySQL source or disposable restore destination."""

    mode: MySQLMode
    host: str
    port: int
    database: str
    user: str
    password: str
    ssl_mode: Literal["REQUIRED", "DISABLED"]

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "MySQLTarget":
        """Create a target after the public adapter has validated its shape."""
        mode = config.get("mode")
        ssl_mode = config.get("ssl_mode")
        if mode not in {"source", "restore_destination"} or ssl_mode not in {
            "REQUIRED",
            "DISABLED",
        }:
            raise ValueError("Invalid MySQL source or restore-destination configuration")
        host = config.get("host")
        port = config.get("port")
        database = config.get("database")
        user = config.get("user")
        password = config.get("password")
        if (
            not isinstance(host, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not isinstance(database, str)
            or not isinstance(user, str)
            or not isinstance(password, str)
        ):
            raise ValueError("Invalid MySQL source or restore-destination configuration")
        return cls(
            mode=cast(MySQLMode, mode),
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            ssl_mode=cast(Literal["REQUIRED", "DISABLED"], ssl_mode),
        )


@dataclass(frozen=True)
class MySQLIdentity:
    """Validated, secret-free identity and catalog evidence for one target."""

    shell_version: str
    server_version: str
    version_comment: str
    version_compile_os: str
    version_compile_machine: str
    server_uuid: str
    gtid_mode: str
    default_storage_engine: str
    character_set_server: str
    collation_server: str
    lower_case_table_names: int
    max_allowed_packet: int
    current_user: str
    database: str
    schema_exists: int
    catalog: Mapping[str, tuple[Mapping[str, object], ...]]
    grants: tuple[str, ...]
    catalog_sha256: str

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        grants: Sequence[str],
        *,
        target: MySQLTarget,
        shell_version: str,
    ) -> "MySQLIdentity":
        """Validate one bounded client response against the exact target contract."""
        if shell_version != "8.4.0":
            raise RuntimeError("MySQL Shell identity is unsupported")
        scalar_strings = (
            "server_version",
            "version_comment",
            "version_compile_os",
            "version_compile_machine",
            "server_uuid",
            "gtid_mode",
            "default_storage_engine",
            "character_set_server",
            "collation_server",
            "current_user",
            "database",
        )
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in scalar_strings):
            raise RuntimeError("MySQL returned an invalid identity response")
        lower_case_table_names = payload.get("lower_case_table_names")
        max_allowed_packet = payload.get("max_allowed_packet")
        schema_exists = payload.get("schema_exists")
        if (
            isinstance(lower_case_table_names, bool)
            or lower_case_table_names != 0
            or isinstance(max_allowed_packet, bool)
            or not isinstance(max_allowed_packet, int)
            or max_allowed_packet <= 0
            or isinstance(schema_exists, bool)
            or schema_exists not in {0, 1}
        ):
            raise RuntimeError("MySQL returned an invalid identity response")
        lower_case_table_names_int = cast(int, lower_case_table_names)
        max_allowed_packet_int = cast(int, max_allowed_packet)
        schema_exists_int = cast(int, schema_exists)
        server_version = str(payload["server_version"])
        version_comment = str(payload["version_comment"])
        version_compile_os = str(payload["version_compile_os"])
        version_compile_machine = str(payload["version_compile_machine"])
        server_uuid = str(payload["server_uuid"])
        if (
            server_version != "8.4.0"
            or version_comment != "MySQL Community Server - GPL"
            or version_compile_os != "Linux"
            or version_compile_machine != "x86_64"
            or _SERVER_UUID_RE.fullmatch(server_uuid) is None
        ):
            raise RuntimeError("MySQL server identity is unsupported")
        if (
            payload["default_storage_engine"] != "InnoDB"
            or payload["character_set_server"] != "utf8mb4"
            or not str(payload["collation_server"]).startswith("utf8mb4_")
            or payload["gtid_mode"] not in {"OFF", "OFF_PERMISSIVE", "ON_PERMISSIVE", "ON"}
            or payload["database"] != target.database
        ):
            raise RuntimeError("MySQL server identity is unsupported")
        current_user = str(payload["current_user"])
        if "@" not in current_user or current_user.split("@", 1)[0] != target.user:
            raise RuntimeError("MySQL returned an invalid identity response")

        canonical_catalog: dict[str, tuple[Mapping[str, object], ...]] = {}
        for key in _CATALOG_KEYS:
            items = payload.get(key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RuntimeError("MySQL returned an invalid catalog response")
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in items]
            if len(encoded) != len(set(encoded)):
                raise RuntimeError("MySQL returned an ambiguous catalog response")
            canonical_catalog[key] = tuple(
                item
                for _, item in sorted(
                    zip(encoded, items, strict=True),
                    key=lambda pair: pair[0],
                )
            )

        tables = canonical_catalog["tables"]
        if any(
            table.get("type") != "BASE TABLE" or table.get("engine") != "InnoDB" for table in tables
        ):
            raise RuntimeError("MySQL source contains non-InnoDB tables")
        normalized_grants = _validate_grants(target, grants, canonical_catalog)
        if target.mode == "source":
            if schema_exists != 1 or not tables:
                raise RuntimeError("MySQL configured schema is absent or empty")
        else:
            if schema_exists != 0:
                raise RuntimeError("MySQL restore destination schema must be absent")
            if any(canonical_catalog[key] for key in _CATALOG_KEYS):
                raise RuntimeError("MySQL restore destination schema must be absent")

        catalog_sha256 = _canonical_sha256(canonical_catalog)
        return cls(
            shell_version=shell_version,
            server_version=server_version,
            version_comment=version_comment,
            version_compile_os=version_compile_os,
            version_compile_machine=version_compile_machine,
            server_uuid=server_uuid,
            gtid_mode=str(payload["gtid_mode"]),
            default_storage_engine=str(payload["default_storage_engine"]),
            character_set_server=str(payload["character_set_server"]),
            collation_server=str(payload["collation_server"]),
            lower_case_table_names=lower_case_table_names_int,
            max_allowed_packet=max_allowed_packet_int,
            current_user=current_user,
            database=target.database,
            schema_exists=schema_exists_int,
            catalog=canonical_catalog,
            grants=normalized_grants,
            catalog_sha256=catalog_sha256,
        )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _grant_parts(grant: str) -> tuple[frozenset[str], str]:
    if not grant.startswith("GRANT ") or " TO " not in grant or " WITH " in grant:
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    authority, account = grant[6:].split(" TO ", 1)
    if not account or " IDENTIFIED " in account or " REQUIRE " in account:
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    privileges_text, separator, scope = authority.partition(" ON ")
    if not separator or not scope:
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    privileges = frozenset(value.strip() for value in privileges_text.split(","))
    if not privileges or "" in privileges:
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    return privileges, scope


def _validate_grants(
    target: MySQLTarget,
    grants: Sequence[str],
    catalog: Mapping[str, tuple[Mapping[str, object], ...]],
) -> tuple[str, ...]:
    if not grants or any(not isinstance(grant, str) or not grant for grant in grants):
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    normalized = tuple(sorted(grants))
    if len(normalized) != len(set(normalized)):
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    observed_usage = False
    observed_schema = False
    observed_show_routine = False
    for grant in normalized:
        privileges, scope = _grant_parts(grant)
        if scope == "*.*" and privileges == {"USAGE"}:
            observed_usage = True
        elif scope == "*.*" and privileges == {"SHOW_ROUTINE"}:
            observed_show_routine = True
        elif scope == f"`{target.database}`.*":
            if target.mode == "source" and privileges == _SOURCE_SCHEMA_PRIVILEGES:
                observed_schema = True
            elif target.mode == "restore_destination" and privileges == {"ALL PRIVILEGES"}:
                observed_schema = True
            else:
                raise RuntimeError("MySQL identity exceeds the least-privilege contract")
        else:
            raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    routines_exist = bool(catalog["routines"])
    if (
        not observed_usage
        or not observed_schema
        or observed_show_routine != routines_exist
        or len(normalized) != 2 + int(routines_exist)
    ):
        raise RuntimeError("MySQL identity exceeds the least-privilege contract")
    return normalized


def _escape_option_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_option_file(workspace: Path, target: MySQLTarget) -> Path:
    path = workspace / "client.cnf"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        lines = [
            "[client]",
            f"host={target.host}",
            f"port={target.port}",
            f"user={target.user}",
            f'password="{_escape_option_value(target.password)}"',
            f"ssl-mode={target.ssl_mode}",
            "protocol=tcp",
        ]
        if target.mode == "source":
            lines.append(f"database={target.database}")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MYSQLSH_USER_CONFIG_HOME": str(workspace),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }


def _json_array(expression: str, source: str, condition: str) -> str:
    return (
        "COALESCE((SELECT JSON_ARRAYAGG(JSON_OBJECT("
        f"{expression})) FROM {source} WHERE {condition}), JSON_ARRAY())"
    )


def _identity_sql(database: str) -> str:
    schema = f"'{database}'"
    tables = _json_array(
        "'name', TABLE_NAME, 'type', TABLE_TYPE, 'engine', ENGINE, "
        "'row_format', ROW_FORMAT, 'collation', TABLE_COLLATION, "
        "'create_options', CREATE_OPTIONS, 'auto_increment', AUTO_INCREMENT",
        "information_schema.TABLES",
        f"TABLE_SCHEMA = {schema} AND TABLE_TYPE = 'BASE TABLE'",
    )
    columns = _json_array(
        "'table', TABLE_NAME, 'name', COLUMN_NAME, 'ordinal', ORDINAL_POSITION, "
        "'default', COLUMN_DEFAULT, 'nullable', IS_NULLABLE, 'data_type', DATA_TYPE, "
        "'column_type', COLUMN_TYPE, 'charset', CHARACTER_SET_NAME, "
        "'collation', COLLATION_NAME, 'extra', EXTRA, "
        "'generation_sha256', SHA2(COALESCE(GENERATION_EXPRESSION, ''), 256)",
        "information_schema.COLUMNS",
        f"TABLE_SCHEMA = {schema}",
    )
    views = _json_array(
        "'name', TABLE_NAME, 'definer', DEFINER, 'security', SECURITY_TYPE, "
        "'check_option', CHECK_OPTION, "
        "'definition_sha256', SHA2(COALESCE(VIEW_DEFINITION, ''), 256)",
        "information_schema.VIEWS",
        f"TABLE_SCHEMA = {schema}",
    )
    routines = _json_array(
        "'name', ROUTINE_NAME, 'type', ROUTINE_TYPE, 'definer', DEFINER, "
        "'security', SECURITY_TYPE, 'data_access', SQL_DATA_ACCESS, "
        "'deterministic', IS_DETERMINISTIC, 'sql_mode', SQL_MODE, "
        "'definition_sha256', SHA2(COALESCE(ROUTINE_DEFINITION, ''), 256)",
        "information_schema.ROUTINES",
        f"ROUTINE_SCHEMA = {schema}",
    )
    triggers = _json_array(
        "'name', TRIGGER_NAME, 'event', EVENT_MANIPULATION, "
        "'table', EVENT_OBJECT_TABLE, 'timing', ACTION_TIMING, 'definer', DEFINER, "
        "'sql_mode', SQL_MODE, "
        "'definition_sha256', SHA2(COALESCE(ACTION_STATEMENT, ''), 256)",
        "information_schema.TRIGGERS",
        f"TRIGGER_SCHEMA = {schema}",
    )
    events = _json_array(
        "'name', EVENT_NAME, 'definer', DEFINER, 'status', STATUS, "
        "'type', EVENT_TYPE, 'interval_value', INTERVAL_VALUE, "
        "'interval_field', INTERVAL_FIELD, 'sql_mode', SQL_MODE, "
        "'definition_sha256', SHA2(COALESCE(EVENT_DEFINITION, ''), 256)",
        "information_schema.EVENTS",
        f"EVENT_SCHEMA = {schema}",
    )
    partitions = _json_array(
        "'table', TABLE_NAME, 'name', PARTITION_NAME, 'ordinal', PARTITION_ORDINAL_POSITION, "
        "'method', PARTITION_METHOD, "
        "'expression_sha256', SHA2(COALESCE(PARTITION_EXPRESSION, ''), 256), "
        "'description_sha256', SHA2(COALESCE(PARTITION_DESCRIPTION, ''), 256), "
        "'rows', TABLE_ROWS",
        "information_schema.PARTITIONS",
        f"TABLE_SCHEMA = {schema}",
    )
    constraints = _json_array(
        "'table', TABLE_NAME, 'name', CONSTRAINT_NAME, 'type', CONSTRAINT_TYPE, "
        "'enforced', ENFORCED",
        "information_schema.TABLE_CONSTRAINTS",
        f"CONSTRAINT_SCHEMA = {schema}",
    )
    constraint_columns = _json_array(
        "'table', TABLE_NAME, 'name', CONSTRAINT_NAME, 'column', COLUMN_NAME, "
        "'ordinal', ORDINAL_POSITION, 'position_in_unique', POSITION_IN_UNIQUE_CONSTRAINT, "
        "'referenced_schema', REFERENCED_TABLE_SCHEMA, "
        "'referenced_table', REFERENCED_TABLE_NAME, "
        "'referenced_column', REFERENCED_COLUMN_NAME",
        "information_schema.KEY_COLUMN_USAGE",
        f"CONSTRAINT_SCHEMA = {schema}",
    )
    indexes = _json_array(
        "'table', TABLE_NAME, 'name', INDEX_NAME, 'non_unique', NON_UNIQUE, "
        "'ordinal', SEQ_IN_INDEX, 'column', COLUMN_NAME, 'expression', EXPRESSION, "
        "'collation', COLLATION, 'sub_part', SUB_PART, 'nullable', NULLABLE, "
        "'type', INDEX_TYPE, 'visible', IS_VISIBLE",
        "information_schema.STATISTICS",
        f"TABLE_SCHEMA = {schema}",
    )
    generated_columns = _json_array(
        "'table', TABLE_NAME, 'name', COLUMN_NAME, 'ordinal', ORDINAL_POSITION, "
        "'data_type', DATA_TYPE, 'extra', EXTRA, "
        "'expression_sha256', SHA2(COALESCE(GENERATION_EXPRESSION, ''), 256)",
        "information_schema.COLUMNS",
        f"TABLE_SCHEMA = {schema} AND EXTRA LIKE '%GENERATED%'",
    )
    return (
        "SELECT JSON_OBJECT("
        "'server_version', @@version, "
        "'version_comment', @@version_comment, "
        "'version_compile_os', @@version_compile_os, "
        "'version_compile_machine', @@version_compile_machine, "
        "'server_uuid', @@server_uuid, "
        "'gtid_mode', @@gtid_mode, "
        "'default_storage_engine', @@default_storage_engine, "
        "'character_set_server', @@character_set_server, "
        "'collation_server', @@collation_server, "
        "'lower_case_table_names', @@lower_case_table_names, "
        "'max_allowed_packet', @@max_allowed_packet, "
        "'current_user', CURRENT_USER(), "
        f"'database', {schema}, "
        "'schema_exists', (SELECT COUNT(*) FROM information_schema.SCHEMATA "
        f"WHERE SCHEMA_NAME = {schema}), "
        f"'tables', {tables}, "
        f"'columns', {columns}, "
        f"'views', {views}, "
        f"'routines', {routines}, "
        f"'triggers', {triggers}, "
        f"'events', {events}, "
        f"'partitions', {partitions}, "
        f"'constraints', {constraints}, "
        f"'constraint_columns', {constraint_columns}, "
        f"'indexes', {indexes}, "
        f"'generated_columns', {generated_columns}"
        "); SHOW GRANTS FOR CURRENT_USER;"
    )


async def _read_limited_stream(
    stream: asyncio.StreamReader,
    *,
    limit_bytes: int,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(min(64 * 1024, limit_bytes + 1 - total)):
        total += len(chunk)
        if total > limit_bytes:
            raise RuntimeError(f"{label} exceeded its safety limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _communicate_with_limits(
    process: asyncio.subprocess.Process,
    *,
    operation: str,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError(f"{operation} did not provide bounded output streams")
    stdout_stream = process.stdout
    stderr_stream = process.stderr

    async def read_and_wait() -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.gather(
            _read_limited_stream(
                stdout_stream,
                limit_bytes=MAX_PROBE_BYTES,
                label=f"{operation} output",
            ),
            _read_limited_stream(
                stderr_stream,
                limit_bytes=MAX_PROBE_BYTES,
                label=f"{operation} diagnostics",
            ),
        )
        await process.wait()
        return stdout, stderr

    return await run_process_with_timeout(
        process,
        read_and_wait(),
        operation=operation,
        timeout_seconds=timeout_seconds,
    )


async def _read_shell_version(environment: Mapping[str, str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            MYSQLSH,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environment),
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("MySQL Shell 8.4 client is unavailable") from exc
    except OSError:
        raise ConnectionError("Unable to execute the MySQL Shell identity check") from None
    stdout, stderr = await _communicate_with_limits(
        process,
        operation="MySQL Shell identity check",
        timeout_seconds=CONNECT_TIMEOUT_SECONDS,
    )
    if process.returncode != 0 or stderr:
        raise RuntimeError("MySQL Shell identity check failed")
    match = re.search(rb"\bVer ([0-9]+\.[0-9]+\.[0-9]+)\b", stdout)
    if match is None:
        raise RuntimeError("MySQL Shell identity is unsupported")
    return match.group(1).decode("ascii")


def _parse_probe_output(payload: bytes) -> tuple[Mapping[str, object], tuple[str, ...]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("MySQL returned an invalid identity response") from exc
    lines = text.splitlines()
    if len(lines) < 3 or any(not line for line in lines):
        raise RuntimeError("MySQL returned an invalid identity response")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError("MySQL returned an invalid identity response") from exc
    if not isinstance(value, dict):
        raise RuntimeError("MySQL returned an invalid identity response")
    return value, tuple(lines[1:])


async def probe_mysql(target: MySQLTarget) -> MySQLIdentity:
    """Probe one exact Oracle MySQL 8.4 target through private client auth."""
    raw_workspace = tempfile.mkdtemp(prefix="homelab-backup-mysql-probe-")
    workspace = Path(raw_workspace)
    os.chmod(workspace, 0o700)
    try:
        environment = _environment(workspace)
        shell_version = await _read_shell_version(environment)
        option_file = _write_option_file(workspace, target)
        try:
            process = await asyncio.create_subprocess_exec(
                MYSQL,
                f"--defaults-file={option_file}",
                "--batch",
                "--raw",
                "--skip-column-names",
                f"--connect-timeout={int(CONNECT_TIMEOUT_SECONDS)}",
                "--execute",
                _identity_sql(target.database),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("Oracle MySQL 8.4 client is unavailable") from exc
        except OSError:
            raise ConnectionError("Unable to connect to the MySQL database") from None
        stdout, stderr = await _communicate_with_limits(
            process,
            operation="MySQL identity probe",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        )
        if process.returncode != 0:
            raise ConnectionError("Unable to connect to the MySQL database")
        if stderr:
            raise RuntimeError("MySQL identity probe emitted diagnostics")
        payload, grants = _parse_probe_output(stdout)
        return MySQLIdentity.from_payload(
            payload,
            grants,
            target=target,
            shell_version=shell_version,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
