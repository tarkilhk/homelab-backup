import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.base import BackupContext
from app.plugins.wordpress import WordPressPlugin


class DummyProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_test_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = WordPressPlugin(name="wordpress")
    ok = await plugin.test({"site_path": str(tmp_path)})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        # last arg is db path when exporting
        if "export" in cmd:
            db_file = cmd[-1]
            with open(db_file, "wb") as f:
                f.write(b"sql")
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    plugin = WordPressPlugin(name="wordpress")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"site_path": str(tmp_path)},
        metadata={"target_slug": "wp-test"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)
