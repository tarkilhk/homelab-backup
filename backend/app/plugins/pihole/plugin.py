from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin


class PiHolePlugin(BackupPlugin):
    """Minimal Pi-hole plugin for scaffolding/testing.

    For now, backup() just writes a small JSON artifact to the conventional
    backup directory and returns metadata.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        # In a later pass, validate base_url/token formats.
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        # Determine directories following convention: /backups/<targetSlug>/<YYYY-MM-DD>/
        # We derive slug from context.metadata["target_slug"] if available, else use target_id.
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

        base_dir = os.path.join("/backups", target_slug, today)
        os.makedirs(base_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"pihole-{timestamp}.json")

        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump({"ok": True}, f)

        return {
            "artifact_path": artifact_path,
        }

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "not_implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}


