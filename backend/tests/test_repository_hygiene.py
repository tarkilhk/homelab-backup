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


def test_backend_pins_database_clients_to_deployed_server_majors() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    contents = dockerfile.read_text(encoding="utf-8")

    assert (
        "FROM mysql@sha256:3e5649c69e6d75cf88fc6f8f39f877453faa4e5167b5e648007e45f54bb17f6b "
        "AS mysql-client" in contents
    )
    assert "AS postgres16-client" in contents
    assert (
        "FROM postgres@sha256:b939b3851e2cccb017dc4497af63b15e34efa57fba036548773c53b2f16a8871 "
        "AS postgres-client" in contents
    )
    assert "FROM wordpress:cli-2.12.0-php8.2 AS wordpress-cli" in contents
    assert "mariadb-client-core" in contents
    assert "ln -s /usr/bin/mariadb-check /usr/local/bin/mysqlcheck" in contents
    assert "default-mysql-client" not in contents
    assert "apt-get install -y postgresql-client" not in contents


def test_backend_pins_exact_mysql_shell_and_python_runtime() -> None:
    """The Oracle Shell protocol must not drift through a moving package or base tag."""
    repository_root = Path(__file__).resolve().parents[2]
    dockerfile = (repository_root / "backend" / "Dockerfile").read_text(encoding="utf-8")
    workflow = (repository_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    normalized_dockerfile = " ".join(dockerfile.replace("\\\n", " ").split())

    assert (
        "FROM python@sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454"
        in normalized_dockerfile
    )
    assert (
        "ADD --checksum=sha256:"
        "5e9576a3e65d1f21d6879882e5c4e73b63b3ac49b6356a171b68b0be7f342621 "
        "https://repo.mysql.com/apt/debian/pool/mysql-8.4-lts/m/mysql-shell/"
        "mysql-shell_8.4.0-1debian12_amd64.deb /tmp/mysql-shell.deb" in normalized_dockerfile
    )
    assert "dpkg-query -W -f='${Version} ${Architecture}' mysql-shell" in dockerfile
    assert "8.4.0-1debian12 amd64" in dockerfile
    assert "locales" in dockerfile
    assert "localedef -i en_US -f UTF-8 en_US.UTF-8" in dockerfile
    assert "mysqlsh --version" in dockerfile
    assert "mysql-apt-config" not in dockerfile
    assert "apt-get install mysql-shell" not in dockerfile
    assert 'docker run --rm --entrypoint mysqlsh "${image}" --version' in workflow
    assert "grep '8.4.0'" in workflow


def test_backend_uses_the_pinned_postgresql_libpq_for_both_client_majors() -> None:
    """The copied PG16/PG18 binaries must not bind to Debian's older libpq."""

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY --from=postgres-client /usr/lib/x86_64-linux-gnu/libpq.so.5.18 "
        "/usr/local/lib/libpq.so.5.18" in dockerfile
    )
    assert "ln -s /usr/local/lib/libpq.so.5.18 /usr/local/lib/libpq.so.5" in dockerfile
    assert "ldconfig" in dockerfile


def test_backend_image_installs_git_for_profilarr_bundles() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    contents = dockerfile.read_text(encoding="utf-8")

    install_block = contents.split("RUN apt-get update && apt-get install -y", 1)[1].split(
        "&& rm -rf /var/lib/apt/lists/*", 1
    )[0]
    assert "\n    git \\" in install_block
