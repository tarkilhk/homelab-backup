from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins import artifacts as artifacts_module
from app.core.plugins.artifacts import (
    create_backup_artifact,
    validate_backup_artifact,
    validate_restore_artifact,
)
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.sidecar import read_backup_sidecar


class _Plugin(BackupPlugin):
    def __init__(self) -> None:
        super().__init__(name="test-plugin", version="2.0")

    async def validate_config(self, config: dict[str, Any]) -> bool:
        return True

    async def test(self, config: dict[str, Any]) -> bool:
        return True

    async def backup(self, context: BackupContext) -> dict[str, Any]:
        raise NotImplementedError

    async def restore(self, context: RestoreContext) -> dict[str, Any]:
        raise NotImplementedError

    async def get_status(self, context: BackupContext) -> dict[str, Any]:
        return {"status": "ready"}


@pytest.fixture
def context() -> BackupContext:
    return BackupContext(
        job_id="17",
        target_id="8",
        config={},
        metadata={"target_slug": "important-service"},
    )


def test_backup_artifact_commits_atomically_with_required_sidecar(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()

    with create_backup_artifact(
        plugin,
        context,
        prefix="service-export",
        suffix=".zip",
        backup_root=tmp_path,
    ) as artifact:
        assert artifact.final_path.exists() is False
        artifact.temporary_path.write_bytes(b"valid backup")

    assert artifact.final_path.read_bytes() == b"valid backup"
    assert artifact.temporary_path.exists() is False
    sidecar = read_backup_sidecar(str(artifact.final_path))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "test-plugin"
    assert sidecar["artifact_path"] == str(artifact.final_path)


def test_backup_artifact_can_publish_the_identity_bound_to_an_open_descriptor(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with create_backup_artifact(
        _Plugin(),
        context,
        prefix="identity-bound",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(b"validated inode")
        artifact.publication_fd = os.open(artifact.temporary_path, os.O_RDONLY)

    assert artifact.final_path.read_bytes() == b"validated inode"
    assert not artifact.temporary_path.exists()
    assert artifact.publication_fd is None


def test_backup_artifact_post_link_replacement_preserves_foreign_file(
    tmp_path: Path,
    context: BackupContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write_sidecar = artifacts_module.write_backup_sidecar
    relocated_owned = tmp_path / "relocated-owned-artifact"

    def replace_after_sidecar(*args: Any, **kwargs: Any) -> tuple[int, int]:
        sidecar_identity = real_write_sidecar(*args, **kwargs)
        final_path = Path(args[0])
        final_path.rename(relocated_owned)
        final_path.write_bytes(b"foreign replacement")
        return sidecar_identity

    monkeypatch.setattr(artifacts_module, "write_backup_sidecar", replace_after_sidecar)

    with pytest.raises(RuntimeError, match="changed while its sidecar"):
        with create_backup_artifact(
            _Plugin(),
            context,
            prefix="identity-race",
            suffix=".bin",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.write_bytes(b"validated inode")
            artifact.publication_fd = os.open(artifact.temporary_path, os.O_RDONLY)
            artifact.publication_sha256 = hashlib.sha256(b"validated inode").hexdigest()

    assert artifact.final_path.read_bytes() == b"foreign replacement"
    assert relocated_owned.read_bytes() == b"validated inode"
    assert not Path(f"{artifact.final_path}.meta.json").exists()


def test_backup_artifact_commits_validated_plugin_evidence_to_sidecar(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()

    with create_backup_artifact(
        plugin,
        context,
        prefix="service-export",
        suffix=".zip",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(b"validated backup")
        artifact.sidecar_metadata["validation"] = "passed"

    sidecar = read_backup_sidecar(str(artifact.final_path))
    assert sidecar is not None
    assert sidecar["sha256"] == hashlib.sha256(b"validated backup").hexdigest()
    assert sidecar["artifact_bytes"] == len(b"validated backup")
    assert sidecar["validation"] == "passed"


def test_backup_artifact_rejects_plugin_evidence_that_overrides_identity(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        with create_backup_artifact(
            _Plugin(),
            context,
            prefix="service-export",
            suffix=".zip",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.write_bytes(b"validated backup")
            artifact.sidecar_metadata["plugin_name"] = "forged"

    assert not list(tmp_path.rglob("*.zip"))


def test_backup_artifact_always_reserves_optional_plugin_version_key(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()
    plugin.version = ""

    with pytest.raises(ValueError, match="reserved"):
        with create_backup_artifact(
            plugin,
            context,
            prefix="service-export",
            suffix=".zip",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.write_bytes(b"validated backup")
            artifact.sidecar_metadata["plugin_version"] = "forged"

    assert not list(tmp_path.rglob("*.zip"))


def test_backup_artifact_names_are_run_unique(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()

    with create_backup_artifact(
        plugin,
        context,
        prefix="service-export",
        suffix=".zip",
        backup_root=tmp_path,
    ) as first:
        first.temporary_path.write_bytes(b"first")
    with create_backup_artifact(
        plugin,
        context,
        prefix="service-export",
        suffix=".zip",
        backup_root=tmp_path,
    ) as second:
        second.temporary_path.write_bytes(b"second")

    assert first.final_path != second.final_path
    assert first.final_path.read_bytes() == b"first"
    assert second.final_path.read_bytes() == b"second"


def test_empty_backup_artifact_is_rejected_and_cleaned(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with pytest.raises(ValueError, match="empty"):
        with create_backup_artifact(
            _Plugin(),
            context,
            prefix="empty",
            suffix=".zip",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.touch()

    assert artifact.final_path.exists() is False
    assert artifact.temporary_path.exists() is False


def test_failed_backup_does_not_expose_partial_artifact(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with pytest.raises(RuntimeError, match="export failed"):
        with create_backup_artifact(
            _Plugin(),
            context,
            prefix="failed",
            suffix=".sql",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.write_bytes(b"partial")
            raise RuntimeError("export failed")

    assert artifact.final_path.exists() is False
    assert artifact.temporary_path.exists() is False


def test_cancelled_backup_does_not_expose_partial_artifact(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        with create_backup_artifact(
            _Plugin(),
            context,
            prefix="cancelled",
            suffix=".db",
            backup_root=tmp_path,
        ) as artifact:
            artifact.temporary_path.write_bytes(b"credential-bearing partial")
            raise asyncio.CancelledError

    assert artifact.final_path.exists() is False
    assert artifact.temporary_path.exists() is False


def test_validation_rejects_artifact_without_sidecar(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    artifact = tmp_path / "legacy.zip"
    artifact.write_bytes(b"content")

    with pytest.raises(ValueError, match="sidecar"):
        validate_backup_artifact(str(artifact), _Plugin(), context)


def test_backup_validation_hashes_while_evicting_completed_ranges(
    tmp_path: Path,
    context: BackupContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _Plugin()
    payload = b"x" * (9 * 1024 * 1024 + 17)
    with create_backup_artifact(
        plugin,
        context,
        prefix="large",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(payload)
    evicted_ranges: list[tuple[int, int]] = []

    def record_eviction(file_descriptor: int, offset: int, length: int) -> None:
        assert file_descriptor >= 0
        evicted_ranges.append((offset, length))

    monkeypatch.setattr("app.core.plugins.artifacts.evict_file_cache", record_eviction)

    validated = validate_backup_artifact(str(artifact.final_path), plugin, context)

    assert validated.sha256 == hashlib.sha256(payload).hexdigest()
    assert evicted_ranges == [
        (0, 8 * 1024 * 1024),
        (8 * 1024 * 1024, 1024 * 1024 + 17),
    ]


def test_backup_and_restore_validation_enforce_sidecar_artifact_identity(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    original = b"artifact-one"
    substituted = b"artifact-two"
    with create_backup_artifact(
        _Plugin(),
        context,
        prefix="identity",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(original)

    artifact.final_path.write_bytes(substituted)

    with pytest.raises(ValueError, match="hash does not match its sidecar"):
        validate_backup_artifact(str(artifact.final_path), _Plugin(), context)
    with pytest.raises(ValueError, match="hash does not match its sidecar"):
        validate_restore_artifact(
            str(artifact.final_path),
            expected_plugin_name="test-plugin",
            backup_root=tmp_path,
        )


def test_backup_and_restore_validation_require_sidecar_artifact_identity(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    with create_backup_artifact(
        _Plugin(),
        context,
        prefix="identity",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(b"artifact")

    sidecar_path = Path(f"{artifact.final_path}.meta.json")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar.pop("artifact_bytes")
    sidecar.pop("sha256")
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(ValueError, match="sidecar artifact size"):
        validate_backup_artifact(str(artifact.final_path), _Plugin(), context)
    with pytest.raises(ValueError, match="sidecar artifact size"):
        validate_restore_artifact(
            str(artifact.final_path),
            expected_plugin_name="test-plugin",
            backup_root=tmp_path,
        )


def test_restore_validation_checks_root_plugin_size_and_hash(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()
    with create_backup_artifact(
        plugin,
        context,
        prefix="test",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(b"trusted restore payload")
    digest = hashlib.sha256(b"trusted restore payload").hexdigest()

    validated = validate_restore_artifact(
        str(artifact.final_path),
        expected_plugin_name="test-plugin",
        backup_root=tmp_path,
        expected_size_bytes=len(b"trusted restore payload"),
        expected_sha256=digest,
    )

    assert validated.sha256 == digest


def test_restore_validation_rejects_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-restore.bin"
    outside.write_bytes(b"payload")

    with pytest.raises(ValueError, match="outside the configured backup root"):
        validate_restore_artifact(
            str(outside),
            expected_plugin_name="test-plugin",
            backup_root=tmp_path,
        )


def test_restore_validation_rejects_plugin_or_hash_mismatch(
    tmp_path: Path,
    context: BackupContext,
) -> None:
    plugin = _Plugin()
    with create_backup_artifact(
        plugin,
        context,
        prefix="test",
        suffix=".bin",
        backup_root=tmp_path,
    ) as artifact:
        artifact.temporary_path.write_bytes(b"original")

    with pytest.raises(ValueError, match="plugin does not match"):
        validate_restore_artifact(
            str(artifact.final_path),
            expected_plugin_name="other",
            backup_root=tmp_path,
        )

    artifact.final_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash does not match"):
        validate_restore_artifact(
            str(artifact.final_path),
            expected_plugin_name="test-plugin",
            backup_root=tmp_path,
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
        )
