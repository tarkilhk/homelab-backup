from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Dict

from .base import RestoreContext


def copy_artifact_for_restore(
    context: RestoreContext,
    *,
    logger: logging.Logger,
    restore_root: str,
    prefix: str,
) -> Dict[str, str]:
    """Utility used by plugins to simulate restore by copying artifact to a restore location.

    Returns a dict with status/message/restored_path for consistency.
    """
    artifact_path = context.artifact_path
    if not artifact_path:
        raise ValueError("Restore requires artifact_path")
    if not os.path.exists(artifact_path):
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    metadata = context.metadata or {}
    slug = (
        metadata.get("destination_target_slug")
        or metadata.get("target_slug")
        or str(context.destination_target_id)
    )
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    restore_dir = os.path.join(restore_root, slug, "restores", timestamp)
    os.makedirs(restore_dir, exist_ok=True)

    _, ext = os.path.splitext(artifact_path)
    restored_path = os.path.join(restore_dir, f"{prefix}-restore{ext or '.bin'}")
    shutil.copy2(artifact_path, restored_path)

    artifact_bytes = None
    artifact_sha = None
    try:
        artifact_bytes = int(os.path.getsize(restored_path))
        import hashlib
        digest = hashlib.sha256()
        with open(restored_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact_sha = digest.hexdigest()
    except Exception:
        artifact_bytes = None
        artifact_sha = None

    logger.info(
        "plugin_restore_copy | plugin=%s source_target=%s destination_target=%s artifact=%s restored_path=%s",
        prefix,
        context.source_target_id,
        context.destination_target_id,
        artifact_path,
        restored_path,
    )

    return {
        "status": "success",
        "message": f"Artifact copied to {restored_path}",
        "restored_path": restored_path,
        "artifact_bytes": artifact_bytes,
        "sha256": artifact_sha,
    }
