from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import threading
import time
import warnings
import zipfile
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app

DATABASE_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
TERMIX_IV_HEX = "101112131415161718191a1b1c1d1e1f"
REQUIRED_TABLES = (
    "users",
    "settings",
    "sessions",
    "trusted_devices",
    "ssh_credentials",
    "ssh_data",
    "snippets",
    "api_keys",
)

# A fixed, independently generated AES-256-GCM known answer. This is deliberately
# not produced by the plugin or by its Python crypto dependency: Node's OpenSSL
# binding encrypted sixteen zero bytes with a zero key and a sixteen-byte zero IV.
KNOWN_ANSWER_CIPHERTEXT = bytes.fromhex("df53c00d30173e8b9cde3ddd2ca3c6bf")
KNOWN_ANSWER_TAG_HEX = "3008621a70b1607f62fd7703232430f1"
TERMIX_COMMIT = "c3282b5dca081d52513e94329bbc71084338217d"
RESTORE_SENTINEL_NAME = ".termix-restore-destination"
RESTORE_SENTINEL_CONTENT = "termix-v2.3.2-isolated-restore-v1\n"


class _NoResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> bool:
        return False

    def recv(self) -> tuple[str, str]:
        raise AssertionError("No worker result is available")

    def close(self) -> None:
        self.closed = True


class _BlockingProcess:
    def __init__(self, *, hold_after_terminate: bool = False) -> None:
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.release = threading.Event()
        self._alive = True
        self._hold_after_terminate = hold_after_terminate

    def join(self, timeout: float) -> None:
        self.join_started.set()
        released = self.release.wait(timeout)
        if released and self.terminate_called.is_set():
            self._alive = False
            self.exitcode = -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()
        if not self._hold_after_terminate:
            self.release.set()

    def kill(self) -> None:
        self.terminate_called.set()
        self.release.set()


class _CompletedProcess:
    exitcode = 1

    def join(self, _timeout: float) -> None:
        return

    def is_alive(self) -> bool:
        return False


class _ResultConnection(_NoResultConnection):
    def __init__(self, result: tuple[str, str]) -> None:
        super().__init__()
        self._result = result

    def poll(self) -> bool:
        return True

    def recv(self) -> tuple[str, str]:
        return self._result


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    """Use the event-loop setup already proven by the real plugin route tests."""

    return ("asyncio", {"use_uvloop": True})


def _termix_plugin_class() -> type[Any]:
    package = importlib.import_module("app.plugins.termix")
    return cast(type[Any], package.TermixPlugin)


def _termix_plugin_module() -> Any:
    return importlib.import_module("app.plugins.termix.plugin")


def _encrypt_with_node(plaintext: bytes, *, key_hex: str, iv_hex: str) -> tuple[bytes, str]:
    """Build a fixture outside the Python implementation under test."""

    script = """
const crypto = require("crypto");
const chunks = [];
process.stdin.on("data", (chunk) => chunks.push(chunk));
process.stdin.on("end", () => {
  const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const cipher = crypto.createCipheriv(
    "aes-256-gcm",
    Buffer.from(input.key, "hex"),
    Buffer.from(input.iv, "hex"),
  );
  const encrypted = Buffer.concat([
    cipher.update(Buffer.from(input.plaintext, "base64")),
    cipher.final(),
  ]);
  process.stdout.write(JSON.stringify({
    ciphertext: encrypted.toString("base64"),
    tag: cipher.getAuthTag().toString("hex"),
  }));
});
"""
    request = json.dumps(
        {
            "key": key_hex,
            "iv": iv_hex,
            "plaintext": base64.b64encode(plaintext).decode("ascii"),
        }
    ).encode("utf-8")
    completed = subprocess.run(
        ["node", "-e", script],
        input=request,
        capture_output=True,
        check=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)
    return base64.b64decode(result["ciphertext"], validate=True), result["tag"]


def _single_file_envelope(ciphertext: bytes, *, iv_hex: str, tag_hex: str) -> bytes:
    metadata = {
        "iv": iv_hex,
        "tag": tag_hex,
        "version": "v2",
        "fingerprint": "termix-v2-systemcrypto",
        "algorithm": "aes-256-gcm",
        "keySource": "SystemCrypto",
        "dataSize": len(ciphertext),
    }
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return len(metadata_bytes).to_bytes(4, byteorder="big") + metadata_bytes + ciphertext


