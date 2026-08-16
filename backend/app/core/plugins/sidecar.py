"""Sidecar metadata utilities for backup artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .base import BackupContext, BackupPlugin

_RESERVED_METADATA_KEYS = frozenset(
    {
        "plugin_name",
        "plugin_version",
        "target_slug",
        "created_at",
        "artifact_path",
        "artifact_bytes",
        "sha256",
    }
)


def write_backup_sidecar(
    artifact_path: str,
    plugin: BackupPlugin,
    context: BackupContext,
    *,
    extra_metadata: Mapping[str, object] | None = None,
    artifact_bytes: int | None = None,
    artifact_sha256: str | None = None,
    logger: Optional[logging.Logger] = None,
    owned_temporary_path: str | None = None,
    owned_temporary_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Write a JSON sidecar file alongside a backup artifact with metadata.

    The sidecar file is named `<artifact_path>.meta.json` and contains:
    - plugin_name: Name of the plugin that created the backup
    - plugin_version: Version of the plugin (optional)
    - target_slug: Slug of the target that was backed up
    - created_at: ISO timestamp when the backup was created
    - artifact_path: Full path to the artifact file

    Args:
        artifact_path: Path to the backup artifact file
        plugin: The BackupPlugin instance that created the artifact
        context: BackupContext used during backup
        extra_metadata: Validated plugin-specific evidence; identity keys are reserved
        logger: Optional logger for error messages (falls back to no-op if None)

    Raises:
        OSError: If the required sidecar cannot be written atomically.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    meta = context.metadata or {}
    target_slug = meta.get("target_slug") or str(context.target_id)
    artifact = Path(artifact_path)
    if artifact_bytes is None or artifact_sha256 is None:
        digest = hashlib.sha256()
        with artifact.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact_bytes = artifact.stat().st_size
        artifact_sha256 = digest.hexdigest()
    if (
        isinstance(artifact_bytes, bool)
        or not isinstance(artifact_bytes, int)
        or artifact_bytes < 0
    ):
        raise ValueError("Backup sidecar artifact size is invalid")
    if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise ValueError("Backup sidecar artifact hash is invalid")

    sidecar_data: Dict[str, Any] = {
        "plugin_name": plugin.name,
        "target_slug": target_slug,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_path": artifact_path,
        "artifact_bytes": artifact_bytes,
        "sha256": artifact_sha256,
    }

    if hasattr(plugin, "version") and plugin.version:
        sidecar_data["plugin_version"] = plugin.version

    if extra_metadata:
        reserved = _RESERVED_METADATA_KEYS & extra_metadata.keys()
        if reserved:
            raise ValueError("Backup sidecar metadata contains reserved keys")
        if not all(isinstance(key, str) for key in extra_metadata):
            raise ValueError("Backup sidecar metadata keys must be strings")
        sidecar_data.update(extra_metadata)

    sidecar_path = f"{artifact_path}.meta.json"
    sidecar_tmp = owned_temporary_path or f"{sidecar_path}.{uuid.uuid4().hex}.tmp"
    sidecar_identity: tuple[int, int] | None = None
    committed = False
    try:
        if owned_temporary_path is None:
            descriptor = os.open(sidecar_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        else:
            flags = os.O_WRONLY | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(sidecar_tmp, flags)
            opened_identity = os.fstat(descriptor)
            if (
                owned_temporary_identity is None
                or not stat.S_ISREG(opened_identity.st_mode)
                or (opened_identity.st_dev, opened_identity.st_ino) != owned_temporary_identity
            ):
                os.close(descriptor)
                raise RuntimeError("Backup sidecar staging identity changed")
        with os.fdopen(descriptor, "w", encoding="utf-8") as sidecar_file:
            json.dump(sidecar_data, sidecar_file, indent=2)
            sidecar_file.flush()
            os.fsync(sidecar_file.fileno())
            opened = os.fstat(sidecar_file.fileno())
            sidecar_identity = opened.st_dev, opened.st_ino
            os.replace(sidecar_tmp, sidecar_path)
            committed = True
            named = os.stat(sidecar_path, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != sidecar_identity:
                raise RuntimeError("Backup sidecar publication identity changed")
        directory_fd = os.open(Path(sidecar_path).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if logger:
            logger.debug(
                "backup_sidecar_written | artifact=%s sidecar=%s",
                artifact_path,
                sidecar_path,
            )
        return sidecar_identity
    except Exception as exc:
        try:
            temporary_status = os.stat(sidecar_tmp, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            temporary_identity = temporary_status.st_dev, temporary_status.st_ino
            if owned_temporary_path is None or temporary_identity == owned_temporary_identity:
                os.unlink(sidecar_tmp)
        if committed and sidecar_identity is not None:
            try:
                named = os.stat(sidecar_path, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (named.st_dev, named.st_ino) == sidecar_identity:
                    os.unlink(sidecar_path)
        if logger:
            logger.error(
                "backup_sidecar_write_failed | artifact=%s error=%s",
                artifact_path,
                exc,
            )
        raise


def read_backup_sidecar(artifact_path: str) -> Optional[Dict[str, Any]]:
    """Read metadata from a backup artifact's sidecar file.

    Args:
        artifact_path: Path to the backup artifact file

    Returns:
        Dictionary with sidecar metadata if found and valid, None otherwise
    """
    sidecar_path = f"{artifact_path}.meta.json"

    if not os.path.exists(sidecar_path):
        return None

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Validate required fields
        if not isinstance(data, dict):
            return None
        if "plugin_name" not in data or "target_slug" not in data:
            return None

        return data
    except Exception:
        return None
