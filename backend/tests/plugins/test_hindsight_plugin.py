from __future__ import annotations

import asyncio
import importlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins

HINDSIGHT_VERSION = "0.8.6"
POSTGRES_SERVER_VERSION_NUM = 180006
VECTOR_VERSION = "0.8.6"
ALEMBIC_HEAD = "c7d1e9a4b3f2"
REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "async_operations",
        "audit_log",
        "bank_stats_cache",
        "banks",
        "chunks",
        "directives",
        "documents",
        "entities",
        "entity_cooccurrences",
        "file_storage",
        "graph_maintenance_queue",
        "invalidated_memory_units",
        "llm_requests",
        "memory_links",
        "memory_units",
        "mental_model_history",
        "mental_models",
        "observation_history",
        "unit_entities",
        "webhooks",
    }
)

SOURCE_CONFIG: dict[str, object] = {
    "mode": "source",
    "host": "hindsight-db.local",
    "port": 5432,
    "database": "hindsight_local",
    "user": "hindsight_backup",
    "password": "synthetic-source-password",
}

DESTINATION_CONFIG: dict[str, object] = {
    "mode": "restore_destination",
    "host": "postgres-restore.local",
    "port": 5432,
    "database": "hlb_hindsight_restore_alpha",
    "user": "hindsight_restore_owner",
    "password": "synthetic-destination-password",
}


def _plugin_module() -> Any:
    return importlib.import_module("app.plugins.hindsight.plugin")


def _plugin_class() -> type[Any]:
    return _plugin_module().HindsightPlugin


class _CompletedProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode: int | None = 0
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_hindsight_discovery_schema_and_partial_restore_contract() -> None:
    plugin_class = _plugin_class()
    plugin = get_plugin("hindsight")

    assert isinstance(plugin, plugin_class)
    assert plugin.restore_capability == "partial"
    assert any(
        item["key"] == "hindsight" and item["restore_capability"] == "partial"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("hindsight")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "mode",
        "host",
        "database",
        "user",
        "password",
    }
    assert set(schema["properties"]) == {
        "mode",
        "host",
        "port",
        "database",
        "user",
        "password",
    }
    assert schema["properties"]["mode"]["enum"] == [
        "source",
        "restore_destination",
    ]
    assert schema["properties"]["mode"]["default"] == "source"
    assert schema["properties"]["port"] == {
        "type": "integer",
        "title": "Port",
        "default": 5432,
        "minimum": 1,
        "maximum": 65535,
    }
    assert "default" not in schema["properties"]["password"]


@pytest.mark.asyncio
async def test_hindsight_configuration_is_strict_and_mode_aware() -> None:
    plugin = _plugin_class()(name="hindsight")

    assert await plugin.validate_config(dict(SOURCE_CONFIG)) is True
    assert await plugin.validate_config(dict(DESTINATION_CONFIG)) is True

    invalid_configs: tuple[object, ...] = (
        None,
        {},
        {**SOURCE_CONFIG, "mode": "legacy"},
        {**SOURCE_CONFIG, "host": "postgresql://user:secret@db.local/hindsight"},
        {**SOURCE_CONFIG, "host": "https://db.local"},
        {**SOURCE_CONFIG, "host": "db.local\nmalicious"},
        {**SOURCE_CONFIG, "port": True},
        {**SOURCE_CONFIG, "port": 0},
        {**SOURCE_CONFIG, "port": 65536},
        {**SOURCE_CONFIG, "database": ""},
        {**SOURCE_CONFIG, "database": "unsafe/name"},
        {**SOURCE_CONFIG, "user": "  "},
        {**SOURCE_CONFIG, "password": ""},
        {**SOURCE_CONFIG, "unexpected": "compatibility-fallback"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False


@pytest.mark.asyncio
async def test_source_connectivity_requires_the_exact_read_only_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = str(SOURCE_CONFIG["password"])
    fingerprint = {
        "server_version_num": POSTGRES_SERVER_VERSION_NUM,
        "database": SOURCE_CONFIG["database"],
        "vector_version": VECTOR_VERSION,
        "alembic_heads": [ALEMBIC_HEAD],
        "tables": sorted(REQUIRED_TABLES),
        "rls_tables": [],
    }
    calls: list[tuple[str, ...]] = []
    password_files: list[Path] = []

    async def fake_exec(*args: str, **kwargs: Any) -> _CompletedProcess:
        calls.append(tuple(args))
        assert args[0] == "psql"
        assert "-X" in args
        assert "-tA" in args
        assert password not in args
        assert not any(
            keyword in " ".join(args).upper()
            for keyword in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
        )

        env = kwargs["env"]
        assert "PGPASSWORD" not in env
        password_file = Path(env["PGPASSFILE"])
        password_files.append(password_file)
        assert password_file.is_file()
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        assert password in password_file.read_text(encoding="utf-8")
        return _CompletedProcess(stdout=(json.dumps(fingerprint) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))

    assert result is True
    assert calls
    assert password_files
    assert all(not path.exists() for path in password_files)
