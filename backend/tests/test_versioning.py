from __future__ import annotations

import json
from importlib.metadata import version as installed_package_version
from pathlib import Path

import pytest

from scripts.check_version import (
    VersionValidationError,
    main,
    parse_semver,
    validate_repository_version,
)


def test_fastapi_reports_installed_distribution_version() -> None:
    from app.main import app

    canonical = (
        (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
    )
    assert installed_package_version("homelab-backup") == canonical
    assert app.version == canonical
    assert app.openapi()["info"]["version"] == canonical


@pytest.mark.parametrize(
    "value",
    [
        "0.2.0",
        "1.0.0",
        "12.34.56-rc.1",
    ],
)
def test_parse_semver_accepts_release_versions(value: str) -> None:
    assert str(parse_semver(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        "v1.2.3",
        "1.2",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3+build.1",
    ],
)
def test_parse_semver_rejects_unsupported_release_versions(value: str) -> None:
    with pytest.raises(VersionValidationError):
        parse_semver(value)


def _write_repository_versions(root: Path, version: str = "1.2.3") -> None:
    (root / "backend").mkdir()
    (root / "frontend").mkdir()
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "homelab-backup"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "frontend" / "package.json").write_text(
        json.dumps({"name": "frontend", "version": version}),
        encoding="utf-8",
    )
    (root / "frontend" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "version": version,
                "packages": {"": {"name": "frontend", "version": version}},
            }
        ),
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{version}] - 2026-08-15\n",
        encoding="utf-8",
    )


def test_validate_repository_version_accepts_synchronized_manifests(tmp_path: Path) -> None:
    _write_repository_versions(tmp_path)

    version = validate_repository_version(tmp_path, release_tag="v1.2.3")

    assert str(version) == "1.2.3"


def test_validate_repository_version_reports_every_mismatch(tmp_path: Path) -> None:
    _write_repository_versions(tmp_path)
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps({"name": "frontend", "version": "1.2.2"}),
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "version": "1.2.1",
                "packages": {"": {"name": "frontend", "version": "1.2.0"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VersionValidationError) as exc_info:
        validate_repository_version(tmp_path)

    message = str(exc_info.value)
    assert "frontend/package.json" in message
    assert "frontend/package-lock.json" in message
    assert 'frontend/package-lock.json packages[""]' in message


def test_validate_repository_version_rejects_wrong_release_tag(tmp_path: Path) -> None:
    _write_repository_versions(tmp_path)

    with pytest.raises(VersionValidationError, match="release tag v1.2.2"):
        validate_repository_version(tmp_path, release_tag="v1.2.2")


def test_validate_repository_version_requires_dated_changelog_entry(tmp_path: Path) -> None:
    _write_repository_versions(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n",
        encoding="utf-8",
    )

    with pytest.raises(VersionValidationError, match="CHANGELOG.md"):
        validate_repository_version(tmp_path)


def test_cli_writes_validated_version_to_github_output(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _write_repository_versions(repository_root)
    github_output = tmp_path / "github-output"

    exit_code = main(
        [
            "--repository-root",
            str(repository_root),
            "--tag",
            "v1.2.3",
            "--github-output",
            str(github_output),
        ]
    )

    assert exit_code == 0
    assert github_output.read_text(encoding="utf-8") == "version=1.2.3\n"
