#!/usr/bin/env python3
"""Validate the repository's canonical semantic version and release tag."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER_PATTERN = re.compile(
    rf"^(?P<major>0|[1-9]\d*)\."
    rf"(?P<minor>0|[1-9]\d*)\."
    rf"(?P<patch>0|[1-9]\d*)"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?$"
)


class VersionValidationError(ValueError):
    """Raised when repository version metadata is invalid or inconsistent."""


@dataclass(frozen=True)
class SemanticVersion:
    """A release version accepted by this repository."""

    value: str
    major: int
    minor: int
    patch: int
    prerelease: str | None

    def __str__(self) -> str:
        return self.value


def parse_semver(value: str) -> SemanticVersion:
    """Parse a strict SemVer release version suitable for a Docker tag."""
    match = _SEMVER_PATTERN.fullmatch(value)
    if match is None:
        raise VersionValidationError(
            f"{value!r} is not a supported semantic version; expected "
            "MAJOR.MINOR.PATCH with an optional prerelease suffix and no v prefix "
            "or build metadata"
        )
    return SemanticVersion(
        value=value,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("prerelease"),
    )


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise VersionValidationError(f"{path} must contain a JSON object")
    return data


def _record_mismatch(
    mismatches: list[str],
    *,
    source: str,
    actual: object,
    expected: str,
) -> None:
    if actual != expected:
        mismatches.append(f"{source} has {actual!r}; expected {expected!r}")


def validate_repository_version(
    repository_root: Path,
    *,
    release_tag: str | None = None,
) -> SemanticVersion:
    """Validate every version mirror against the root VERSION file."""
    repository_root = repository_root.resolve()
    version_path = repository_root / "VERSION"
    canonical = version_path.read_text(encoding="utf-8").strip()
    version = parse_semver(canonical)

    with (repository_root / "backend" / "pyproject.toml").open("rb") as handle:
        backend_manifest = tomllib.load(handle)
    frontend_manifest = _load_json(repository_root / "frontend" / "package.json")
    frontend_lock = _load_json(repository_root / "frontend" / "package-lock.json")

    mismatches: list[str] = []
    project = backend_manifest.get("project")
    backend_version = project.get("version") if isinstance(project, dict) else None
    _record_mismatch(
        mismatches,
        source="backend/pyproject.toml",
        actual=backend_version,
        expected=canonical,
    )
    _record_mismatch(
        mismatches,
        source="frontend/package.json",
        actual=frontend_manifest.get("version"),
        expected=canonical,
    )
    _record_mismatch(
        mismatches,
        source="frontend/package-lock.json",
        actual=frontend_lock.get("version"),
        expected=canonical,
    )

    packages = frontend_lock.get("packages")
    root_package = packages.get("") if isinstance(packages, dict) else None
    locked_package_version = root_package.get("version") if isinstance(root_package, dict) else None
    _record_mismatch(
        mismatches,
        source='frontend/package-lock.json packages[""]',
        actual=locked_package_version,
        expected=canonical,
    )

    changelog = (repository_root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_heading = re.compile(
        rf"^## \[{re.escape(canonical)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        flags=re.MULTILINE,
    )
    if changelog_heading.search(changelog) is None:
        mismatches.append(
            f"CHANGELOG.md must contain a dated '## [{canonical}] - YYYY-MM-DD' entry"
        )

    if release_tag:
        expected_tag = f"v{canonical}"
        if release_tag != expected_tag:
            mismatches.append(f"release tag {release_tag} does not match {expected_tag}")

    if mismatches:
        raise VersionValidationError("\n".join(mismatches))
    return version


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Release tag to validate; omit for branch and pull-request builds",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append version outputs to a GitHub Actions output file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        version = validate_repository_version(
            args.repository_root,
            release_tag=args.tag or None,
        )
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, VersionValidationError) as exc:
        print(f"version validation failed: {exc}", file=sys.stderr)
        return 1

    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
