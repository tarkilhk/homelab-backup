"""Sidecar metadata utilities for backup artifacts."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .base import BackupContext, BackupPlugin


def write_backup_sidecar(
    artifact_path: str,
    plugin: BackupPlugin,
    context: BackupContext,
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
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
        logger: Optional logger for error messages (falls back to no-op if None)
        
    Raises:
        OSError: If writing the sidecar file fails (but backup should still succeed)
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    try:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        
        sidecar_data: Dict[str, Any] = {
            "plugin_name": plugin.name,
            "target_slug": target_slug,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifact_path": artifact_path,
        }
        
        # Include plugin version if available
        if hasattr(plugin, "version") and plugin.version:
            sidecar_data["plugin_version"] = plugin.version
        
        sidecar_path = f"{artifact_path}.meta.json"
        
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(sidecar_data, f, indent=2)
        
        if logger:
            logger.debug(
                "backup_sidecar_written | artifact=%s sidecar=%s",
                artifact_path,
                sidecar_path,
            )
    except Exception as exc:
        # Never fail backup due to sidecar write failure
        if logger:
            logger.warning(
                "backup_sidecar_write_failed | artifact=%s error=%s",
                artifact_path,
                exc,
            )


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


