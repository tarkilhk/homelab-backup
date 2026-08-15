import asyncio
import io
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
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
        assert "--allow-root" in cmd
        assert f"--path={tmp_path}" in cmd
        assert cmd[-2:] == ("core", "is-installed")
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await WordPressPlugin("wordpress").test({"site_path": str(tmp_path)})


@pytest.mark.asyncio
async def test_backup_contains_consistent_database_and_site_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "wp-config.php").write_text("<?php // fixture")
    backup_root = tmp_path / "backups"

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        assert "--allow-root" in cmd
        assert f"--path={site}" in cmd
        assert "export" in cmd
        for option in (
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
        ):
            assert option in cmd
        Path(cmd[cmd.index("export") + 1]).write_text("CREATE TABLE proof (id INT);")
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await WordPressPlugin("wordpress").backup(
        BackupContext(
            job_id="1",
            target_id="1",
            config={"site_path": str(site), "backup_base_path": str(backup_root)},
            metadata={"target_slug": "wp-test"},
        )
    )

    artifact = Path(result["artifact_path"])
    assert artifact.is_file()
    with tarfile.open(artifact, "r:gz") as archive:
        assert "db.sql" in archive.getnames()
        assert "site/wp-config.php" in archive.getnames()


@pytest.mark.asyncio
async def test_restore_replaces_files_imports_database_and_verifies_wordpress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "wordpress-backup.tar.gz"
    source_site = tmp_path / "source-site"
    (source_site / "wp-content" / "uploads").mkdir(parents=True)
    (source_site / "wp-config.php").write_text("<?php // restored")
    (source_site / "wp-content" / "uploads" / "proof.txt").write_text("restored")
    database = tmp_path / "db.sql"
    database.write_text("CREATE TABLE proof (id INT);")
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(source_site, arcname="site")
        archive.add(database, arcname="db.sql")

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "wp-config.php").write_text("<?php // original")
    (destination / "stale.txt").write_text("remove me")
    commands: list[tuple[Any, ...]] = []

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        assert "--allow-root" in cmd
        assert f"--path={destination}" in cmd
        commands.append(cmd)
        if "export" in cmd:
            Path(cmd[cmd.index("export") + 1]).write_text("CREATE TABLE original (id INT);")
            return DummyProcess(returncode=0)
        if "reset" in cmd:
            return DummyProcess(returncode=0)
        assert (destination / "wp-content" / "uploads" / "proof.txt").read_text() == "restored"
        if "import" in cmd:
            imported = Path(cmd[cmd.index("import") + 1])
            assert imported.read_text() == database.read_text()
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await WordPressPlugin("wordpress").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={"site_path": str(destination)},
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert not (destination / "stale.txt").exists()
    assert any("import" in command for command in commands)
    assert any("reset" in command for command in commands)
    assert any(command[-2:] == ("core", "is-installed") for command in commands)
    assert any(command[-2:] == ("db", "check") for command in commands)


@pytest.mark.asyncio
async def test_restore_rejects_archive_traversal(tmp_path: Path) -> None:
    artifact = tmp_path / "unsafe.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"evil"))

    with pytest.raises(RuntimeError, match="unsafe path"):
        await WordPressPlugin("wordpress").restore(
            RestoreContext(
                job_id="2",
                source_target_id="1",
                destination_target_id="2",
                config={"site_path": str(tmp_path / "destination")},
                artifact_path=str(artifact),
            )
        )

    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_restore_rejects_dangerous_destination(tmp_path: Path) -> None:
    artifact = tmp_path / "wordpress-backup.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "wp-config.php").write_text("<?php")
    database = tmp_path / "db.sql"
    database.write_text("SELECT 1;")
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(source, arcname="site")
        archive.add(database, arcname="db.sql")

    with pytest.raises(ValueError, match="unsafe WordPress destination"):
        await WordPressPlugin("wordpress").restore(
            RestoreContext(
                job_id="2",
                source_target_id="1",
                destination_target_id="2",
                config={"site_path": "/"},
                artifact_path=str(artifact),
            )
        )


def test_restore_rejects_destination_overlapping_backup_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup_root = tmp_path / "backups"
    destination = backup_root / "wordpress"
    destination.mkdir(parents=True)
    (destination / "wp-config.php").write_text("<?php")
    artifact = backup_root / "artifact.tar.gz"
    artifact.write_bytes(b"artifact")
    monkeypatch.setenv("BACKUP_BASE_PATH", str(backup_root))

    with pytest.raises(ValueError, match="unsafe WordPress destination"):
        WordPressPlugin("wordpress")._validate_restore_destination(destination, artifact)


def test_restore_rejects_destination_containing_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "wordpress"
    destination.mkdir()
    (destination / "wp-config.php").write_text("<?php")
    artifact = destination / "artifact.tar.gz"
    artifact.write_bytes(b"artifact")

    with pytest.raises(ValueError, match="unsafe WordPress destination"):
        WordPressPlugin("wordpress")._validate_restore_destination(destination, artifact)


@pytest.mark.asyncio
async def test_restore_rolls_back_files_and_database_on_import_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "wordpress-backup.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "wp-config.php").write_text("<?php // restored")
    (source / "new.txt").write_text("new")
    database = tmp_path / "db.sql"
    database.write_text("CREATE TABLE restored (id INT);")
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(source, arcname="site")
        archive.add(database, arcname="db.sql")

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "wp-config.php").write_text("<?php // original")
    (destination / "old.txt").write_text("old")
    restored_import_failed = False
    rollback_imported = False

    async def fake_exec(*cmd: Any, **kwargs: Any) -> DummyProcess:
        nonlocal restored_import_failed, rollback_imported
        if "export" in cmd:
            Path(cmd[cmd.index("export") + 1]).write_text("CREATE TABLE original (id INT);")
            return DummyProcess()
        if "import" in cmd:
            import_path = Path(cmd[cmd.index("import") + 1])
            if import_path == database or import_path.name == "db.sql":
                restored_import_failed = True
                return DummyProcess(returncode=1, stderr=b"import failed")
            rollback_imported = True
        return DummyProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="import failed"):
        await WordPressPlugin("wordpress").restore(
            RestoreContext(
                job_id="2",
                source_target_id="1",
                destination_target_id="2",
                config={"site_path": str(destination)},
                artifact_path=str(artifact),
            )
        )

    assert restored_import_failed is True
    assert rollback_imported is True
    assert (destination / "old.txt").read_text() == "old"
    assert not (destination / "new.txt").exists()
