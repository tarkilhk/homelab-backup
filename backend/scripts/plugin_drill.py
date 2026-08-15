#!/usr/bin/env python3
"""Run a repeatable plugin backup/restore drill against isolated services."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin


def _read_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run(args: argparse.Namespace) -> None:
    plugin = get_plugin(args.plugin)
    source_config = _read_config(args.source_config)
    destination_config = _read_config(args.destination_config)

    if not await plugin.test(source_config):
        raise RuntimeError("Source connectivity test did not succeed")
    if not await plugin.test(destination_config):
        raise RuntimeError("Destination connectivity test did not succeed")

    context = BackupContext(
        job_id=f"drill-{args.plugin}",
        target_id=f"{args.plugin}-source",
        config=source_config,
        metadata={"target_slug": args.target_slug},
    )
    artifacts = []
    for _ in range(2):
        result = await plugin.backup(context)
        artifact_value = result.get("artifact_path")
        if not isinstance(artifact_value, str):
            raise RuntimeError("Plugin did not return an artifact path")
        validated = validate_backup_artifact(artifact_value, plugin, context)
        independent_path = Path(artifact_value)
        if independent_path.stat().st_size != validated.size_bytes:
            raise RuntimeError("Independent artifact size does not match validation")
        if _sha256(independent_path) != validated.sha256:
            raise RuntimeError("Independent artifact digest does not match validation")
        artifacts.append(validated)

    if artifacts[0].path == artifacts[1].path:
        raise RuntimeError("Consecutive backups reused an artifact path")

    restore_result = await plugin.restore(
        RestoreContext(
            job_id=f"drill-{args.plugin}",
            source_target_id=f"{args.plugin}-source",
            destination_target_id=f"{args.plugin}-destination",
            config=destination_config,
            artifact_path=str(artifacts[-1].path),
            metadata={"component_version": args.component_version},
        )
    )
    restore_status = restore_result.get("status")
    if restore_status != args.expect_restore_status:
        raise RuntimeError(
            f"Expected restore status {args.expect_restore_status!r}, got {restore_status!r}"
        )

    print(
        json.dumps(
            {
                "plugin": args.plugin,
                "component_version": args.component_version,
                "connectivity": "passed",
                "backup_runs": 2,
                "artifacts_unique": True,
                "artifact_sizes": [item.size_bytes for item in artifacts],
                "artifact_sha256": [item.sha256 for item in artifacts],
                "sidecars": "validated",
                "restore_status": restore_status,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run two backups and one restore against isolated source/destination targets. "
            "Never point the destination config at production."
        )
    )
    parser.add_argument("plugin")
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--target-slug", required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--destination-config", type=Path, required=True)
    parser.add_argument(
        "--expect-restore-status",
        choices=("success", "partial"),
        default="success",
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
