from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUNTIME_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".backup")


def _looks_like_runtime_database(path: str) -> bool:
    name = Path(path).name.lower()
    return name.endswith(RUNTIME_DATABASE_SUFFIXES) or any(
        marker in name for marker in (".db.backup.", ".sqlite.backup.", ".sqlite3.backup.")
    )


def test_repository_does_not_track_runtime_databases() -> None:
    """Runtime state can contain credentials and must never enter Git."""

    if shutil.which("git") is None:
        pytest.skip("Git is not installed in this minimal runtime image")

    root_result = subprocess.run(
        [
            "git",
            "-C",
            str(Path(__file__).resolve().parent),
            "rev-parse",
            "--show-toplevel",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    repository_root = Path(root_result.stdout.strip())

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_runtime_databases = [
        path
        for path in result.stdout.split("\0")
        if path and _looks_like_runtime_database(path) and (repository_root / path).exists()
    ]

    assert tracked_runtime_databases == [], (
        "Runtime database or backup files are tracked by Git: " f"{tracked_runtime_databases}"
    )
