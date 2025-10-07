from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.plugins.base import RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore


def test_copy_artifact_for_restore(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.sql"
    artifact.write_text("restore data")

    ctx = RestoreContext(
        job_id="99",
        source_target_id="1",
        destination_target_id="2",
        config={},
        artifact_path=str(artifact),
        metadata={"destination_target_slug": "dest"},
    )

    result = copy_artifact_for_restore(
        ctx,
        logger=logging.getLogger("test"),
        restore_root=str(tmp_path),
        prefix="unit",
    )

    restored_path = Path(result["restored_path"])
    assert restored_path.exists()
    assert restored_path.read_text() == "restore data"
    assert restored_path.parent.parent.name == "restores"
    assert result["status"] == "success"
    assert result["artifact_bytes"] == len("restore data")
    assert isinstance(result["sha256"], str) and len(result["sha256"]) == 64


def test_copy_artifact_for_restore_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={},
        artifact_path=str(missing),
    )

    with pytest.raises(FileNotFoundError):
        copy_artifact_for_restore(
            ctx,
            logger=logging.getLogger("test"),
            restore_root=str(tmp_path),
            prefix="unit",
        )