def _create_termix_database(
    path: Path,
    *,
    omit_table: str | None = None,
    foreign_key_violation: bool = False,
) -> None:
    table_statements = {
        "users": (
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0)"
        ),
        "settings": "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "sessions": (
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, jwt_token TEXT NOT NULL)"
        ),
        "trusted_devices": (
            "CREATE TABLE trusted_devices (id TEXT PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, device_fingerprint TEXT NOT NULL)"
        ),
        "ssh_credentials": (
            "CREATE TABLE ssh_credentials (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL)"
        ),
        "ssh_data": (
            "CREATE TABLE ssh_data (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, name TEXT, ip TEXT NOT NULL, "
            "credential_id INTEGER REFERENCES ssh_credentials(id))"
        ),
        "snippets": (
            "CREATE TABLE snippets (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, content TEXT NOT NULL)"
        ),
        "api_keys": (
            "CREATE TABLE api_keys (id TEXT PRIMARY KEY, user_id TEXT NOT NULL "
            "REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, key_hash TEXT NOT NULL)"
        ),
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA page_size=512")
        connection.execute("PRAGMA foreign_keys=ON")
        for table in REQUIRED_TABLES:
            if table != omit_table:
                connection.execute(table_statements[table])
        if foreign_key_violation:
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO users(id, username, password_hash, is_admin) VALUES (?, ?, ?, ?)",
            ("fixture-user", "fixture-admin", "fixed-test-hash", 1),
        )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("instance_name", "Known Answer Termix"),
        )
        if foreign_key_violation:
            connection.execute(
                "INSERT INTO sessions(id, user_id, jwt_token) VALUES (?, ?, ?)",
                ("broken-session", "missing-user", "fixed-test-token"),
            )
        connection.commit()


def _write_termix_source(
    data_path: Path,
    *,
    key_hex: str = DATABASE_KEY_HEX,
    omit_table: str | None = None,
    foreign_key_violation: bool = False,
    include_known_transient_entries: bool = True,
) -> None:
    data_path.mkdir(parents=True)
    database_path = data_path.parent / f"{data_path.name}-fixture.sqlite"
    _create_termix_database(
        database_path,
        omit_table=omit_table,
        foreign_key_violation=foreign_key_violation,
    )
    try:
        plaintext = database_path.read_bytes()
    finally:
        database_path.unlink()

    ciphertext, tag_hex = _encrypt_with_node(
        plaintext,
        key_hex=key_hex,
        iv_hex=TERMIX_IV_HEX,
    )
    (data_path / ".env").write_text(
        "\n".join(
            (
                f"DATABASE_KEY={key_hex}",
                f"JWT_SECRET={'11' * 32}",
                f"ENCRYPTION_KEY={'22' * 32}",
                f"INTERNAL_AUTH_TOKEN={'33' * 32}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (data_path / "db.sqlite.encrypted").write_bytes(
        _single_file_envelope(ciphertext, iv_hex=TERMIX_IV_HEX, tag_hex=tag_hex)
    )
    opk_directory = data_path / ".opk"
    opk_directory.mkdir()
    (opk_directory / "config.yml").write_text("enabled: true\n", encoding="utf-8")

    if include_known_transient_entries:
        for directory_name in ("opkssh", "uploads", ".temp"):
            directory = data_path / directory_name
            directory.mkdir()
            (directory / "regenerable.tmp").write_text("ignored\n", encoding="utf-8")


def _backup_context(data_path: Path, *, target_slug: str = "termix-source") -> BackupContext:
    return BackupContext(
        job_id="termix-backup",
        target_id="1",
        config={"data_path": str(data_path)},
        metadata={"target_slug": target_slug},
    )


def _fresh_restore_path(tmp_path: Path, name: str = "restore") -> Path:
    parent = tmp_path / name
    parent.mkdir()
    (parent / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    return parent / "data"


def _restore_context(artifact_path: Path, destination_path: Path) -> RestoreContext:
    return RestoreContext(
        job_id="termix-restore",
        source_target_id="1",
        destination_target_id="2",
        config={"data_path": str(destination_path)},
        artifact_path=str(artifact_path),
    )


def _assert_restore_parent_contains_only_sentinel(destination_path: Path) -> None:
    assert not destination_path.exists()
    assert {entry.name for entry in destination_path.parent.iterdir()} == {RESTORE_SENTINEL_NAME}


async def _create_backup_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path, Path]:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    monkeypatch.setattr(_termix_plugin_module(), "BACKUP_BASE_PATH", str(backup_root))
    plugin = _termix_plugin_class()(name="termix")
    artifact_path = Path((await plugin.backup(_backup_context(source_path)))["artifact_path"])
    return plugin, artifact_path, source_path


def _archive_payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


def _private_zip_member(name: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o600) << 16
    member.compress_type = zipfile.ZIP_DEFLATED
    return member


def _write_tampered_archive(source: Path, destination: Path, variant: str) -> None:
    payloads = _archive_payloads(source)
    manifest = json.loads(payloads["manifest.json"])
    destination.parent.mkdir(parents=True, exist_ok=True)

    if variant == "bad-zip":
        destination.write_bytes(b"not a zip archive")
        destination.chmod(0o600)
        return
    if variant == "format-version":
        manifest["format_version"] = 2
    elif variant == "wrong-plugin":
        manifest["plugin"] = "not-termix"
    elif variant == "wrong-version":
        manifest["termix_version"] = "2.4.0"
    elif variant == "wrong-commit":
        manifest["termix_commit"] = "0" * 40
    elif variant == "digest":
        manifest["files"][".env"]["sha256"] = "0" * 64
    elif variant == "size":
        manifest["files"]["db.sqlite.encrypted"]["size_bytes"] += 1
    elif variant == "corrupt-payload":
        database = bytearray(payloads["db.sqlite.encrypted"])
        database[-1] ^= 0xFF
        payloads["db.sqlite.encrypted"] = bytes(database)
        manifest["files"]["db.sqlite.encrypted"]["sha256"] = hashlib.sha256(
            payloads["db.sqlite.encrypted"]
        ).hexdigest()

    with zipfile.ZipFile(destination, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            if variant == "missing-env" and name == ".env":
                continue
            if variant == "missing-database" and name == "db.sqlite.encrypted":
                continue
            if name == "manifest.json":
                if variant == "invalid-json":
                    archive.writestr(_private_zip_member(name), b"{")
                elif variant != "missing-manifest":
                    archive.writestr(_private_zip_member(name), json.dumps(manifest))
                continue
            if variant == "symlink-member" and name == ".env":
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(member, b"db.sqlite.encrypted")
            else:
                archive.writestr(_private_zip_member(name), payload)

        if variant == "traversal-member":
            archive.writestr(_private_zip_member("../escape"), b"unsafe")
        elif variant == "absolute-member":
            archive.writestr(_private_zip_member("/absolute"), b"unsafe")
        elif variant == "extra-member":
            archive.writestr(_private_zip_member("unexpected.json"), b"{}")
        elif variant == "duplicate-manifest":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(_private_zip_member("manifest.json"), json.dumps(manifest))
    destination.chmod(0o644 if variant == "public-artifact" else 0o600)


@pytest.mark.anyio
async def test_termix_discovery_schema_and_configuration_contract() -> None:
    plugin_class = _termix_plugin_class()
    plugin = get_plugin("termix")

    assert isinstance(plugin, plugin_class)
    assert plugin.restore_capability == "partial"
    assert any(
        item["key"] == "termix" and item["restore_capability"] == "partial"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("termix")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["required"] == ["data_path"]
    assert set(schema["properties"]) == {"data_path"}
    assert schema["properties"]["data_path"]["default"] == "/sources/termix/data"

    assert await plugin.validate_config({"data_path": "/safe/termix/data"}) is True
    for invalid in (
        {},
        {"data_path": None},
        {"data_path": ""},
        {"data_path": "relative/termix/data"},
        {"data_path": "/"},
        {"data_path": "/safe/../termix/data"},
        {"data_path": "/backups/termix/data"},
        {"data_path": "/safe/termix/data", "extra": True},
    ):
        assert await plugin.validate_config(invalid) is False


@pytest.mark.anyio
async def test_termix_test_accepts_exact_v2_encrypted_state_and_known_transients(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    _write_termix_source(data_path)
    plugin = _termix_plugin_class()(name="termix")

    assert await plugin.test({"data_path": str(data_path)}) is True


@pytest.mark.anyio
async def test_termix_test_uses_exact_aes256_gcm_v2_known_answer(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    key_hex = "00" * 32
    (data_path / ".env").write_text(f"DATABASE_KEY={key_hex}\n", encoding="utf-8")
    (data_path / "db.sqlite.encrypted").write_bytes(
        _single_file_envelope(
            KNOWN_ANSWER_CIPHERTEXT,
            iv_hex="00" * 16,
            tag_hex=KNOWN_ANSWER_TAG_HEX,
        )
    )
    plugin = _termix_plugin_class()(name="termix")

    with pytest.raises(RuntimeError, match="SQLite|database"):
        await plugin.test({"data_path": str(data_path)})

    wrong_key = "ff" * 32
    (data_path / ".env").write_text(f"DATABASE_KEY={wrong_key}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="authenticate|decrypt|key") as exc_info:
        await plugin.test({"data_path": str(data_path)})
    assert wrong_key not in str(exc_info.value)


@pytest.mark.anyio
async def test_termix_test_rejects_invalid_config_without_leaking_supplied_secret() -> None:
    plugin = _termix_plugin_class()(name="termix")
    supplied_secret = "must-never-appear-in-errors"

    with pytest.raises(ValueError, match="Invalid configuration") as exc_info:
        await plugin.test(
            {
                "data_path": "relative/data",
                "DATABASE_KEY": supplied_secret,
            }
        )

    assert supplied_secret not in str(exc_info.value)


@pytest.mark.anyio
async def test_termix_test_rejects_missing_and_legacy_state(tmp_path: Path) -> None:
    plugin = _termix_plugin_class()(name="termix")
    missing_path = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match="data directory|data path"):
        await plugin.test({"data_path": str(missing_path)})

    legacy_path = tmp_path / "legacy"
    legacy_path.mkdir()
    (legacy_path / ".env").write_text(f"DATABASE_KEY={DATABASE_KEY_HEX}\n", encoding="utf-8")
    (legacy_path / "db.sqlite").write_bytes(b"legacy-unencrypted-layout")
    with pytest.raises(RuntimeError, match="legacy|unencrypted|unsupported"):
        await plugin.test({"data_path": str(legacy_path)})


@pytest.mark.anyio
async def test_termix_test_rejects_symlinked_authoritative_files(tmp_path: Path) -> None:
    plugin = _termix_plugin_class()(name="termix")
    data_path = tmp_path / "data"
    _write_termix_source(data_path)
    external = tmp_path / "external.encrypted"
    encrypted_path = data_path / "db.sqlite.encrypted"
    encrypted_path.replace(external)
    encrypted_path.symlink_to(external)

    with pytest.raises(RuntimeError, match="symlink|symbolic|regular file"):
        await plugin.test({"data_path": str(data_path)})


@pytest.mark.anyio
async def test_termix_test_rejects_unknown_persistent_entries(tmp_path: Path) -> None:
    plugin = _termix_plugin_class()(name="termix")
    data_path = tmp_path / "data"
    _write_termix_source(data_path)
    (data_path / "unexpected-state.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unknown|unexpected|unsupported"):
        await plugin.test({"data_path": str(data_path)})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("omit_table", "foreign_key_violation", "message"),
    [
        ("api_keys", False, "schema|api_keys|required table"),
        (None, True, "foreign key|foreign-key"),
    ],
)
async def test_termix_test_rejects_invalid_sqlite_state(
    tmp_path: Path,
    omit_table: str | None,
    foreign_key_violation: bool,
    message: str,
) -> None:
    plugin = _termix_plugin_class()(name="termix")
    data_path = tmp_path / "data"
    _write_termix_source(
        data_path,
        omit_table=omit_table,
        foreign_key_violation=foreign_key_violation,
    )

    with pytest.raises(RuntimeError, match=message):
        await plugin.test({"data_path": str(data_path)})


@pytest.mark.anyio
async def test_termix_plugin_api_exposes_schema_and_secret_safe_connectivity(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    _write_termix_source(data_path)
    supplied_secret = "route-secret-must-be-redacted"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/termix/schema")
        test_response = await client.post(
            "/api/v1/plugins/termix/test",
            json={"data_path": str(data_path)},
        )
        invalid_response = await client.post(
            "/api/v1/plugins/termix/test",
            json={"data_path": "relative/data", "DATABASE_KEY": supplied_secret},
        )

    assert plugins_response.status_code == 200
    assert any(
        item["key"] == "termix" and item["restore_capability"] == "partial"
        for item in plugins_response.json()
    )
    assert schema_response.status_code == 200
    assert schema_response.json()["required"] == ["data_path"]
    assert set(schema_response.json()["properties"]) == {"data_path"}
    assert test_response.json() == {"ok": True}
    assert invalid_response.json()["ok"] is False
    assert supplied_secret not in invalid_response.text


@pytest.mark.anyio
async def test_termix_backup_publishes_private_strict_archive_manifest_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )

    assert artifact_path.is_file()
    assert artifact_path.suffix == ".zip"
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert artifact_path.stat().st_size > 0

    expected_payloads = {
        ".env": (source_path / ".env").read_bytes(),
        "db.sqlite.encrypted": (source_path / "db.sqlite.encrypted").read_bytes(),
        ".opk/config.yml": (source_path / ".opk" / "config.yml").read_bytes(),
    }
    with zipfile.ZipFile(artifact_path) as archive:
        members = archive.infolist()
        assert {member.filename for member in members} == {
            "manifest.json",
            ".env",
            "db.sqlite.encrypted",
            ".opk/config.yml",
        }
        assert archive.testzip() is None
        for member in members:
            mode = member.external_attr >> 16
            assert stat.S_ISREG(mode)
            assert stat.S_IMODE(mode) == 0o600
        manifest = json.loads(archive.read("manifest.json"))
        for name, expected in expected_payloads.items():
            assert archive.read(name) == expected

    assert manifest == {
        "format_version": 1,
        "plugin": "termix",
        "termix_version": "2.3.2",
        "termix_commit": TERMIX_COMMIT,
        "files": {
            name: {
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mode": 0o600,
            }
            for name, payload in expected_payloads.items()
        },
    }

    sidecar_path = Path(f"{artifact_path}.meta.json")
    assert sidecar_path.is_file()
    assert not sidecar_path.is_symlink()
    sidecar = read_backup_sidecar(str(artifact_path))
    assert sidecar is not None
    assert sidecar["plugin_name"] == plugin.name
    assert sidecar["target_slug"] == "termix-source"
    assert Path(sidecar["artifact_path"]) == artifact_path
    assert DATABASE_KEY_HEX not in sidecar_path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_termix_backup_retries_one_change_and_captures_the_stable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    opk_config = source_path / ".opk" / "config.yml"
    opk_config.write_text("revision: before\n", encoding="utf-8")
    final_payload = b"revision: stable-after-retry\n"
    termix_module = _termix_plugin_module()
    monkeypatch.setattr(termix_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = _termix_plugin_class()(name="termix")
    original_start = termix_module._start_backup_process
    mutation_threads: list[threading.Thread] = []

    def start_with_mutation_spanning_first_observation(
        data_path: Path,
        archive_path: Path,
        validation_root: Path,
    ) -> tuple[Any, Any]:
        if mutation_threads:
            mutation_threads[0].join(5)
        process, connection = original_start(data_path, archive_path, validation_root)

        if mutation_threads:
            return process, connection

        def mutate_through_first_settle() -> None:
            deadline = time.monotonic() + termix_module._STABLE_SETTLE_SECONDS + 0.75
            revision = 0
            while time.monotonic() < deadline:
                opk_config.write_text(f"revision: changing-{revision}\n", encoding="utf-8")
                revision += 1
                time.sleep(0.02)
            opk_config.write_bytes(final_payload)

        mutation = threading.Thread(target=mutate_through_first_settle, daemon=True)
        mutation.start()
        mutation_threads.append(mutation)
        return process, connection

    monkeypatch.setattr(
        termix_module, "_start_backup_process", start_with_mutation_spanning_first_observation
    )
    result = await asyncio.wait_for(
        plugin.backup(_backup_context(source_path, target_slug="stable-retry")),
        timeout=15,
    )
    for mutation in mutation_threads:
        await asyncio.to_thread(mutation.join, 5)
        assert not mutation.is_alive()

    with zipfile.ZipFile(result["artifact_path"]) as archive:
        assert archive.read(".opk/config.yml") == final_payload


@pytest.mark.anyio
async def test_termix_backup_refuses_a_source_that_never_stabilizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    opk_config = source_path / ".opk" / "config.yml"
    monkeypatch.setattr(_termix_plugin_module(), "BACKUP_BASE_PATH", str(backup_root))
    plugin = _termix_plugin_class()(name="termix")
    stop = asyncio.Event()

    async def keep_mutating() -> None:
        revision = 0
        while not stop.is_set():
            opk_config.write_text(f"revision: {revision}\n", encoding="utf-8")
            revision += 1
            await asyncio.sleep(0.05)

    mutation = asyncio.create_task(keep_mutating())
    try:
        with pytest.raises(RuntimeError, match="stable|changing|changed"):
            await asyncio.wait_for(
                plugin.backup(_backup_context(source_path, target_slug="never-stable")),
                timeout=12,
            )
    finally:
        stop.set()
        await mutation

    assert not [path for path in backup_root.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_termix_backup_cancellation_leaves_no_sensitive_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    monkeypatch.setattr(_termix_plugin_module(), "BACKUP_BASE_PATH", str(backup_root))
    plugin = _termix_plugin_class()(name="termix")

    task = asyncio.create_task(plugin.backup(_backup_context(source_path, target_slug="cancelled")))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)

    assert not [path for path in backup_root.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_termix_backup_timeout_reaps_worker_and_removes_parent_owned_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    termix_module = _termix_plugin_module()
    monkeypatch.setattr(termix_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(termix_module, "_BACKUP_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess()
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_backup(
        _data_path: Path,
        archive_path: Path,
        validation_root: Path,
    ) -> tuple[Any, Any]:
        validation_roots.append(validation_root)
        (validation_root / "decrypted-secret.db").write_bytes(b"sensitive partial")
        descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as partial:
            partial.write(b"sensitive partial archive")
        return process, connection

    monkeypatch.setattr(termix_module, "_start_backup_process", start_blocked_backup)
    plugin = _termix_plugin_class()(name="termix")

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.backup(_backup_context(source_path, target_slug="timeout"))

    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in backup_root.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_termix_validation_timeout_removes_decrypted_parent_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    _write_termix_source(source_path)
    termix_module = _termix_plugin_module()
    monkeypatch.setattr(termix_module, "_VALIDATION_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess()
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_validation(
        _data_path: Path,
        validation_root: Path,
    ) -> tuple[Any, Any]:
        validation_roots.append(validation_root)
        (validation_root / "decrypted-secret.db").write_bytes(b"decrypted secret")
        return process, connection

    monkeypatch.setattr(termix_module, "_start_validation_process", start_blocked_validation)
    plugin = _termix_plugin_class()(name="termix")

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.test({"data_path": str(source_path)})

    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)


@pytest.mark.anyio
async def test_termix_validation_repeated_cancellation_waits_for_stop_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    _write_termix_source(source_path)
    termix_module = _termix_plugin_module()
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_validation(
        _data_path: Path,
        validation_root: Path,
    ) -> tuple[Any, Any]:
        validation_roots.append(validation_root)
        (validation_root / "decrypted-secret.db").write_bytes(b"decrypted secret")
        return process, connection

    monkeypatch.setattr(termix_module, "_start_validation_process", start_blocked_validation)
    plugin = _termix_plugin_class()(name="termix")
    task = asyncio.create_task(plugin.test({"data_path": str(source_path)}))
    assert await asyncio.to_thread(process.join_started.wait, 2)

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)


@pytest.mark.anyio
async def test_termix_backup_artifact_is_private_from_first_observable_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    termix_module = _termix_plugin_module()
    monkeypatch.setattr(termix_module, "BACKUP_BASE_PATH", str(backup_root))
    original_start = termix_module._start_backup_process
    observed_modes: list[int] = []

    def start_and_observe_initial_mode(
        data_path: Path,
        archive_path: Path,
        validation_root: Path,
    ) -> tuple[Any, Any]:
        process, connection = original_start(data_path, archive_path, validation_root)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if archive_path.exists():
                observed_modes.append(stat.S_IMODE(archive_path.stat().st_mode))
                break
            if not process.is_alive():
                break
            time.sleep(0.005)
        return process, connection

    monkeypatch.setattr(termix_module, "_start_backup_process", start_and_observe_initial_mode)
    plugin = _termix_plugin_class()(name="termix")

    artifact_path = Path(
        (await plugin.backup(_backup_context(source_path, target_slug="private-from-open")))[
            "artifact_path"
        ]
    )

    assert observed_modes == [0o600]
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600


@pytest.mark.anyio
async def test_termix_backup_creates_two_distinct_valid_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source-data"
    backup_root = tmp_path / "backups"
    _write_termix_source(source_path)
    monkeypatch.setattr(_termix_plugin_module(), "BACKUP_BASE_PATH", str(backup_root))
    plugin = _termix_plugin_class()(name="termix")
    context = _backup_context(source_path, target_slug="consecutive")

    first = Path((await plugin.backup(context))["artifact_path"])
    second = Path((await plugin.backup(context))["artifact_path"])

    assert first != second
    for artifact_path in (first, second):
        assert artifact_path.is_file()
        assert artifact_path.stat().st_size > 0
        assert Path(f"{artifact_path}.meta.json").is_file()
        with zipfile.ZipFile(artifact_path) as archive:
            assert archive.testzip() is None


@pytest.mark.anyio
async def test_termix_restore_materializes_fresh_private_state_and_revalidates_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path)

    result = await plugin.restore(_restore_context(artifact_path, destination_path))

    assert result["status"] == "partial"
    assert result["restored_path"] == str(destination_path)
    assert "isolated" in result["message"]
    assert "2.3.2" in result["message"]
    assert destination_path.is_dir()
    assert stat.S_IMODE(destination_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination_path / ".opk").stat().st_mode) == 0o700
    for relative_path in (".env", "db.sqlite.encrypted", ".opk/config.yml"):
        restored = destination_path / relative_path
        assert restored.read_bytes() == (source_path / relative_path).read_bytes()
        assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert not (destination_path / "opkssh").exists()
    assert not (destination_path / "uploads").exists()
    assert not (destination_path / ".temp").exists()
    assert await plugin.test({"data_path": str(destination_path)}) is True
    assert {item.name for item in destination_path.parent.iterdir()} == {
        RESTORE_SENTINEL_NAME,
        destination_path.name,
    }


@pytest.mark.anyio
async def test_termix_restore_rejects_malformed_incomplete_and_malicious_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    variants = (
        "bad-zip",
        "missing-manifest",
        "invalid-json",
        "missing-env",
        "missing-database",
        "format-version",
        "wrong-plugin",
        "wrong-version",
        "wrong-commit",
        "digest",
        "size",
        "corrupt-payload",
        "symlink-member",
        "traversal-member",
        "absolute-member",
        "extra-member",
        "duplicate-manifest",
        "public-artifact",
    )

    for variant in variants:
        tampered = tmp_path / "tampered" / f"{variant}.zip"
        _write_tampered_archive(artifact_path, tampered, variant)
        destination_path = _fresh_restore_path(tmp_path, name=f"restore-{variant}")

        with pytest.raises((PermissionError, RuntimeError, ValueError)):
            await plugin.restore(_restore_context(tampered, destination_path))
        assert not destination_path.exists()
        assert {item.name for item in destination_path.parent.iterdir()} == {RESTORE_SENTINEL_NAME}

    linked_artifact = tmp_path / "tampered" / "linked.zip"
    linked_artifact.symlink_to(artifact_path)
    linked_destination = _fresh_restore_path(tmp_path, name="restore-linked")
    with pytest.raises((PermissionError, RuntimeError, ValueError)):
        await plugin.restore(_restore_context(linked_artifact, linked_destination))
    assert not linked_destination.exists()


@pytest.mark.anyio
async def test_termix_restore_refuses_collision_and_preserves_foreign_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path)
    destination_path.mkdir()
    foreign_file = destination_path / "foreign-state"
    foreign_file.write_bytes(b"must-survive")

    with pytest.raises(ValueError, match="exist|collision|fresh"):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert foreign_file.read_bytes() == b"must-survive"
    assert not [
        path
        for path in destination_path.parent.iterdir()
        if path.name not in {RESTORE_SENTINEL_NAME, destination_path.name}
    ]


@pytest.mark.anyio
async def test_termix_restore_refuses_missing_or_nonexclusive_sentinel_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )

    missing_sentinel = tmp_path / "missing-sentinel" / "data"
    missing_sentinel.parent.mkdir()
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.restore(_restore_context(artifact_path, missing_sentinel))

    wrong_sentinel = _fresh_restore_path(tmp_path, name="wrong-sentinel")
    (wrong_sentinel.parent / RESTORE_SENTINEL_NAME).write_text(
        "wrong marker\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.restore(_restore_context(artifact_path, wrong_sentinel))

    nonexclusive = _fresh_restore_path(tmp_path, name="nonexclusive")
    extra = nonexclusive.parent / "foreign-state"
    extra.write_bytes(b"keep")
    with pytest.raises(ValueError, match="empty|sentinel|foreign"):
        await plugin.restore(_restore_context(artifact_path, nonexclusive))
    assert extra.read_bytes() == b"keep"


@pytest.mark.anyio
async def test_termix_restore_refuses_forbidden_overlap_and_symlink_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )

    for forbidden_path in (
        Path("/backups/termix-restore/data"),
        Path("/sources/termix/restore-data"),
        Path("/app/data"),
    ):
        with pytest.raises(ValueError, match="forbidden|backup|source|live"):
            await plugin.restore(_restore_context(artifact_path, forbidden_path))

    backup_root = Path(_termix_plugin_module().BACKUP_BASE_PATH)
    overlap_parent = backup_root / "overlap"
    overlap_parent.mkdir(parents=True)
    (overlap_parent / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlap|forbidden|backup"):
        await plugin.restore(_restore_context(artifact_path, overlap_parent / "data"))

    actual_parent = tmp_path / "actual-restore"
    actual_parent.mkdir()
    (actual_parent / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    linked_parent = tmp_path / "linked-restore"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic|symlink"):
        await plugin.restore(_restore_context(artifact_path, linked_parent / "data"))


@pytest.mark.anyio
async def test_termix_restore_timeout_reaps_worker_and_removes_secret_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path, name="restore-timeout")
    termix_module = _termix_plugin_module()
    monkeypatch.setattr(termix_module, "_RESTORE_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess()
    connection = _NoResultConnection()
    owned_paths: list[Path] = []

    def start_blocked_restore(
        _artifact_path: Path,
        parent_path: Path,
        _expected_parent_identity: tuple[int, int],
        staging_name: str,
        _expected_staging_identity: tuple[int, int],
        _destination_name: str,
        validation_name: str,
        _expected_validation_identity: tuple[int, int],
    ) -> tuple[Any, Any]:
        staging_path = parent_path / staging_name
        validation_path = parent_path / validation_name
        (staging_path / ".env").write_bytes(b"restored secret")
        (validation_path / "decrypted.db").write_bytes(b"decrypted secret")
        owned_paths.extend((staging_path, validation_path))
        return process, connection

    monkeypatch.setattr(termix_module, "_start_restore_process", start_blocked_restore)

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert owned_paths and all(not path.exists() for path in owned_paths)
    _assert_restore_parent_contains_only_sentinel(destination_path)


@pytest.mark.anyio
async def test_termix_restore_repeated_cancellation_waits_for_stop_and_cleans_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path, name="restore-cancelled")
    termix_module = _termix_plugin_module()
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    owned_paths: list[Path] = []

    def start_blocked_restore(
        _artifact_path: Path,
        parent_path: Path,
        _expected_parent_identity: tuple[int, int],
        staging_name: str,
        _expected_staging_identity: tuple[int, int],
        _destination_name: str,
        validation_name: str,
        _expected_validation_identity: tuple[int, int],
    ) -> tuple[Any, Any]:
        staging_path = parent_path / staging_name
        validation_path = parent_path / validation_name
        (staging_path / ".env").write_bytes(b"restored secret")
        (validation_path / "decrypted.db").write_bytes(b"decrypted secret")
        owned_paths.extend((staging_path, validation_path))
        return process, connection

    monkeypatch.setattr(termix_module, "_start_restore_process", start_blocked_restore)
    task = asyncio.create_task(plugin.restore(_restore_context(artifact_path, destination_path)))
    assert await asyncio.to_thread(process.join_started.wait, 2)

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert owned_paths and all(not path.exists() for path in owned_paths)
    _assert_restore_parent_contains_only_sentinel(destination_path)


@pytest.mark.anyio
async def test_termix_restore_refuses_parent_swapped_to_symlink_before_child_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path, name="restore-parent-swap")
    original_parent = destination_path.parent
    moved_parent = tmp_path / "restore-parent-owned"
    foreign_parent = tmp_path / "restore-parent-foreign"
    foreign_parent.mkdir()
    foreign_state = foreign_parent / "foreign-state"
    foreign_state.write_bytes(b"must survive")
    termix_module = _termix_plugin_module()
    original_start = termix_module._start_restore_process

    def swap_parent_then_start(
        artifact: Path,
        parent_path: Path,
        expected_parent_identity: tuple[int, int],
        staging_name: str,
        expected_staging_identity: tuple[int, int],
        destination_name: str,
        validation_name: str,
        expected_validation_identity: tuple[int, int],
    ) -> tuple[Any, Any]:
        parent_path.rename(moved_parent)
        parent_path.symlink_to(foreign_parent, target_is_directory=True)
        return cast(
            tuple[Any, Any],
            original_start(
                artifact,
                parent_path,
                expected_parent_identity,
                staging_name,
                expected_staging_identity,
                destination_name,
                validation_name,
                expected_validation_identity,
            ),
        )

    monkeypatch.setattr(termix_module, "_start_restore_process", swap_parent_then_start)

    with pytest.raises(
        (RuntimeError, ValueError),
        match="changed|failed|unsafe|Not a directory",
    ):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert original_parent.is_symlink()
    assert foreign_state.read_bytes() == b"must survive"
    assert not (foreign_parent / destination_path.name).exists()
    assert {entry.name for entry in moved_parent.iterdir()} == {RESTORE_SENTINEL_NAME}


@pytest.mark.anyio
async def test_termix_restore_real_worker_refuses_replaced_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path, name="restore-staging-swap")
    owned_relocated = destination_path.parent / "owned-staging-relocated"
    replacement_identity: tuple[int, int] | None = None
    replacement_path: Path | None = None
    termix_module = _termix_plugin_module()
    original_start = termix_module._start_restore_process

    def replace_staging_then_start(
        artifact: Path,
        parent_path: Path,
        expected_parent_identity: tuple[int, int],
        staging_name: str,
        expected_staging_identity: tuple[int, int],
        destination_name: str,
        validation_name: str,
        expected_validation_identity: tuple[int, int],
    ) -> tuple[Any, Any]:
        nonlocal replacement_identity, replacement_path
        staging_path = parent_path / staging_name
        staging_path.rename(owned_relocated)
        staging_path.mkdir(mode=0o700)
        replacement_path = staging_path
        replacement_stat = staging_path.stat()
        replacement_identity = (replacement_stat.st_dev, replacement_stat.st_ino)
        return cast(
            tuple[Any, Any],
            original_start(
                artifact,
                parent_path,
                expected_parent_identity,
                staging_name,
                expected_staging_identity,
                destination_name,
                validation_name,
                expected_validation_identity,
            ),
        )

    monkeypatch.setattr(
        termix_module,
        "_start_restore_process",
        replace_staging_then_start,
    )

    with pytest.raises(ValueError, match="staging directory changed"):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert replacement_path is not None
    replacement_stat = replacement_path.stat()
    assert (replacement_stat.st_dev, replacement_stat.st_ino) == replacement_identity
    assert list(replacement_path.iterdir()) == []
    assert owned_relocated.is_dir()
    assert list(owned_relocated.iterdir()) == []
    assert not destination_path.exists()


@pytest.mark.anyio
async def test_termix_restore_refuses_false_success_after_publication_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _source_path = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    destination_path = _fresh_restore_path(tmp_path, name="restore-publish-race")
    owned_relocated = destination_path.parent / "owned-output-relocated"
    foreign_state = destination_path / "foreign-state"
    termix_module = _termix_plugin_module()

    def publish_then_replace_before_success(
        _artifact_path: Path,
        parent_path: Path,
        _expected_parent_identity: tuple[int, int],
        staging_name: str,
        _expected_staging_identity: tuple[int, int],
        destination_name: str,
        _validation_name: str,
        _expected_validation_identity: tuple[int, int],
    ) -> tuple[Any, Any]:
        staging_path = parent_path / staging_name
        (staging_path / ".env").write_bytes(b"owned restored secret")
        staging_path.rename(parent_path / destination_name)
        (parent_path / destination_name).rename(owned_relocated)
        (parent_path / destination_name).mkdir()
        foreign_state.write_bytes(b"must survive")
        process = _CompletedProcess()
        process.exitcode = 0
        return process, _ResultConnection(("ok", ""))

    monkeypatch.setattr(
        termix_module,
        "_start_restore_process",
        publish_then_replace_before_success,
    )

    with pytest.raises(ValueError, match="restore-owned directory changed"):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert foreign_state.read_bytes() == b"must survive"
    assert owned_relocated.is_dir()
    assert list(owned_relocated.iterdir()) == []
    assert {entry.name for entry in destination_path.parent.iterdir()} == {
        RESTORE_SENTINEL_NAME,
        destination_path.name,
        owned_relocated.name,
    }


@pytest.mark.anyio
async def test_termix_get_status_reports_only_observed_health(tmp_path: Path) -> None:
    source_path = tmp_path / "source-data"
    _write_termix_source(source_path)
    plugin = _termix_plugin_class()(name="termix")

    assert await plugin.get_status(_backup_context(source_path)) == {"status": "ok"}

    (source_path / "db.sqlite.encrypted").write_bytes(b"corrupt")
    assert await plugin.get_status(_backup_context(source_path)) == {"status": "unknown"}
    assert await plugin.get_status(
        BackupContext(
            job_id="status-invalid",
            target_id="1",
            config={"data_path": "relative/data"},
        )
    ) == {"status": "unknown"}
