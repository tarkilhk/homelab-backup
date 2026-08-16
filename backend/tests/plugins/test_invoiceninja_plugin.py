import asyncio
import hashlib
import io
import json
import os
import pickle
import stat
import threading
import warnings
import zipfile
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar, write_backup_sidecar
from app.core.scheduler import _perform_target_run
from app.models import Job, Run, Tag, Target, TargetRun
from app.plugins.invoiceninja import plugin as plugin_module
from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin
from app.services.restores import RestoreService
from app.services.targets import TargetService

INVOICE_NINJA_ARRAY_FIELDS = (
    "activities",
    "backups",
    "users",
    "client_contacts",
    "client_gateway_tokens",
    "clients",
    "company_gateways",
    "company_tokens",
    "company_ledger",
    "company_users",
    "credits",
    "credit_invitations",
    "designs",
    "documents",
    "expense_categories",
    "expenses",
    "group_settings",
    "invoices",
    "invoice_invitations",
    "payment_terms",
    "payments",
    "products",
    "projects",
    "quotes",
    "quote_invitations",
    "recurring_expenses",
    "recurring_invoices",
    "recurring_invoice_invitations",
    "subscriptions",
    "system_logs",
    "tasks",
    "task_statuses",
    "tax_rates",
    "vendors",
    "vendor_contacts",
    "webhooks",
    "purchase_orders",
    "purchase_order_invitations",
    "bank_integrations",
    "bank_transactions",
    "schedulers",
    "e_invoicing_tokens",
    "locations",
)
DOCUMENT_URL = "synthetic-company/documents/document-hash.txt"
DOCUMENT_BYTES = b"synthetic invoice document"
SIGNED_EXPORT_UUID = "123e4567-e89b-42d3-a456-426614174000"
RESTORE_COMPANY_MARKER = "private-company-marker-must-not-escape"
RESTORE_CLIENT_MARKER = "private-client-marker-must-not-escape"
RESTORE_INVOICE_MARKER = "private-invoice-marker-must-not-escape"
RESTORE_CLIENT_SOURCE_ID = "private-client-source-id"
RESTORE_CLIENT_ID_NUMBER = "private-client-id-number"
RESTORE_CLIENT_EMAIL = "private-client@example.test"
RESTORE_INVOICE_PUBLIC_NOTES = "private-public-note"
RESTORE_INVOICE_PRIVATE_NOTES = "private-private-note"
RESTORE_INVOICE_PRODUCT_KEY = "private-product-key"
RESTORE_INVOICE_LINE_NOTES = "private-line-note"
RESTORE_SOURCE_ORIGIN = "https://invoice-source.local"
RESTORE_DESTINATION_ORIGIN = "https://invoice-restore.local"
RESTORE_RESOURCE_PATHS = tuple(
    f"/api/v1/{resource}"
    for resource in (
        "clients",
        "invoices",
        "payments",
        "projects",
        "quotes",
        "expenses",
        "vendors",
        "products",
        "tasks",
        "documents",
    )
)


def _exact_backup_json() -> dict[str, object]:
    payload: dict[str, object] = {field: [] for field in INVOICE_NINJA_ARRAY_FIELDS}
    payload.update(
        {
            "app_version": "5.13.31",
            "storage_url": "http://invoice.local/storage",
            "company": {"settings": {"name": RESTORE_COMPANY_MARKER}},
        }
    )
    payload["documents"] = [
        {
            "url": DOCUMENT_URL,
            # Invoice Ninja's vendor `hash` is the stored filename, not a digest.
            "hash": Path(DOCUMENT_URL).name,
            "size": len(DOCUMENT_BYTES),
        }
    ]
    payload["clients"] = [
        {
            "id": 1,
            "hashed_id": RESTORE_CLIENT_SOURCE_ID,
            "name": RESTORE_CLIENT_MARKER,
            "id_number": RESTORE_CLIENT_ID_NUMBER,
        }
    ]
    payload["client_contacts"] = [
        {
            "client_id": RESTORE_CLIENT_SOURCE_ID,
            "email": RESTORE_CLIENT_EMAIL,
        }
    ]
    payload["invoices"] = [
        {
            "id": "private-invoice-id",
            "client_id": RESTORE_CLIENT_SOURCE_ID,
            "number": RESTORE_INVOICE_MARKER,
            "public_notes": RESTORE_INVOICE_PUBLIC_NOTES,
            "private_notes": RESTORE_INVOICE_PRIVATE_NOTES,
            "line_items": [
                {
                    "product_key": RESTORE_INVOICE_PRODUCT_KEY,
                    "notes": RESTORE_INVOICE_LINE_NOTES,
                }
            ],
        }
    ]
    payload["backups"] = [{"filename": "previous-export.zip"}]
    return payload


def _private_zip_member(
    name: str,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    file_type: int = stat.S_IFREG,
) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (file_type | 0o600) << 16
    member.compress_type = compression
    return member


def _exact_export_members(
    backup_json: object | None = None,
) -> list[tuple[zipfile.ZipInfo | str, bytes]]:
    return [
        (
            "backup.json",
            json.dumps(
                _exact_backup_json() if backup_json is None else backup_json,
                sort_keys=True,
            ).encode(),
        ),
        ("company_logo.png", b"synthetic-logo"),
        (f"documents/{DOCUMENT_URL}", DOCUMENT_BYTES),
        ("backups/previous-export.zip", b"synthetic-previous-export"),
    ]


def _invoice_export_zip(
    *,
    backup_json: object | None = None,
    members: list[tuple[zipfile.ZipInfo | str, bytes]] | None = None,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for member, content in members or _exact_export_members(backup_json):
                if isinstance(member, str):
                    member = _private_zip_member(member)
                archive.writestr(member, content)
    return payload.getvalue()


def _patch_first_zip_flag(payload: bytes, mask: int) -> bytes:
    mutated = bytearray(payload)
    local = mutated.index(b"PK\x03\x04")
    central = mutated.index(b"PK\x01\x02")
    local_flags = int.from_bytes(mutated[local + 6 : local + 8], "little") | mask
    central_flags = int.from_bytes(mutated[central + 8 : central + 10], "little") | mask
    mutated[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
    mutated[central + 8 : central + 10] = central_flags.to_bytes(2, "little")
    return bytes(mutated)


def _strict_archive_case(case: str, monkeypatch: pytest.MonkeyPatch) -> bytes:
    backup_json = _exact_backup_json()
    members = _exact_export_members(backup_json)

    if case == "unexpected-root":
        members.append(("unexpected.txt", b"not part of a native export"))
    elif case == "absolute-path":
        members.append(("/absolute.txt", b"unsafe"))
    elif case == "traversal-path":
        members.append(("documents/../escape.txt", b"unsafe"))
    elif case == "duplicate-name":
        members.append(("backup.json", members[0][1]))
    elif case == "case-collision":
        members.append(("BACKUP.JSON", members[0][1]))
    elif case in {"link-member", "special-member"}:
        file_type = stat.S_IFLNK if case == "link-member" else stat.S_IFCHR
        members[1] = (
            _private_zip_member("company_logo.png", file_type=file_type),
            b"unsafe",
        )
    elif case == "unsupported-compression":
        members[1] = (
            _private_zip_member("company_logo.png", compression=zipfile.ZIP_BZIP2),
            b"unsupported",
        )
    elif case in {
        "wrong-version",
        "missing-field",
        "wrong-company-type",
        "wrong-storage-type",
        "wrong-array-type",
        "unexpected-json-field",
        "unsafe-document-url",
        "wrong-document-size",
    }:
        if case == "wrong-version":
            backup_json["app_version"] = "5.13.30"
        elif case == "missing-field":
            backup_json.pop("activities")
        elif case == "wrong-company-type":
            backup_json["company"] = []
        elif case == "wrong-storage-type":
            backup_json["storage_url"] = 123
        elif case == "wrong-array-type":
            backup_json["invoices"] = {}
        elif case == "unexpected-json-field":
            backup_json["legacy"] = True
        elif case == "unsafe-document-url":
            document = dict(backup_json["documents"][0])  # type: ignore[index]
            document["url"] = "../private/customer.txt"
            backup_json["documents"] = [document]
        elif case == "wrong-document-size":
            document = dict(backup_json["documents"][0])  # type: ignore[index]
            document["size"] = len(DOCUMENT_BYTES) + 1
            backup_json["documents"] = [document]
        members = _exact_export_members(backup_json)
    elif case == "missing-document-member":
        members = [item for item in members if item[0] != f"documents/{DOCUMENT_URL}"]
    elif case == "duplicate-document-member":
        members.append((f"documents/{DOCUMENT_URL}", DOCUMENT_BYTES))
    elif case == "document-link":
        members[2] = (
            _private_zip_member(
                f"documents/{DOCUMENT_URL}",
                file_type=stat.S_IFLNK,
            ),
            DOCUMENT_BYTES,
        )
    elif case == "archive-size-limit":
        monkeypatch.setattr(plugin_module, "_MAX_ARCHIVE_BYTES", 1, raising=False)
    elif case == "member-count-limit":
        monkeypatch.setattr(plugin_module, "_MAX_ARCHIVE_MEMBERS", 3, raising=False)
    elif case == "member-compressed-limit":
        monkeypatch.setattr(plugin_module, "_MAX_MEMBER_COMPRESSED_BYTES", 1, raising=False)
    elif case == "member-expanded-limit":
        monkeypatch.setattr(plugin_module, "_MAX_MEMBER_EXPANDED_BYTES", 1, raising=False)
    elif case == "total-compressed-limit":
        monkeypatch.setattr(plugin_module, "_MAX_TOTAL_COMPRESSED_BYTES", 1, raising=False)
    elif case == "total-expanded-limit":
        monkeypatch.setattr(plugin_module, "_MAX_TOTAL_EXPANDED_BYTES", 1, raising=False)
    elif case == "ratio-limit":
        monkeypatch.setattr(plugin_module, "_MAX_EXPANSION_RATIO", 1.0, raising=False)
    elif case == "path-depth-limit":
        monkeypatch.setattr(plugin_module, "_MAX_MEMBER_PATH_DEPTH", 3, raising=False)
    elif case == "backup-json-size-limit":
        monkeypatch.setattr(plugin_module, "_MAX_BACKUP_JSON_BYTES", 1, raising=False)
    elif case == "document-size-limit":
        monkeypatch.setattr(plugin_module, "_MAX_DOCUMENT_BYTES", 1, raising=False)

    payload = _invoice_export_zip(members=members)
    if case == "encrypted-member":
        return _patch_first_zip_flag(payload, 0x1)
    if case == "crc-error":
        corrupted = bytearray(payload)
        local = corrupted.index(b"PK\x03\x04")
        central = corrupted.index(b"PK\x01\x02")
        corrupted[local + 14 : local + 18] = b"\x00" * 4
        corrupted[central + 16 : central + 20] = b"\x00" * 4
        return bytes(corrupted)
    if case == "trailing-data":
        return payload + b"ambiguous-secret-trailer"
    return payload


def _company_export_zip() -> bytes:
    return _invoice_export_zip()


def _streaming_export_zip() -> bytes:
    payload = _exact_backup_json()
    payload["company"] = {"settings": {"name": "private-content-must-not-escape"}}
    return _invoice_export_zip(backup_json=payload)


def _restore_preflight_context(
    artifact: Path,
    *,
    config: dict[str, object] | None = None,
    source_origin: str | None = RESTORE_SOURCE_ORIGIN,
    metadata_overrides: dict[str, object] | None = None,
) -> RestoreContext:
    artifact_payload = artifact.read_bytes()
    metadata: dict[str, object] = {
        "artifact_bytes": len(artifact_payload),
        "artifact_sha256": hashlib.sha256(artifact_payload).hexdigest(),
    }
    if source_origin is not None:
        metadata["source_database_identity"] = {"base_url": source_origin}
    if metadata_overrides:
        metadata.update(metadata_overrides)
    return RestoreContext(
        job_id="invoice-restore-preflight",
        source_target_id="invoice-source",
        destination_target_id="invoice-isolated-destination",
        config=config
        or {
            "base_url": RESTORE_DESTINATION_ORIGIN,
            "token": "destination-token-must-not-escape",
        },
        artifact_path=str(artifact),
        metadata=metadata,
    )


def _authorize_invoice_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        "https://INVOICE-RESTORE.local:443",
    )


def _install_immediate_restore_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    validation_path: Path | None = None

    def record_validation_path(path: Path) -> tuple[object, object]:
        nonlocal validation_path
        validation_path = path
        return object(), object()

    async def return_valid_evidence(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert validation_path is not None
        status = validation_path.stat()
        payload = validation_path.read_bytes()
        return {
            "member_count": InvoiceNinjaPlugin("invoiceninja")._validate_export(validation_path),
            "size_bytes": status.st_size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "device": status.st_dev,
            "inode": status.st_ino,
            "markers": plugin_module._restore_markers(validation_path),
        }

    monkeypatch.setattr(plugin_module, "_start_validation_process", record_validation_path)
    monkeypatch.setattr(plugin_module, "_await_validation_process", return_valid_evidence)


def _create_restore_service_records(
    db_session: Session,
    tmp_path: Path,
) -> tuple[Target, Target, Run, TargetRun, Path]:
    source = Target(
        name="Invoice Ninja source",
        slug="invoice-source",
        plugin_name="invoiceninja",
        plugin_config_json=json.dumps(
            {"base_url": RESTORE_SOURCE_ORIGIN, "token": "source-token-must-not-escape"}
        ),
    )
    destination = Target(
        name="Invoice Ninja isolated destination",
        slug="invoice-restore",
        plugin_name="invoiceninja",
        plugin_config_json=json.dumps(
            {
                "base_url": RESTORE_DESTINATION_ORIGIN,
                "token": "destination-token-must-not-escape",
            }
        ),
    )
    db_session.add_all((source, destination))
    db_session.flush()
    artifact_dir = tmp_path / "backups" / source.slug / "2026-08-16"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "invoiceninja-export.zip"
    artifact.write_bytes(_invoice_export_zip())
    artifact.chmod(0o600)
    plugin = InvoiceNinjaPlugin("invoiceninja")
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(
            job_id="source-backup",
            target_id=str(source.id),
            config=json.loads(source.plugin_config_json or "{}"),
            metadata={"target_slug": source.slug},
        ),
    )
    source_run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.flush()
    payload = artifact.read_bytes()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_identity_json=json.dumps({"base_url": RESTORE_SOURCE_ORIGIN}),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()
    return source, destination, source_run, source_target_run, artifact


def _install_successful_restore_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    uploaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        requests.append(request)
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        if request.url.path == "/api/v1/clients":
            return _resource_response([_restored_client_row()])
        if request.url.path == "/api/v1/invoices":
            return _resource_response([_restored_invoice_row()])
        raise AssertionError(f"Unexpected restore request: {request.url.path}")

    _install_transport(monkeypatch, handler)
    return requests


def _resource_response(data: list[object] | None = None) -> httpx.Response:
    rows = [] if data is None else data
    return httpx.Response(
        200,
        json={
            "data": rows,
            "meta": {
                "pagination": {
                    "total": len(rows),
                    "count": len(rows),
                    "per_page": 20,
                    "current_page": 1,
                    "total_pages": 1,
                    "links": [],
                }
            },
        },
    )


def _empty_resource_response() -> httpx.Response:
    return _resource_response()


def _restored_client_row() -> dict[str, object]:
    return {
        "id": "private-client-id",
        "name": RESTORE_CLIENT_MARKER,
        "id_number": RESTORE_CLIENT_ID_NUMBER,
        "contacts": [{"email": RESTORE_CLIENT_EMAIL}],
    }


def _restored_invoice_row() -> dict[str, object]:
    return {
        "id": "private-invoice-id",
        "client_id": "private-client-id",
        "number": RESTORE_INVOICE_MARKER,
        "public_notes": RESTORE_INVOICE_PUBLIC_NOTES,
        "private_notes": RESTORE_INVOICE_PRIVATE_NOTES,
        "line_items": [
            {
                "product_key": RESTORE_INVOICE_PRODUCT_KEY,
                "notes": RESTORE_INVOICE_LINE_NOTES,
            }
        ],
    }


class _ObservedChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], backup_root: Path) -> None:
        self._chunks = chunks
        self._backup_root = backup_root
        self.first_write_modes: list[int] = []
        self.yielded_chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            self.yielded_chunks += 1
            yield chunk
            if index == 0:
                temporary_files = [
                    path
                    for path in self._backup_root.rglob("*.tmp")
                    if path.is_file() and ".meta.json." not in path.name
                ]
                assert len(temporary_files) == 1
                self.first_write_modes.append(stat.S_IMODE(temporary_files[0].stat().st_mode))

    async def aclose(self) -> None:
        self.closed = True


class _FailingChunkStream(httpx.AsyncByteStream):
    def __init__(self, error: httpx.HTTPError) -> None:
        self._error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial-sensitive-export"
        raise self._error

    async def aclose(self) -> None:
        self.closed = True


class _BlockingChunkStream(httpx.AsyncByteStream):
    def __init__(self, first_write_observed: asyncio.Event) -> None:
        self._first_write_observed = first_write_observed
        self._release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial-sensitive-export"
        self._first_write_observed.set()
        await self._release.wait()

    async def aclose(self) -> None:
        self.closed = True


class _NoValidationResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> bool:
        return False

    def recv(self) -> tuple[str, object]:
        raise AssertionError("No validation result is available")

    def close(self) -> None:
        self.closed = True


class _BlockingValidationProcess:
    def __init__(self) -> None:
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self._alive = True

    def join(self, timeout: float | None = None) -> None:
        self.join_started.set()
        released = self.release.wait(timeout)
        if released and self.terminate_called.is_set():
            self._alive = False
            self.exitcode = -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()

    def kill(self) -> None:
        self.kill_called.set()
        self.release.set()

    def close(self) -> None:
        assert not self._alive
        self.closed = True


def _backup_files(backup_root: Path) -> list[Path]:
    return [path for path in backup_root.rglob("*") if path.is_file()]


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _install_async_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> None:
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _install_ready_export_transport(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        if request.url.path == f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="company-export.zip"',
                },
                content=payload,
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    return requests


def _ping_response(*, version: str = "5.13.31") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"X-APP-VERSION": version},
        json={"company_name": RESTORE_COMPANY_MARKER, "user_name": "Synthetic user"},
    )


def test_exact_discovery_and_flat_schema_contract() -> None:
    plugin = get_plugin("invoiceninja")
    assert isinstance(plugin, InvoiceNinjaPlugin)
    assert plugin.restore_capability == "partial"

    loader_entry = next(item for item in list_plugins() if item["key"] == "invoiceninja")
    assert loader_entry == {
        "key": "invoiceninja",
        "name": "invoiceninja",
        "version": "0.2.1",
        "restore_capability": "partial",
    }
    schema_path = get_plugin_schema_path("invoiceninja")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["base_url", "token"],
        "properties": {
            "base_url": {
                "type": "string",
                "format": "uri",
                "title": "Base URL",
                "default": "http://invoiceninja.local",
                "minLength": 1,
                "pattern": r"^https?://[^/?#]+$",
            },
            "token": {
                "type": "string",
                "title": "API Token",
                "minLength": 1,
            },
            "export_timeout_seconds": {
                "type": "integer",
                "title": "Export timeout (seconds)",
                "default": 3300,
                "minimum": 60,
                "maximum": 3300,
            },
        },
    }
    assert "default" not in schema["properties"]["token"]
    assert InvoiceNinjaPlugin.__doc__
    assert plugin_module._LOGGER.name == "app.plugins.invoiceninja.plugin"
    for method_name in ("validate_config", "test", "backup", "restore", "get_status"):
        assert getattr(InvoiceNinjaPlugin, method_name).__doc__


@pytest.mark.asyncio
async def test_backup_emits_secret_safe_lifecycle_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = tmp_path / "synthetic-backup.zip"
    artifact.write_bytes(b"synthetic")
    secret = "backup-lifecycle-token-must-not-escape"
    context = BackupContext(
        job_id="backup-lifecycle-job",
        target_id="backup-lifecycle-target",
        config={"base_url": "https://invoice.local", "token": secret},
        metadata={"target_slug": "backup-lifecycle-target"},
    )

    async def succeed(
        _self: InvoiceNinjaPlugin,
        _context: BackupContext,
    ) -> dict[str, object]:
        return {"artifact_path": str(artifact)}

    caplog.set_level("INFO", logger=plugin_module._LOGGER.name)
    monkeypatch.setattr(InvoiceNinjaPlugin, "_backup_operation", succeed)
    assert await InvoiceNinjaPlugin("invoiceninja").backup(context) == {
        "artifact_path": str(artifact)
    }
    assert "invoiceninja_backup_start" in caplog.text
    assert "invoiceninja_backup_success" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert secret not in caplog.text

    caplog.clear()

    async def missing_artifact(
        _self: InvoiceNinjaPlugin,
        _context: BackupContext,
    ) -> dict[str, object]:
        return {"artifact_path": str(tmp_path / "disappeared.zip")}

    monkeypatch.setattr(InvoiceNinjaPlugin, "_backup_operation", missing_artifact)
    with pytest.raises(FileNotFoundError):
        await InvoiceNinjaPlugin("invoiceninja").backup(context)
    assert "invoiceninja_backup_start" in caplog.text
    assert "invoiceninja_backup_failure" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert secret not in caplog.text

    caplog.clear()

    async def fail(
        _self: InvoiceNinjaPlugin,
        _context: BackupContext,
    ) -> dict[str, object]:
        raise RuntimeError("synthetic backup failure")

    monkeypatch.setattr(InvoiceNinjaPlugin, "_backup_operation", fail)
    with pytest.raises(RuntimeError, match="synthetic backup failure"):
        await InvoiceNinjaPlugin("invoiceninja").backup(context)
    assert "invoiceninja_backup_start" in caplog.text
    assert "invoiceninja_backup_failure" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_restore_emits_secret_safe_lifecycle_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "restore-lifecycle-token-must-not-escape"
    context = RestoreContext(
        job_id="restore-lifecycle-job",
        source_target_id="restore-source",
        destination_target_id="restore-destination",
        config={"base_url": "https://invoice.local", "token": secret},
        artifact_path="/private/staged/invoice.zip",
        metadata={},
    )

    async def succeed(
        _self: InvoiceNinjaPlugin,
        _context: RestoreContext,
    ) -> dict[str, object]:
        return {"status": "partial"}

    caplog.set_level("INFO", logger=plugin_module._LOGGER.name)
    monkeypatch.setattr(InvoiceNinjaPlugin, "_restore_operation", succeed)
    assert await InvoiceNinjaPlugin("invoiceninja").restore(context) == {"status": "partial"}
    assert "invoiceninja_restore_start" in caplog.text
    assert "invoiceninja_restore_success" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert secret not in caplog.text
    assert context.artifact_path not in caplog.text

    caplog.clear()

    async def fail(
        _self: InvoiceNinjaPlugin,
        _context: RestoreContext,
    ) -> dict[str, object]:
        raise RuntimeError("synthetic restore failure")

    monkeypatch.setattr(InvoiceNinjaPlugin, "_restore_operation", fail)
    with pytest.raises(RuntimeError, match="synthetic restore failure"):
        await InvoiceNinjaPlugin("invoiceninja").restore(context)
    assert "invoiceninja_restore_start" in caplog.text
    assert "invoiceninja_restore_failure" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert secret not in caplog.text
    assert context.artifact_path not in caplog.text


def test_target_service_persists_only_exact_flat_configuration(
    db_session: Session,
) -> None:
    service = TargetService(db_session)
    exact_config = {
        "base_url": "http://invoiceninja.local",
        "token": "synthetic-token",
        "export_timeout_seconds": 3300,
    }
    serialized = json.dumps(exact_config, sort_keys=True)
    target = service.create(
        name="Invoice Ninja exact target",
        plugin_name="invoiceninja",
        plugin_config_json=serialized,
    )

    assert target.plugin_name == "invoiceninja"
    assert target.plugin_config_json == serialized

    invalid_target_configs = (
        {**exact_config, "legacy_url": "http://legacy.invalid"},
        {**exact_config, "base_url": "http://invoiceninja.local/api"},
        {**exact_config, "export_timeout_seconds": 3301},
    )
    for index, invalid_config in enumerate(invalid_target_configs):
        with pytest.raises(ValueError, match="Invalid plugin_config_json"):
            service.create(
                name=f"Invoice Ninja invalid target {index}",
                plugin_name="invoiceninja",
                plugin_config_json=json.dumps(invalid_config),
            )


@pytest.mark.asyncio
async def test_configuration_accepts_only_exact_flat_values() -> None:
    plugin = get_plugin("invoiceninja")
    exact_config = {
        "base_url": "https://invoiceninja.local:8443",
        "token": "synthetic-token",
    }

    assert await plugin.validate_config(exact_config) is True
    assert await plugin.validate_config({**exact_config, "export_timeout_seconds": 60}) is True
    assert await plugin.validate_config({**exact_config, "export_timeout_seconds": 3300}) is True

    invalid_configs: tuple[object, ...] = (
        None,
        [],
        {},
        {"base_url": exact_config["base_url"]},
        {"token": exact_config["token"]},
        {**exact_config, "base_url": ""},
        {**exact_config, "base_url": 123},
        {**exact_config, "base_url": "invoiceninja.local"},
        {**exact_config, "base_url": "ftp://invoiceninja.local"},
        {**exact_config, "base_url": "http://user:password@invoiceninja.local"},
        {**exact_config, "base_url": "http://invoiceninja.local/"},
        {**exact_config, "base_url": "http://invoiceninja.local/api"},
        {**exact_config, "base_url": "http://invoiceninja.local?token=value"},
        {**exact_config, "base_url": "http://invoiceninja.local#fragment"},
        {**exact_config, "token": ""},
        {**exact_config, "token": 123},
        {**exact_config, "export_timeout_seconds": 59},
        {**exact_config, "export_timeout_seconds": 3301},
        {**exact_config, "export_timeout_seconds": True},
        {**exact_config, "export_timeout_seconds": 60.0},
        {**exact_config, "export_timeout_seconds": "60"},
        {**exact_config, "legacy_url": "http://legacy.invalid"},
        {**exact_config, "api_key": "legacy-key"},
    )
    for invalid_config in invalid_configs:
        assert await plugin.validate_config(invalid_config) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ping_and_status_use_exact_authenticated_v51331_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    token = "synthetic-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url == httpx.URL("https://invoice.local:8443/api/v1/ping")
        assert request.headers["X-API-TOKEN"] == token
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(
            200,
            headers={"X-APP-VERSION": "5.13.31"},
            json={"company_name": "Synthetic company", "user_name": "Synthetic user"},
        )

    _install_transport(monkeypatch, handler)
    config = {"base_url": "https://invoice.local:8443", "token": token}
    plugin = get_plugin("invoiceninja")

    assert await plugin.test(config) is True
    assert await plugin.get_status(
        BackupContext(job_id="status", target_id="invoice", config=config)
    ) == {"status": "ok"}
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unauthorized",
        "forbidden",
        "missing-version",
        "wrong-version",
        "malformed-json",
        "non-object-json",
        "missing-company",
        "empty-company",
        "missing-user",
        "non-string-user",
    ),
)
async def test_ping_rejects_auth_protocol_and_application_identity_failures_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    token = "synthetic-token-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        if case == "unauthorized":
            return httpx.Response(401, text=token)
        if case == "forbidden":
            return httpx.Response(403, text=token)
        if case == "missing-version":
            return httpx.Response(
                200,
                json={"company_name": "Synthetic company", "user_name": "Synthetic user"},
            )
        if case == "wrong-version":
            return httpx.Response(
                200,
                headers={"X-APP-VERSION": "5.13.30"},
                json={"company_name": "Synthetic company", "user_name": "Synthetic user"},
            )
        if case == "malformed-json":
            return httpx.Response(
                200,
                headers={"X-APP-VERSION": "5.13.31"},
                content=b"not-json",
            )
        if case == "non-object-json":
            return httpx.Response(
                200,
                headers={"X-APP-VERSION": "5.13.31"},
                json=["unexpected", token],
            )
        payload: dict[str, object] = {
            "company_name": "Synthetic company",
            "user_name": "Synthetic user",
        }
        if case == "missing-company":
            payload.pop("company_name")
        elif case == "empty-company":
            payload["company_name"] = ""
        elif case == "missing-user":
            payload.pop("user_name")
        elif case == "non-string-user":
            payload["user_name"] = 123
        return httpx.Response(
            200,
            headers={"X-APP-VERSION": "5.13.31"},
            json=payload,
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError) as raised:
        await get_plugin("invoiceninja").test({"base_url": "http://invoice.local", "token": token})

    assert token not in str(raised.value)
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_ping_reports_network_failure_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "synthetic-token-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection rejected for {token}", request=request)

    _install_transport(monkeypatch, handler)

    with pytest.raises(ConnectionError) as raised:
        await get_plugin("invoiceninja").test({"base_url": "http://invoice.local", "token": token})

    assert token not in str(raised.value)
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_get_status_reports_fresh_probe_failure_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "synthetic-token-must-not-escape"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, text=token)

    _install_transport(monkeypatch, handler)
    result = await get_plugin("invoiceninja").get_status(
        BackupContext(
            job_id="status",
            target_id="invoice",
            config={"base_url": "http://invoice.local", "token": token},
        )
    )

    assert result["status"] == "error"
    assert "401" in result["error"]
    assert token not in json.dumps(result)
    assert [request.url.path for request in requests] == ["/api/v1/ping"]


@pytest.mark.asyncio
async def test_test_does_not_follow_cross_origin_redirect_with_api_token(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/collect"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    with pytest.raises(RuntimeError, match="status 302"):
        await InvoiceNinjaPlugin("invoiceninja").test(
            {"base_url": "http://example.local", "token": "t"}
        )

    assert [request.url.host for request in requests] == ["example.local"]


@pytest.mark.asyncio
async def test_backup_probes_then_triggers_exact_canonical_export_without_forwarding_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "synthetic-token"
    requests: list[httpx.Request] = []
    protected_path = f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST" and request.url.path == "/api/v1/export":
            assert request.headers["X-API-TOKEN"] == token
            assert request.headers["X-Requested-With"] == "XMLHttpRequest"
            assert request.headers["Accept"] == "application/json"
            assert request.content == b""
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"https://invoice.local{protected_path}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        if request.method == "GET" and request.url.path == protected_path:
            query_items = request.url.params.multi_items()
            assert [key for key, _value in query_items] == ["expires", "signature"]
            assert all(value for _key, value in query_items)
            assert request.headers.get("X-API-TOKEN") is None
            assert request.headers.get("X-Requested-With") is None
            assert request.headers["Accept"] == ("application/zip, application/octet-stream")
            return httpx.Response(401)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="authorization expired"):
        await plugin.backup(
            BackupContext(
                job_id="exact-trigger",
                target_id="invoice",
                config={
                    "base_url": "https://INVOICE.local:443",
                    "token": token,
                },
                metadata={"target_slug": "invoice"},
            )
        )

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/ping"),
        ("POST", "/api/v1/export"),
        ("GET", protected_path),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "wrong-version",
        "redirect",
        "wrong-status",
        "malformed-json",
        "non-object-json",
        "extra-response-key",
        "wrong-message",
        "missing-url",
        "empty-url",
        "relative-url",
        "cross-origin-url",
        "credentialed-url",
        "missing-query",
        "duplicate-expires",
        "duplicate-signature",
        "unknown-query",
        "empty-expires",
        "empty-signature",
        "fragment-url",
        "wrong-path-url",
        "non-uuid-path",
    ),
)
async def test_backup_rejects_nonexact_trigger_and_unsafe_signed_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    token = "synthetic-token-must-not-escape"
    signed_secret = "signed-secret-must-not-escape"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            return _ping_response(version="5.13.30" if case == "wrong-version" else "5.13.31")
        if request.url.path != "/api/v1/export":
            raise AssertionError("Invalid export response reached the download boundary")
        unsigned_url = f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
        exact_url = f"{unsigned_url}?expires=1700000000&signature={signed_secret}"
        exact_payload: object = {"message": "Processing", "url": exact_url}
        status_code = 200
        headers: dict[str, str] = {}
        content: bytes | None = None
        if case == "redirect":
            status_code = 302
            headers = {"location": "https://attacker.invalid/collect"}
        elif case == "wrong-status":
            status_code = 201
        elif case == "malformed-json":
            content = b"not-json"
        elif case == "non-object-json":
            exact_payload = ["Processing", exact_url]
        elif case == "extra-response-key":
            exact_payload = {
                "message": "Processing",
                "url": exact_url,
                "legacy": True,
            }
        elif case == "wrong-message":
            exact_payload = {"message": "Queued", "url": exact_url}
        elif case == "missing-url":
            exact_payload = {"message": "Processing"}
        elif case == "empty-url":
            exact_payload = {"message": "Processing", "url": ""}
        elif case == "relative-url":
            exact_payload = {
                "message": "Processing",
                "url": (
                    f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                    f"?expires=1700000000&signature={signed_secret}"
                ),
            }
        elif case == "cross-origin-url":
            exact_payload = {
                "message": "Processing",
                "url": (
                    "https://attacker.invalid/api/v1/protected_download/"
                    f"{SIGNED_EXPORT_UUID}?expires=1700000000&signature={signed_secret}"
                ),
            }
        elif case == "credentialed-url":
            exact_payload = {
                "message": "Processing",
                "url": (
                    "http://user:password@invoice.local/api/v1/"
                    f"protected_download/{SIGNED_EXPORT_UUID}"
                    f"?expires=1700000000&signature={signed_secret}"
                ),
            }
        elif case == "missing-query":
            exact_payload = {
                "message": "Processing",
                "url": unsigned_url,
            }
        elif case == "duplicate-expires":
            exact_payload = {
                "message": "Processing",
                "url": f"{exact_url}&expires=1700000001",
            }
        elif case == "duplicate-signature":
            exact_payload = {
                "message": "Processing",
                "url": f"{exact_url}&signature=second-signature",
            }
        elif case == "unknown-query":
            exact_payload = {
                "message": "Processing",
                "url": f"{exact_url}&legacy=true",
            }
        elif case == "empty-expires":
            exact_payload = {
                "message": "Processing",
                "url": f"{unsigned_url}?expires=&signature=synthetic-signature",
            }
        elif case == "empty-signature":
            exact_payload = {
                "message": "Processing",
                "url": f"{unsigned_url}?expires=1700000000&signature=",
            }
        elif case == "fragment-url":
            exact_payload = {
                "message": "Processing",
                "url": f"{exact_url}#fragment",
            }
        elif case == "wrong-path-url":
            exact_payload = {
                "message": "Processing",
                "url": (
                    f"http://invoice.local/downloads/{signed_secret}.zip"
                    "?expires=1700000000&signature=synthetic-signature"
                ),
            }
        elif case == "non-uuid-path":
            exact_payload = {
                "message": "Processing",
                "url": (
                    "http://invoice.local/api/v1/protected_download/not-a-uuid"
                    f"?expires=1700000000&signature={signed_secret}"
                ),
            }
        if content is not None:
            return httpx.Response(status_code, headers=headers, content=content)
        return httpx.Response(status_code, headers=headers, json=exact_payload)

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError) as raised:
        await plugin.backup(
            BackupContext(
                job_id=f"invalid-trigger-{case}",
                target_id="invoice",
                config={"base_url": "http://invoice.local", "token": token},
                metadata={"target_slug": "invoice"},
            )
        )

    assert token not in str(raised.value)
    assert token not in caplog.text
    assert signed_secret not in str(raised.value)
    assert signed_secret not in caplog.text
    expected_paths = (
        ["/api/v1/ping"] if case == "wrong-version" else ["/api/v1/ping", "/api/v1/export"]
    )
    assert [request.url.path for request in requests] == expected_paths
    assert all(request.url.host == "invoice.local" for request in requests)


@pytest.mark.asyncio
async def test_backup_lock_serializes_canonical_origin_but_not_distinct_origins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase = "same"
    same_first_post = asyncio.Event()
    same_second_post = asyncio.Event()
    same_release = asyncio.Event()
    distinct_both_posts = asyncio.Event()
    distinct_release = asyncio.Event()
    post_count = 0
    distinct_hosts: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            if phase == "same":
                post_count += 1
                if post_count == 1:
                    same_first_post.set()
                    await same_release.wait()
                else:
                    same_second_post.set()
            else:
                assert request.url.host is not None
                distinct_hosts.add(request.url.host)
                if len(distinct_hosts) == 2:
                    distinct_both_posts.set()
                await distinct_release.wait()
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"{request.url.scheme}://{request.url.host}"
                        f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        if request.url.path.startswith("/api/v1/protected_download/"):
            return httpx.Response(401)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_async_transport(monkeypatch, handler)
    monkeypatch.setattr(InvoiceNinjaPlugin, "_base_dir", lambda _self: str(tmp_path))

    async def run_backup(base_url: str, target_id: str) -> object:
        return await get_plugin("invoiceninja").backup(
            BackupContext(
                job_id=f"job-{target_id}",
                target_id=target_id,
                config={"base_url": base_url, "token": f"token-{target_id}"},
                metadata={"target_slug": target_id},
            )
        )

    first = asyncio.create_task(run_backup("https://INVOICE.local:443", "same-a"))
    await asyncio.wait_for(same_first_post.wait(), timeout=1)
    second = asyncio.create_task(run_backup("https://invoice.local", "same-b"))
    await asyncio.sleep(0.05)
    same_origin_was_serialized = not same_second_post.is_set()
    same_release.set()
    same_results = await asyncio.gather(first, second, return_exceptions=True)

    assert same_origin_was_serialized
    assert post_count == 2
    assert all(
        isinstance(result, RuntimeError) and "authorization expired" in str(result)
        for result in same_results
    )

    phase = "distinct"
    distinct_a = asyncio.create_task(run_backup("https://invoice-a.local", "distinct-a"))
    distinct_b = asyncio.create_task(run_backup("https://invoice-b.local", "distinct-b"))
    try:
        await asyncio.wait_for(distinct_both_posts.wait(), timeout=1)
        distinct_origins_ran_concurrently = True
    except asyncio.TimeoutError:
        distinct_origins_ran_concurrently = False
    finally:
        distinct_release.set()
    distinct_results = await asyncio.gather(
        distinct_a,
        distinct_b,
        return_exceptions=True,
    )

    assert distinct_origins_ran_concurrently
    assert distinct_hosts == {"invoice-a.local", "invoice-b.local"}
    assert all(
        isinstance(result, RuntimeError) and "authorization expired" in str(result)
        for result in distinct_results
    )


@pytest.mark.asyncio
async def test_backup_polls_then_streams_one_private_durable_artifact_and_safe_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backup_root = tmp_path / "backups"
    payload = _streaming_export_zip()
    split = len(payload) // 3
    stream = _ObservedChunkStream(
        (payload[:split], payload[split : split * 2], payload[split * 2 :]),
        backup_root,
    )
    token = "synthetic-token-must-not-escape"
    signed_id = SIGNED_EXPORT_UUID
    signed_secret = "signed-secret-must-not-escape"
    download_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_requests
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        "http://invoice.local/api/v1/protected_download/"
                        f"{signed_id}?expires=1700000000&signature={signed_secret}"
                    ),
                },
            )
        if request.url.path == f"/api/v1/protected_download/{signed_id}":
            download_requests += 1
            if download_requests == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=b"<html><body>Export is still processing</body></html>",
                )
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="company-export.zip"',
                },
                stream=stream,
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    original_read_bytes = Path.read_bytes

    def reject_artifact_read_bytes(path: Path) -> bytes:
        if path.is_relative_to(backup_root):
            raise AssertionError("Invoice Ninja artifact must remain streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_artifact_read_bytes)
    original_fsync = os.fsync
    fsynced_paths: list[str] = []

    def record_fsync(file_descriptor: int) -> None:
        try:
            fsynced_paths.append(os.readlink(f"/proc/self/fd/{file_descriptor}"))
        except OSError:
            fsynced_paths.append("<unresolved>")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    result = await plugin.backup(
        BackupContext(
            job_id="streaming-publication",
            target_id="invoice",
            config={"base_url": "http://invoice.local", "token": token},
            metadata={"target_slug": "invoice-source"},
        )
    )

    assert set(result) == {"artifact_path"}
    artifact = Path(result["artifact_path"])
    sidecar_path = Path(f"{artifact}.meta.json")
    with artifact.open("rb") as artifact_file:
        published = artifact_file.read()
    assert published == payload
    assert stream.yielded_chunks == 3
    assert stream.first_write_modes == [0o600]
    assert stream.closed
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    assert not list(backup_root.rglob("*.tmp"))

    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "invoiceninja"
    assert sidecar["plugin_version"] == "0.2.1"
    assert sidecar["target_slug"] == "invoice-source"
    assert sidecar["artifact_path"] == str(artifact)
    assert sidecar["artifact_bytes"] == len(payload)
    assert sidecar["sha256"] == hashlib.sha256(payload).hexdigest()
    assert sidecar["application_version"] == "5.13.31"
    assert sidecar["archive_member_count"] == 4
    assert sidecar["validation"] == "passed"

    assert any(path.endswith(".tmp") and ".meta.json." not in path for path in fsynced_paths)
    assert any(".meta.json." in path and path.endswith(".tmp") for path in fsynced_paths)
    assert fsynced_paths.count(str(artifact.parent)) >= 2

    public_evidence = json.dumps(sidecar, sort_keys=True)
    for secret in (
        token,
        signed_secret,
        "http://invoice.local",
        "private-content-must-not-escape",
        "/private/source/customer-contract.pdf",
    ):
        assert secret not in public_evidence
        assert secret not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert download_requests == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_exception", "expected_downloads"),
    (
        ("timeout", RuntimeError, 12),
        ("auth", RuntimeError, 1),
        ("unexpected-status", RuntimeError, 1),
        ("html-timeout", RuntimeError, 12),
        ("bad-media", RuntimeError, 1),
        ("network", ConnectionError, 1),
    ),
)
async def test_backup_terminal_failures_remove_every_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
    expected_exception: type[Exception],
    expected_downloads: int,
) -> None:
    backup_root = tmp_path / "backups"
    token = "synthetic-token-must-not-escape"
    signed_id = SIGNED_EXPORT_UUID
    signed_secret = "signed-secret-must-not-escape"
    download_requests = 0
    network_stream: _FailingChunkStream | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_requests, network_stream
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        "http://invoice.local/api/v1/protected_download/"
                        f"{signed_id}?expires=1700000000&signature={signed_secret}"
                    ),
                },
            )
        download_requests += 1
        if case == "timeout":
            return httpx.Response(404)
        if case == "auth":
            return httpx.Response(403, text=token)
        if case == "unexpected-status":
            return httpx.Response(202)
        if case == "html-timeout":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><body>Export is still processing</body></html>",
            )
        if case == "bad-media":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"not-an-export",
            )
        network_error = httpx.ReadError(f"stream failed for {token}", request=request)
        network_stream = _FailingChunkStream(network_error)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/zip",
                "content-disposition": 'attachment; filename="company-export.zip"',
            },
            stream=network_stream,
        )

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    with pytest.raises(expected_exception):
        await plugin.backup(
            BackupContext(
                job_id=f"cleanup-{case}",
                target_id="invoice",
                config={
                    "base_url": "http://invoice.local",
                    "token": token,
                    "export_timeout_seconds": 60,
                },
                metadata={"target_slug": f"invoice-{case}"},
            )
        )

    assert download_requests == expected_downloads
    assert _backup_files(backup_root) == []
    assert token not in caplog.text
    assert signed_secret not in caplog.text
    if network_stream is not None:
        assert network_stream.closed


@pytest.mark.asyncio
async def test_cancelled_streaming_backup_closes_response_and_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    first_write_observed = asyncio.Event()
    stream = _BlockingChunkStream(first_write_observed)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "application/zip",
                "content-disposition": 'attachment; filename="company-export.zip"',
            },
            stream=stream,
        )

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))
    task = asyncio.create_task(
        plugin.backup(
            BackupContext(
                job_id="cancelled-stream",
                target_id="invoice",
                config={"base_url": "http://invoice.local", "token": "synthetic-token"},
                metadata={"target_slug": "invoice-cancelled"},
            )
        )
    )

    await asyncio.wait_for(first_write_observed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed
    assert _backup_files(backup_root) == []


def test_strict_export_validator_accepts_exact_v51331_native_archive(tmp_path: Path) -> None:
    artifact = tmp_path / "exact-v51331-export.zip"
    artifact.write_bytes(_invoice_export_zip())

    assert InvoiceNinjaPlugin("invoiceninja")._validate_export(artifact) == 4


@pytest.mark.parametrize(
    "case",
    (
        "unexpected-root",
        "absolute-path",
        "traversal-path",
        "duplicate-name",
        "case-collision",
        "link-member",
        "special-member",
        "encrypted-member",
        "unsupported-compression",
        "crc-error",
        "trailing-data",
        "archive-size-limit",
        "member-count-limit",
        "member-compressed-limit",
        "member-expanded-limit",
        "total-compressed-limit",
        "total-expanded-limit",
        "ratio-limit",
        "path-depth-limit",
        "backup-json-size-limit",
        "document-size-limit",
        "wrong-version",
        "missing-field",
        "wrong-company-type",
        "wrong-storage-type",
        "wrong-array-type",
        "unexpected-json-field",
        "unsafe-document-url",
        "missing-document-member",
        "duplicate-document-member",
        "wrong-document-size",
        "document-link",
    ),
)
def test_strict_export_validator_rejects_unsafe_or_nonexact_native_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    private_marker = "ambiguous-secret-trailer"
    artifact = tmp_path / f"invalid-{case}.zip"
    artifact.write_bytes(_strict_archive_case(case, monkeypatch))

    with pytest.raises(RuntimeError) as raised:
        InvoiceNinjaPlugin("invoiceninja")._validate_export(artifact)

    assert private_marker not in str(raised.value)
    assert DOCUMENT_URL not in str(raised.value)


@pytest.mark.asyncio
async def test_backup_polling_uses_a_wall_clock_deadline_not_attempt_arithmetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    now = 100.0
    download_requests = 0

    def monotonic() -> float:
        return now

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_requests, now
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=2000000000&signature=synthetic-signature"
                    ),
                },
            )
        download_requests += 1
        now += 61.0
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(plugin_module, "_monotonic", monotonic, raising=False)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    with pytest.raises(RuntimeError, match="not ready|timed out"):
        await plugin.backup(
            BackupContext(
                job_id="wall-clock-deadline",
                target_id="invoice",
                config={
                    "base_url": "http://invoice.local",
                    "token": "synthetic-token",
                    "export_timeout_seconds": 60,
                },
                metadata={"target_slug": "invoice-deadline"},
            )
        )

    assert download_requests == 1
    assert _backup_files(backup_root) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("timeout", "repeated-cancellation"))
async def test_backup_validation_worker_is_stopped_reaped_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    backup_root = tmp_path / "backups"
    _install_ready_export_transport(monkeypatch, _invoice_export_zip())
    process = _BlockingValidationProcess()
    connection = _NoValidationResultConnection()
    validation_paths: list[Path] = []

    def start_blocked_validation(artifact_path: Path) -> tuple[object, object]:
        assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
        validation_paths.append(artifact_path)
        return process, connection

    monkeypatch.setattr(
        plugin_module,
        "_start_validation_process",
        start_blocked_validation,
        raising=False,
    )
    monkeypatch.setattr(
        plugin_module,
        "_VALIDATION_TIMEOUT_SECONDS",
        0.01 if mode == "timeout" else 60.0,
        raising=False,
    )
    monkeypatch.setattr(
        plugin_module,
        "_WORKER_STOP_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))
    context = BackupContext(
        job_id=f"validation-{mode}",
        target_id="invoice",
        config={"base_url": "http://invoice.local", "token": "synthetic-token"},
        metadata={"target_slug": f"invoice-{mode}"},
    )

    task = asyncio.create_task(plugin.backup(context))
    if mode == "timeout":
        with pytest.raises(TimeoutError, match="validation.*timed out|timed out.*validation"):
            await task
    else:
        assert await asyncio.to_thread(process.join_started.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(process.terminate_called.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(process.kill_called.wait, 2)
        with pytest.raises(asyncio.CancelledError):
            await task

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -15
    assert not process.is_alive()
    assert connection.closed
    assert validation_paths and all(not path.exists() for path in validation_paths)
    assert _backup_files(backup_root) == []


@pytest.mark.asyncio
async def test_public_backup_validates_in_one_real_spawned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    _install_ready_export_transport(monkeypatch, _invoice_export_zip())
    start_validation = getattr(plugin_module, "_start_validation_process", None)
    assert callable(start_validation), "Invoice Ninja must expose the validation worker seam"
    worker_pids: list[int] = []

    def record_real_spawn(artifact_path: Path) -> tuple[object, object]:
        process, connection = start_validation(artifact_path)
        assert getattr(process, "_start_method", None) == "spawn"
        pid = getattr(process, "pid", None)
        assert isinstance(pid, int) and pid != os.getpid()
        worker_pids.append(pid)
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_validation_process", record_real_spawn)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    result = await plugin.backup(
        BackupContext(
            job_id="real-spawn-validation",
            target_id="invoice",
            config={"base_url": "http://invoice.local", "token": "synthetic-token"},
            metadata={"target_slug": "invoice-real-spawn"},
        )
    )

    assert len(worker_pids) == 1
    artifact = Path(result["artifact_path"])
    assert artifact.is_file()
    assert read_backup_sidecar(str(artifact))["archive_member_count"] == 4  # type: ignore[index]


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST" and request.url.path.endswith("/api/v1/export"):
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://example.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        if (
            request.method == "GET"
            and request.url.path == f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
        ):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": "attachment; filename=export.zip",
                },
                content=_company_export_zip(),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    async def fake_sleep(seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "token": "t"},
        metadata={"target_slug": "slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)


@pytest.mark.asyncio
async def test_backup_rejects_html_page(tmp_path, monkeypatch):
    # Always return 200 HTML page to emulate Invoice Ninja error template
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST" and request.url.path.endswith("/api/v1/export"):
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://example.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        if (
            request.method == "GET"
            and request.url.path == f"/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
        ):
            html = (
                b"<!DOCTYPE html>\n<html><head><title>Error</title></head><body>404</body></html>"
            )
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, content=html
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    async def fake_sleep(seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "token": "t"},
        metadata={"target_slug": "slug"},
    )

    with pytest.raises(RuntimeError):
        await plugin.backup(ctx)


@pytest.mark.asyncio
async def test_backup_rejects_corrupt_zip_response(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST" and request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://example.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            content=b"PK\x03\x04truncated",
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="valid ZIP archive"):
        await plugin.backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={"base_url": "http://example.local", "token": "t"},
                metadata={"target_slug": "slug"},
            )
        )


@pytest.mark.asyncio
async def test_backup_rejects_cross_origin_signed_download(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/ping":
            return _ping_response()
        return httpx.Response(
            200,
            json={
                "message": "Processing",
                "url": (
                    f"https://attacker.invalid/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                    "?expires=1700000000&signature=synthetic-signature"
                ),
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="same origin"):
        await plugin.backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={"base_url": "http://example.local", "token": "t"},
                metadata={"target_slug": "slug"},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "missing-authorization",
        "broad-authorization",
        "missing-allowlist",
        "unapproved-origin",
        "missing-source-origin",
        "same-origin",
        "invalid-config",
    ),
)
async def test_restore_refuses_unauthorized_or_ambiguous_destination_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    artifact = tmp_path / "invoice-restore-refusal.zip"
    artifact.write_bytes(_invoice_export_zip())
    monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", raising=False)
    monkeypatch.delenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        raising=False,
    )
    if case != "missing-authorization":
        monkeypatch.setenv(
            "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE",
            "true" if case == "broad-authorization" else "1",
        )
    if case != "missing-allowlist":
        monkeypatch.setenv(
            "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
            (
                "https://unapproved-restore.local"
                if case == "unapproved-origin"
                else "https://INVOICE-RESTORE.local:443"
            ),
        )
    source_origin: str | None = RESTORE_SOURCE_ORIGIN
    if case == "missing-source-origin":
        source_origin = None
    elif case == "same-origin":
        source_origin = "https://INVOICE-RESTORE.local:443"
    config: dict[str, object] = {
        "base_url": RESTORE_DESTINATION_ORIGIN,
        "token": "destination-token-must-not-escape",
    }
    if case == "invalid-config":
        config.pop("token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Unauthorized restore attempted network I/O")

    _install_transport(monkeypatch, handler)

    with pytest.raises((RuntimeError, ValueError)) as raised:
        await get_plugin("invoiceninja").restore(
            _restore_preflight_context(
                artifact,
                config=config,
                source_origin=source_origin,
            )
        )

    assert requests == []
    assert "destination-token-must-not-escape" not in str(raised.value)
    assert "destination-token-must-not-escape" not in caplog.text


@pytest.mark.asyncio
async def test_restore_runs_exact_fresh_destination_preflight_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UploadBoundaryReached(Exception):
        pass

    artifact = tmp_path / "invoice-restore-fresh.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-API-TOKEN"] == "destination-token-must-not-escape"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        if request.method == "POST":
            raise UploadBoundaryReached
        assert request.method == "GET"
        assert request.headers["Accept"] == "application/json"
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        assert request.url.path in RESTORE_RESOURCE_PATHS
        assert not request.url.query
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)

    with pytest.raises(UploadBoundaryReached):
        await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/ping"),
        *(("GET", path) for path in RESTORE_RESOURCE_PATHS),
        ("POST", "/api/v1/import_json"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("nonempty_path", RESTORE_RESOURCE_PATHS)
async def test_restore_rejects_each_nonempty_supported_resource_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    nonempty_path: str,
) -> None:
    artifact = tmp_path / "invoice-restore-nonempty.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST":
            raise AssertionError("Nonempty destination reached upload")
        if request.url.path == nonempty_path:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "private-row-must-not-escape"}],
                    "meta": {"pagination": {"total": 1}},
                },
            )
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError) as raised:
        await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert not any(request.method == "POST" for request in requests)
    assert "private-row-must-not-escape" not in str(raised.value)
    assert "private-row-must-not-escape" not in caplog.text


@pytest.mark.asyncio
async def test_restore_rejects_malformed_resource_response_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-malformed-preflight.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST":
            raise AssertionError("Malformed preflight reached upload")
        if request.url.path == "/api/v1/projects":
            return httpx.Response(200, json={"data": {}, "meta": {}})
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError):
        await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert [request.url.path for request in requests] == [
        "/api/v1/ping",
        *RESTORE_RESOURCE_PATHS[:4],
    ]
    assert not any(request.method == "POST" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "accepted"),
    (
        ("exact", True),
        ("missing-settings", False),
        ("nonobject-settings", False),
        ("missing-name", False),
        ("nonstring-name", False),
        ("empty-name", False),
    ),
)
async def test_restore_derives_company_marker_from_exact_v51331_settings_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
    accepted: bool,
) -> None:
    backup_json = _exact_backup_json()
    if case == "missing-settings":
        backup_json["company"] = {}
    elif case == "nonobject-settings":
        backup_json["company"] = {"settings": []}
    elif case == "missing-name":
        backup_json["company"] = {"settings": {}}
    elif case == "nonstring-name":
        backup_json["company"] = {"settings": {"name": 123}}
    elif case == "empty-name":
        backup_json["company"] = {"settings": {"name": ""}}
    artifact = tmp_path / f"invoice-company-marker-{case}.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []
    uploaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        requests.append(request)
        if request.method == "POST":
            if not accepted:
                raise AssertionError("Invalid company marker reached upload")
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        if request.url.path == "/api/v1/clients":
            return _resource_response([_restored_client_row()])
        if request.url.path == "/api/v1/invoices":
            return _resource_response([_restored_invoice_row()])
        raise AssertionError(f"Unexpected marker request: {request.url.path}")

    _install_transport(monkeypatch, handler)
    operation = get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))
    if accepted:
        result = await operation
        assert result["status"] == "partial"
        assert [request.method for request in requests].count("POST") == 1
    else:
        with pytest.raises(RuntimeError) as raised:
            await operation
        assert not any(request.method == "POST" for request in requests)
        assert RESTORE_COMPANY_MARKER not in str(raised.value)
    assert RESTORE_COMPANY_MARKER not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "missing-bytes",
        "boolean-bytes",
        "wrong-bytes",
        "missing-hash",
        "malformed-hash",
        "wrong-hash",
        "symlink",
        "queue-status",
        "queue-shape",
        "marker-malformed",
        "marker-mismatch",
        "marker-timeout",
    ),
)
async def test_restore_requires_verified_nofollow_artifact_identity_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    artifact = tmp_path / "invoice-restore-identity.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    context = _restore_preflight_context(artifact)
    assert context.metadata is not None
    if case == "missing-bytes":
        context.metadata.pop("artifact_bytes")
    elif case == "boolean-bytes":
        context.metadata["artifact_bytes"] = True
    elif case == "wrong-bytes":
        context.metadata["artifact_bytes"] = artifact.stat().st_size + 1
    elif case == "missing-hash":
        context.metadata.pop("artifact_sha256")
    elif case == "malformed-hash":
        context.metadata["artifact_sha256"] = "not-a-sha256"
    elif case == "wrong-hash":
        context.metadata["artifact_sha256"] = "0" * 64
    elif case == "symlink":
        real_artifact = tmp_path / "real-invoice-restore.zip"
        artifact.rename(real_artifact)
        artifact.symlink_to(real_artifact)
    requests: list[httpx.Request] = []
    uploaded = False
    now = 0.0

    if case in {"marker-mismatch", "marker-timeout"}:
        monkeypatch.setattr(
            plugin_module,
            "_RESTORE_MARKER_TIMEOUT_SECONDS",
            1.0,
            raising=False,
        )
        monkeypatch.setattr(
            plugin_module,
            "_RESTORE_POLL_INTERVAL_SECONDS",
            0.0,
            raising=False,
        )
        monkeypatch.setattr(plugin_module, "_monotonic", lambda: now)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal now, uploaded
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            if uploaded and case == "marker-mismatch":
                now += 2.0
                return httpx.Response(
                    200,
                    headers={"X-APP-VERSION": "5.13.31"},
                    json={
                        "company_name": "private-mismatch-must-not-escape",
                        "user_name": "Synthetic user",
                    },
                )
            return _ping_response()
        if request.method == "POST":
            if case not in {
                "queue-status",
                "queue-shape",
                "marker-malformed",
                "marker-mismatch",
                "marker-timeout",
            }:
                raise AssertionError("Unverified artifact reached upload")
            uploaded = True
            if case == "queue-status":
                return httpx.Response(202, json={"message": "Processing", "success": True})
            if case == "queue-shape":
                return httpx.Response(
                    200,
                    json={"message": "Processing", "success": True, "legacy": True},
                )
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if uploaded and case == "marker-malformed" and request.url.path == "/api/v1/clients":
            return httpx.Response(200, json={"data": {}, "meta": {}})
        if uploaded and case == "marker-timeout" and request.url.path == "/api/v1/invoices":
            now += 2.0
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)

    with pytest.raises((RuntimeError, ValueError)) as raised:
        await get_plugin("invoiceninja").restore(context)

    identity_cases = {
        "missing-bytes",
        "boolean-bytes",
        "wrong-bytes",
        "missing-hash",
        "malformed-hash",
        "wrong-hash",
        "symlink",
    }
    if case in identity_cases:
        assert not any(request.method == "POST" for request in requests)
    for private_value in (
        RESTORE_CLIENT_MARKER,
        RESTORE_INVOICE_MARKER,
        "private-mismatch-must-not-escape",
        "destination-token-must-not-escape",
    ):
        assert private_value not in str(raised.value)
        assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_restore_uploads_held_verified_descriptor_and_polls_derived_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-held-descriptor.zip"
    original_payload = _invoice_export_zip()
    artifact.write_bytes(original_payload)
    replacement_payload = bytearray(original_payload)
    replacement_payload[len(replacement_payload) // 2] ^= 0x01
    replacement = bytes(replacement_payload)
    assert len(replacement) == len(original_payload)
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []
    uploaded = False
    marker_reads = {"clients": 0, "invoices": 0}
    opened_with_nofollow = False
    substituted = False
    original_open = os.open
    original_dup = os.dup

    def observe_open(path: os.PathLike[str] | str, flags: int, *args: object) -> int:
        nonlocal opened_with_nofollow
        if os.fspath(path) == str(artifact):
            opened_with_nofollow = bool(flags & getattr(os, "O_NOFOLLOW", 0))
        return original_open(path, flags, *args)  # type: ignore[arg-type]

    def substitute_path_then_dup(file_descriptor: int) -> int:
        nonlocal substituted
        if not substituted and os.fstat(file_descriptor).st_size == len(original_payload):
            relocated = artifact.with_suffix(".verified.zip")
            artifact.rename(relocated)
            artifact.write_bytes(replacement)
            substituted = True
        return original_dup(file_descriptor)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/v1/import_json"
            assert request.content.count(original_payload) == 1
            assert replacement not in request.content
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        resource = request.url.path.rsplit("/", 1)[-1]
        if resource == "clients":
            marker_reads[resource] += 1
            return _resource_response(
                [] if marker_reads[resource] == 1 else [_restored_client_row()]
            )
        if resource == "invoices":
            marker_reads[resource] += 1
            return _resource_response(
                [] if marker_reads[resource] == 1 else [_restored_invoice_row()]
            )
        raise AssertionError(f"Unexpected post-import resource: {request.url.path}")

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "dup", substitute_path_then_dup)
    _install_transport(monkeypatch, handler)

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    result = await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert opened_with_nofollow
    assert substituted
    assert marker_reads == {"clients": 2, "invoices": 2}
    assert result["status"] == "partial"
    assert result["artifact_bytes"] == len(original_payload)
    assert result["artifact_sha256"] == hashlib.sha256(original_payload).hexdigest()
    assert "asynchronous" in result["message"].lower()
    assert "document" in result["message"].lower()
    public_result = json.dumps(result, sort_keys=True)
    assert RESTORE_CLIENT_MARKER not in public_result
    assert RESTORE_INVOICE_MARKER not in public_result


@pytest.mark.asyncio
async def test_restore_marker_poll_cancellation_propagates_without_deleting_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-cancelled-poll.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    uploaded = False
    marker_poll_started = asyncio.Event()
    never_release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        if request.url.path == "/api/v1/clients":
            marker_poll_started.set()
            await never_release.wait()
        return _empty_resource_response()

    _install_async_transport(monkeypatch, handler)
    task = asyncio.create_task(
        get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))
    )
    await asyncio.wait_for(marker_poll_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert artifact.is_file()


def test_scheduler_records_invoice_ninja_source_origin_without_token(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = Tag(display_name="Invoice Ninja scheduler provenance")
    db_session.add(tag)
    db_session.flush()
    job = Job(
        tag_id=tag.id,
        name="Invoice Ninja backup",
        schedule_cron="* * * * *",
        enabled=True,
    )
    target = Target(
        name="Invoice Ninja source",
        slug="invoice-source",
        plugin_name="invoiceninja",
        plugin_config_json=json.dumps(
            {
                "base_url": "https://INVOICE-SOURCE.local:443",
                "token": "scheduler-token-must-not-escape",
            }
        ),
    )
    run = Run(status="running", operation="backup")
    db_session.add_all((job, target, run))
    db_session.commit()
    plugin = InvoiceNinjaPlugin("invoiceninja")
    artifact_dir = tmp_path / "scheduler-backups"
    artifact_dir.mkdir()

    async def local_backup(context: BackupContext) -> dict[str, str]:
        artifact = artifact_dir / "invoice-scheduled.zip"
        artifact.write_bytes(_invoice_export_zip())
        write_backup_sidecar(str(artifact), plugin, context)
        return {"artifact_path": str(artifact)}

    monkeypatch.setattr(plugin, "backup", local_backup)
    monkeypatch.setattr("app.core.scheduler.get_plugin", lambda _name: plugin)

    result = _perform_target_run(
        db_session,
        job,
        run,
        target_id=int(target.id),
    )

    assert result["status"] == "success"
    target_run = db_session.query(TargetRun).one()
    assert json.loads(target_run.source_identity_json or "{}") == {
        "base_url": "https://invoice-source.local:443"
    }
    audit = "\n".join(
        filter(None, (target_run.message, target_run.logs_text, target_run.source_identity_json))
    )
    assert "scheduler-token-must-not-escape" not in audit


def test_restore_service_preserves_source_and_records_private_partial_orchestration(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _source, destination, _source_run, source_target_run, artifact = (
        _create_restore_service_records(db_session, tmp_path)
    )
    source_payload = artifact.read_bytes()
    source_inode = artifact.stat().st_ino
    source_sidecar = Path(f"{artifact}.meta.json")
    sidecar_payload = source_sidecar.read_bytes()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path / "backups"))
    _authorize_invoice_restore(monkeypatch)
    _install_successful_restore_transport(monkeypatch)
    plugin = InvoiceNinjaPlugin("invoiceninja")
    original_restore = plugin.restore
    observed: dict[str, object] = {}

    async def observe_staging(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["path"] = staged
        observed["inode"] = staged.stat().st_ino
        observed["mode"] = stat.S_IMODE(staged.stat().st_mode)
        observed["bytes"] = staged.stat().st_size
        observed["sha256"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        observed["metadata"] = dict(context.metadata or {})
        return await original_restore(context)

    monkeypatch.setattr(plugin, "restore", observe_staging)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)

    restored = RestoreService(db_session).restore(
        source_target_run_id=int(source_target_run.id),
        destination_target_id=int(destination.id),
    )

    staged = observed["path"]
    assert isinstance(staged, Path)
    assert staged != artifact
    assert observed["inode"] != source_inode
    assert observed["mode"] == 0o600
    assert observed["bytes"] == len(source_payload)
    assert observed["sha256"] == hashlib.sha256(source_payload).hexdigest()
    assert source_target_run.started_at is not None
    assert source_target_run.finished_at is not None
    assert observed["metadata"] == {
        "destination_target_slug": destination.slug,
        "source_target_run_id": source_target_run.id,
        "source_run_id": source_target_run.run_id,
        "source_target_id": source_target_run.target_id,
        "source_target_slug": "invoice-source",
        "source_database_identity": {"base_url": RESTORE_SOURCE_ORIGIN},
        "artifact_bytes": len(source_payload),
        "artifact_sha256": hashlib.sha256(source_payload).hexdigest(),
        "backup_started_at": source_target_run.started_at.isoformat(),
        "backup_finished_at": source_target_run.finished_at.isoformat(),
    }
    assert not staged.exists()
    assert artifact.read_bytes() == source_payload
    assert artifact.stat().st_ino == source_inode
    assert source_sidecar.read_bytes() == sidecar_payload
    assert restored.status == "partial"
    assert len(restored.target_runs) == 1
    assert restored.target_runs[0].status == "partial"
    assert restored.target_runs[0].target_id == destination.id
    assert restored.target_runs[0].artifact_bytes == len(source_payload)
    assert restored.target_runs[0].sha256 == hashlib.sha256(source_payload).hexdigest()
    audit = "\n".join(
        filter(
            None,
            (
                restored.message,
                restored.logs_text,
                restored.target_runs[0].message,
                restored.target_runs[0].logs_text,
                caplog.text,
            ),
        )
    )
    for private_value in (
        "source-token-must-not-escape",
        "destination-token-must-not-escape",
        RESTORE_CLIENT_MARKER,
        RESTORE_INVOICE_MARKER,
        str(staged),
    ):
        assert private_value not in audit


@pytest.mark.parametrize(
    "case",
    (
        "tampered-artifact",
        "tampered-sidecar",
        "cancelled",
        "nonfresh",
        "marker-failure",
    ),
)
def test_restore_service_failure_audit_is_secret_safe_and_cleans_private_staging(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    _source, destination, _source_run, source_target_run, artifact = (
        _create_restore_service_records(db_session, tmp_path)
    )
    original_payload = artifact.read_bytes()
    original_inode = artifact.stat().st_ino
    sidecar = Path(f"{artifact}.meta.json")
    original_sidecar = sidecar.read_bytes()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path / "backups"))
    _authorize_invoice_restore(monkeypatch)
    plugin = InvoiceNinjaPlugin("invoiceninja")
    plugin_called = False
    uploaded = False

    if case == "tampered-artifact":
        with artifact.open("ab") as output:
            output.write(b"tampered-private-bytes")
    elif case == "tampered-sidecar":
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        sidecar.write_text(json.dumps(metadata), encoding="utf-8")

    if case == "cancelled":

        async def cancel_restore(_context: RestoreContext) -> dict[str, Any]:
            nonlocal plugin_called
            plugin_called = True
            raise asyncio.CancelledError

        monkeypatch.setattr(plugin, "restore", cancel_restore)
    else:
        original_restore = plugin.restore

        async def observe_restore(context: RestoreContext) -> dict[str, Any]:
            nonlocal plugin_called
            plugin_called = True
            return await original_restore(context)

        monkeypatch.setattr(plugin, "restore", observe_restore)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            if uploaded and case == "marker-failure":
                return httpx.Response(401, text="destination-token-must-not-escape")
            return _ping_response()
        if case == "nonfresh" and not uploaded and request.url.path == "/api/v1/clients":
            return _resource_response([{"name": "private-row-must-not-escape"}])
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)

    expected_exception: type[BaseException] = (
        asyncio.CancelledError
        if case == "cancelled"
        else (ValueError if case.startswith("tampered") else RuntimeError)
    )
    with pytest.raises(expected_exception):
        RestoreService(db_session).restore(
            source_target_run_id=int(source_target_run.id),
            destination_target_id=int(destination.id),
        )

    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    if case.startswith("tampered"):
        assert plugin_called is False
        assert db_session.query(Run).count() == 1
    else:
        restore_run = db_session.query(Run).filter(Run.operation == "restore").one()
        restore_target_run = (
            db_session.query(TargetRun).filter(TargetRun.operation == "restore").one()
        )
        assert restore_run.status == "failed"
        assert restore_run.finished_at is not None
        assert restore_target_run.status == "failed"
        assert restore_target_run.finished_at is not None
        audit = "\n".join(
            filter(
                None,
                (
                    restore_run.message,
                    restore_run.logs_text,
                    restore_target_run.message,
                    restore_target_run.logs_text,
                    caplog.text,
                ),
            )
        )
        for secret in (
            "source-token-must-not-escape",
            "destination-token-must-not-escape",
            "private-row-must-not-escape",
            RESTORE_CLIENT_MARKER,
            RESTORE_INVOICE_MARKER,
        ):
            assert secret not in audit
    if case != "tampered-artifact":
        assert artifact.read_bytes() == original_payload
        assert artifact.stat().st_ino == original_inode
    if case != "tampered-sidecar":
        assert sidecar.read_bytes() == original_sidecar


@pytest.mark.asyncio
async def test_restore_submits_official_company_import(tmp_path, monkeypatch):
    artifact = tmp_path / "invoiceninja-export.zip"
    artifact.write_bytes(_company_export_zip())
    _authorize_invoice_restore(monkeypatch)
    uploaded = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path in RESTORE_RESOURCE_PATHS:
            if uploaded and request.url.path == "/api/v1/clients":
                return _resource_response([_restored_client_row()])
            if uploaded and request.url.path == "/api/v1/invoices":
                return _resource_response([_restored_invoice_row()])
            return _empty_resource_response()
        assert request.url.path == "/api/v1/import_json"
        assert request.headers["X-API-Token"] == "destination-token-must-not-escape"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert b"invoiceninja-export.zip" in request.content
        assert b"import_data" in request.content
        assert b"import_settings" in request.content
        uploaded = True
        return httpx.Response(200, json={"message": "Processing", "success": True})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    result = await InvoiceNinjaPlugin(name="invoiceninja").restore(
        _restore_preflight_context(artifact)
    )

    assert result["status"] == "partial"
    assert "queued" in result["message"].lower()


@pytest.mark.asyncio
async def test_backup_rejects_worker_evidence_from_an_aba_replaced_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    original_payload = _invoice_export_zip()
    replacement_json = _exact_backup_json()
    replacement_json["company"] = {"settings": {"name": "replacement-company"}}
    replacement_payload = _invoice_export_zip(backup_json=replacement_json)
    _install_ready_export_transport(monkeypatch, original_payload)
    validation_path: Path | None = None

    def record_validation_path(path: Path) -> tuple[object, object]:
        nonlocal validation_path
        validation_path = path
        return object(), object()

    async def return_replacement_evidence(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert validation_path is not None
        held_original = validation_path.with_name(f"{validation_path.name}.held")
        validation_path.rename(held_original)
        validation_path.write_bytes(replacement_payload)
        replacement_status = validation_path.stat(follow_symlinks=False)
        evidence: dict[str, object] = {
            "member_count": 4,
            "size_bytes": len(replacement_payload),
            "sha256": hashlib.sha256(replacement_payload).hexdigest(),
            "device": replacement_status.st_dev,
            "inode": replacement_status.st_ino,
        }
        validation_path.unlink()
        held_original.rename(validation_path)
        return evidence

    monkeypatch.setattr(
        plugin_module,
        "_start_validation_process",
        record_validation_path,
    )
    monkeypatch.setattr(
        plugin_module,
        "_await_validation_process",
        return_replacement_evidence,
    )
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    with pytest.raises(RuntimeError, match="validation|changed|identity"):
        await plugin.backup(
            BackupContext(
                job_id="aba-worker-evidence",
                target_id="invoice",
                config={"base_url": "http://invoice.local", "token": "synthetic-token"},
                metadata={"target_slug": "invoice-aba"},
            )
        )

    assert _backup_files(backup_root) == []


@pytest.mark.asyncio
async def test_backup_stops_stream_before_archive_size_bound_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"a" * 8, b"b" * 8, b"private-third-chunk"):
                self.yielded += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    backup_root = tmp_path / "backups"
    stream = CountedStream()
    monkeypatch.setattr(plugin_module, "_MAX_ARCHIVE_BYTES", 10, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            stream=stream,
        )

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    with pytest.raises(RuntimeError, match="size|bound|large"):
        await plugin.backup(
            BackupContext(
                job_id="bounded-stream",
                target_id="invoice",
                config={"base_url": "http://invoice.local", "token": "synthetic-token"},
                metadata={"target_slug": "invoice-bounded-stream"},
            )
        )

    assert stream.yielded == 2
    assert stream.closed
    assert _backup_files(backup_root) == []


@pytest.mark.asyncio
async def test_backup_streaming_obeys_the_fixed_export_wall_clock_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClockAdvancingStream(httpx.AsyncByteStream):
        def __init__(self, chunks: tuple[bytes, ...], clock: list[float]) -> None:
            self._chunks = chunks
            self._clock = clock
            self.yielded = 0
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                self._clock[0] += 31.0
                self.yielded += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    backup_root = tmp_path / "backups"
    payload = _invoice_export_zip()
    split = len(payload) // 3
    clock = [100.0]
    stream = ClockAdvancingStream(
        (payload[:split], payload[split : split * 2], payload[split * 2 :]),
        clock,
    )
    monkeypatch.setattr(plugin_module, "_monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.url.path == "/api/v1/export":
            return httpx.Response(
                200,
                json={
                    "message": "Processing",
                    "url": (
                        f"http://invoice.local/api/v1/protected_download/{SIGNED_EXPORT_UUID}"
                        "?expires=1700000000&signature=synthetic-signature"
                    ),
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            stream=stream,
        )

    _install_transport(monkeypatch, handler)
    plugin = get_plugin("invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(backup_root))

    with pytest.raises(RuntimeError, match="timed out|deadline"):
        await plugin.backup(
            BackupContext(
                job_id="stream-deadline",
                target_id="invoice",
                config={
                    "base_url": "http://invoice.local",
                    "token": "synthetic-token",
                    "export_timeout_seconds": 60,
                },
                metadata={"target_slug": "invoice-stream-deadline"},
            )
        )

    assert stream.yielded == 2
    assert stream.closed
    assert _backup_files(backup_root) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("archive-validation", "marker-validation"))
async def test_restore_validation_consumes_the_same_fixed_wall_clock_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    artifact = tmp_path / f"invoice-restore-{stage}.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_module, "_RESTORE_MARKER_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(plugin_module, "_monotonic", lambda: clock[0])
    original_validate = InvoiceNinjaPlugin._validate_export
    original_markers = plugin_module._restore_markers
    validation_path: Path | None = None

    def record_validation_path(path: Path) -> tuple[object, object]:
        nonlocal validation_path
        validation_path = path
        return object(), object()

    async def return_delayed_valid_evidence(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert validation_path is not None
        member_count = original_validate(InvoiceNinjaPlugin("invoiceninja"), validation_path)
        if stage == "archive-validation":
            clock[0] += 2.0
        markers = original_markers(validation_path)
        if stage == "marker-validation":
            clock[0] += 2.0
        status = validation_path.stat()
        payload = validation_path.read_bytes()
        return {
            "member_count": member_count,
            "size_bytes": status.st_size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "device": status.st_dev,
            "inode": status.st_ino,
            "markers": markers,
        }

    monkeypatch.setattr(
        plugin_module,
        "_start_validation_process",
        record_validation_path,
    )
    monkeypatch.setattr(
        plugin_module,
        "_await_validation_process",
        return_delayed_valid_evidence,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Expired restore validation reached network I/O")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="timed out|deadline"):
        await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert requests == []
    assert artifact.is_file()


@pytest.mark.asyncio
async def test_restore_marker_request_is_cancelled_by_the_fixed_marker_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-request-deadline.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    monkeypatch.setattr(plugin_module, "_RESTORE_MARKER_TIMEOUT_SECONDS", 0.01)
    validation_path: Path | None = None

    def record_validation_path(path: Path) -> tuple[object, object]:
        nonlocal validation_path
        validation_path = path
        return object(), object()

    async def return_immediate_valid_evidence(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert validation_path is not None
        status = validation_path.stat()
        payload = validation_path.read_bytes()
        return {
            "member_count": InvoiceNinjaPlugin("invoiceninja")._validate_export(validation_path),
            "size_bytes": status.st_size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "device": status.st_dev,
            "inode": status.st_ino,
            "markers": plugin_module._restore_markers(validation_path),
        }

    monkeypatch.setattr(plugin_module, "_start_validation_process", record_validation_path)
    monkeypatch.setattr(
        plugin_module,
        "_await_validation_process",
        return_immediate_valid_evidence,
    )
    uploaded = False
    blocked_request_cancelled = asyncio.Event()
    never_release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        if request.url.path == "/api/v1/clients":
            try:
                await never_release.wait()
            finally:
                blocked_request_cancelled.set()
        return _empty_resource_response()

    _install_async_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="markers.*ready|timed out|deadline"):
        await asyncio.wait_for(
            get_plugin("invoiceninja").restore(_restore_preflight_context(artifact)),
            timeout=0.2,
        )

    assert blocked_request_cancelled.is_set()
    assert artifact.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_kind", ("userinfo", "path", "query", "fragment", "non-http"))
@pytest.mark.parametrize("metadata_field", ("allowlist", "source"))
async def test_restore_rejects_non_origin_allowlist_and_source_provenance_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_kind: str,
    metadata_field: str,
) -> None:
    artifact = tmp_path / f"invoice-invalid-{metadata_field}-{origin_kind}.zip"
    artifact.write_bytes(_invoice_export_zip())
    invalid_origins = {
        "userinfo": "https://user:secret@{host}",
        "path": "https://{host}/api",
        "query": "https://{host}?token=secret",
        "fragment": "https://{host}#private",
        "non-http": "ftp://{host}",
    }
    destination_invalid = invalid_origins[origin_kind].format(host="invoice-restore.local")
    source_invalid = invalid_origins[origin_kind].format(host="invoice-source.local")
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_invalid if metadata_field == "allowlist" else "https://invoice-restore.local",
    )
    source_origin = source_invalid if metadata_field == "source" else RESTORE_SOURCE_ORIGIN
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Invalid origin reached network I/O")

    _install_transport(monkeypatch, handler)

    with pytest.raises((RuntimeError, ValueError), match="origin|allowlist|source"):
        await get_plugin("invoiceninja").restore(
            _restore_preflight_context(artifact, source_origin=source_origin)
        )

    assert requests == []


@pytest.mark.asyncio
async def test_restore_fresh_preflight_requires_pagination_total_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-nonzero-pagination-total.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if request.method == "POST":
            raise AssertionError("Ambiguous fresh-destination response reached upload")
        if request.url.path == "/api/v1/clients":
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "meta": {
                        "pagination": {
                            "total": 1,
                            "count": 0,
                            "per_page": 20,
                            "current_page": 1,
                            "total_pages": 1,
                            "links": [],
                        }
                    },
                },
            )
        return _empty_resource_response()

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="fresh|empty|pagination"):
        await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert not any(request.method == "POST" for request in requests)


@pytest.mark.asyncio
async def test_restore_paginates_and_verifies_relational_content_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_json = _exact_backup_json()
    backup_json["clients"] = [
        {
            "id": 1,
            "hashed_id": "source-client-hashed-id",
            "name": RESTORE_CLIENT_MARKER,
            "id_number": "client-id-number",
        },
        {
            "id": 2,
            "hashed_id": "wrong-client-id",
            "name": RESTORE_CLIENT_MARKER,
            "id_number": "wrong-id-number",
        },
    ]
    backup_json["client_contacts"] = [
        {
            "id": 1,
            "client_id": "source-client-hashed-id",
            "email": "client@example.test",
        },
        {
            "id": 2,
            "client_id": "wrong-client-id",
            "email": "wrong@example.test",
        },
    ]
    backup_json["invoices"] = [
        {
            "id": 1,
            "client_id": "source-client-hashed-id",
            "number": RESTORE_INVOICE_MARKER,
            "public_notes": "public-note-marker",
            "private_notes": "private-note-marker",
            "line_items": [{"product_key": "product-key-marker", "notes": "line-note-marker"}],
        },
        {
            "id": 2,
            "client_id": "wrong-client-id",
            "number": RESTORE_INVOICE_MARKER,
            "public_notes": "wrong-public-note",
            "private_notes": "wrong-private-note",
            "line_items": [{"product_key": "wrong-product-key", "notes": "wrong-line-note"}],
        },
    ]
    artifact = tmp_path / "invoice-relational-markers.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    _authorize_invoice_restore(monkeypatch)
    uploaded = False
    page_two_requests: set[str] = set()

    def paged_response(data: list[object], page: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": data,
                "meta": {
                    "pagination": {
                        "total": 2,
                        "count": 1,
                        "per_page": 1,
                        "current_page": page,
                        "total_pages": 2,
                        "links": [],
                    }
                },
            },
        )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        page = int(request.url.params.get("page", "1"))
        resource = request.url.path.rsplit("/", 1)[-1]
        if page == 2:
            page_two_requests.add(resource)
        if resource == "clients":
            client = {
                "id": "source-client-hashed-id" if page == 2 else "wrong-client-id",
                "name": RESTORE_CLIENT_MARKER,
                "id_number": "client-id-number" if page == 2 else "wrong-id-number",
                "contacts": [
                    {"email": ("client@example.test" if page == 2 else "wrong@example.test")}
                ],
            }
            return paged_response([client], page)
        if resource == "invoices":
            invoice = {
                "id": "source-invoice-hashed-id",
                "client_id": ("source-client-hashed-id" if page == 2 else "wrong-client-id"),
                "number": RESTORE_INVOICE_MARKER,
                "public_notes": "public-note-marker" if page == 2 else "wrong-public-note",
                "private_notes": "private-note-marker" if page == 2 else "wrong-private-note",
                "line_items": [
                    {
                        "product_key": ("product-key-marker" if page == 2 else "wrong-product-key"),
                        "notes": "line-note-marker" if page == 2 else "wrong-line-note",
                    }
                ],
            }
            return paged_response([invoice], page)
        raise AssertionError(f"Unexpected marker request: {request.url}")

    _install_transport(monkeypatch, handler)

    result = await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert result["status"] == "partial"
    assert page_two_requests == {"clients", "invoices"}


def test_restore_marker_matching_requires_distinct_destination_clients(tmp_path: Path) -> None:
    backup_json = _exact_backup_json()
    backup_json["clients"] = [
        {
            "id": index,
            "hashed_id": f"source-client-{index}",
            "name": "duplicate-client-marker",
            "id_number": "duplicate-client-number",
        }
        for index in (1, 2)
    ]
    backup_json["client_contacts"] = []
    backup_json["invoices"] = []
    artifact = tmp_path / "invoice-duplicate-client-markers.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))

    markers = plugin_module._restore_markers(artifact)
    destination_clients = [
        {
            "id": "one-destination-client",
            "name": "duplicate-client-marker",
            "id_number": "duplicate-client-number",
            "contacts": [],
        }
    ]

    assert not plugin_module._restored_content_matches(markers, destination_clients, [])


def test_restore_marker_matching_requires_distinct_destination_invoices(tmp_path: Path) -> None:
    backup_json = _exact_backup_json()
    source_invoice = {
        "client_id": RESTORE_CLIENT_SOURCE_ID,
        "number": "duplicate-invoice-marker",
        "public_notes": "duplicate-public-note",
        "private_notes": "duplicate-private-note",
        "line_items": [{"product_key": "duplicate-product", "notes": "duplicate-line"}],
    }
    backup_json["invoices"] = [
        {"id": 1, **source_invoice},
        {"id": 2, **source_invoice},
    ]
    artifact = tmp_path / "invoice-duplicate-invoice-markers.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))

    markers = plugin_module._restore_markers(artifact)
    destination_clients = [
        {
            "id": "destination-client",
            "name": RESTORE_CLIENT_MARKER,
            "id_number": RESTORE_CLIENT_ID_NUMBER,
            "contacts": [{"email": RESTORE_CLIENT_EMAIL}],
        }
    ]
    destination_invoices = [
        {
            "id": "one-destination-invoice",
            "client_id": "destination-client",
            "number": "duplicate-invoice-marker",
            "public_notes": "duplicate-public-note",
            "private_notes": "duplicate-private-note",
            "line_items": [{"product_key": "duplicate-product", "notes": "duplicate-line"}],
        }
    ]

    assert not plugin_module._restored_content_matches(
        markers,
        destination_clients,
        destination_invoices,
    )


def test_restore_marker_matching_preserves_contact_email_multiplicity(tmp_path: Path) -> None:
    backup_json = _exact_backup_json()
    duplicate_contact = {
        "client_id": RESTORE_CLIENT_SOURCE_ID,
        "email": RESTORE_CLIENT_EMAIL,
    }
    backup_json["client_contacts"] = [
        {"id": 1, **duplicate_contact},
        {"id": 2, **duplicate_contact},
    ]
    backup_json["invoices"] = []
    artifact = tmp_path / "invoice-duplicate-contact-markers.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))

    markers = plugin_module._restore_markers(artifact)
    destination_clients = [
        {
            "id": "destination-client",
            "name": RESTORE_CLIENT_MARKER,
            "id_number": RESTORE_CLIENT_ID_NUMBER,
            "contacts": [{"email": RESTORE_CLIENT_EMAIL}],
        }
    ]

    assert not plugin_module._restored_content_matches(markers, destination_clients, [])


@pytest.mark.parametrize("source_is_empty", (False, True))
def test_restore_marker_matching_rejects_unexpected_destination_business_units(
    tmp_path: Path,
    source_is_empty: bool,
) -> None:
    backup_json = _exact_backup_json()
    if source_is_empty:
        backup_json["clients"] = []
        backup_json["client_contacts"] = []
        backup_json["invoices"] = []
    artifact = tmp_path / f"invoice-extra-destination-{source_is_empty}.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    markers = plugin_module._restore_markers(artifact)
    destination_clients = [] if source_is_empty else [_restored_client_row()]
    destination_invoices = [] if source_is_empty else [_restored_invoice_row()]
    destination_clients.append(
        {
            "id": "unexpected-destination-client",
            "name": "unexpected-client",
            "id_number": "unexpected-number",
            "contacts": [],
        }
    )

    assert not plugin_module._restored_content_matches(
        markers,
        destination_clients,
        destination_invoices,
    )


def test_restore_marker_matching_canonicalizes_distinct_invoice_lines(tmp_path: Path) -> None:
    backup_json = _exact_backup_json()
    backup_json["invoices"] = [
        {
            "id": index,
            "client_id": RESTORE_CLIENT_SOURCE_ID,
            "number": "shared-number",
            "public_notes": "shared-public-notes",
            "private_notes": "shared-private-notes",
            "line_items": [{"product_key": product_key, "notes": notes}],
        }
        for index, (product_key, notes) in enumerate(
            (("product-b", "notes-b"), ("product-a", "notes-a")),
            start=1,
        )
    ]
    artifact = tmp_path / "invoice-sortable-line-markers.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    markers = plugin_module._restore_markers(artifact)
    destination_invoices = [
        {
            "id": f"destination-invoice-{index}",
            "client_id": "destination-client",
            "number": "shared-number",
            "public_notes": "shared-public-notes",
            "private_notes": "shared-private-notes",
            "line_items": [{"product_key": product_key, "notes": notes}],
        }
        for index, (product_key, notes) in enumerate(
            (("product-a", "notes-a"), ("product-b", "notes-b")),
            start=1,
        )
    ]

    assert plugin_module._restored_content_matches(
        markers,
        [
            {
                "id": "destination-client",
                "name": RESTORE_CLIENT_MARKER,
                "id_number": RESTORE_CLIENT_ID_NUMBER,
                "contacts": [{"email": RESTORE_CLIENT_EMAIL}],
            }
        ],
        destination_invoices,
    )


@pytest.mark.parametrize(
    "ambiguous_url",
    (
        "synthetic-company/./documents/document-hash.txt",
        "synthetic-company//documents/document-hash.txt",
    ),
)
def test_strict_export_validator_rejects_ambiguous_zip_path_separators(
    tmp_path: Path,
    ambiguous_url: str,
) -> None:
    backup_json = _exact_backup_json()
    backup_json["documents"] = [
        {
            "url": ambiguous_url,
            "hash": "document-hash.txt",
            "size": len(DOCUMENT_BYTES),
        }
    ]
    case = "dot" if "/./" in ambiguous_url else "repeated-slash"
    artifact = tmp_path / f"invoice-ambiguous-{case}.zip"
    artifact.write_bytes(
        _invoice_export_zip(
            backup_json=backup_json,
            members=[
                ("backup.json", json.dumps(backup_json, sort_keys=True).encode()),
                ("company_logo.png", b"synthetic-logo"),
                (f"documents/{ambiguous_url}", DOCUMENT_BYTES),
                ("backups/previous-export.zip", b"synthetic-previous-export"),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="unsafe|document"):
        InvoiceNinjaPlugin("invoiceninja")._validate_export(artifact)


@pytest.mark.asyncio
async def test_spawned_validation_drains_large_marker_evidence_before_reaping_worker(
    tmp_path: Path,
) -> None:
    backup_json = _exact_backup_json()
    backup_json["clients"] = [
        {
            "id": index,
            "hashed_id": f"source-client-{index}",
            "name": hashlib.sha512(f"client-{index}".encode()).hexdigest() * 4,
            "id_number": f"id-number-{index}",
        }
        for index in range(4096)
    ]
    backup_json["client_contacts"] = []
    backup_json["invoices"] = []
    artifact = tmp_path / "invoice-large-validation-evidence.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    expected_markers = plugin_module._restore_markers(artifact)
    assert len(pickle.dumps(expected_markers)) > 2 * 1024 * 1024

    process, connection = plugin_module._start_validation_process(artifact)
    try:
        evidence = await asyncio.wait_for(
            plugin_module._await_validation_process(
                process,
                connection,
                timeout_seconds=5.0,
            ),
            timeout=10.0,
        )

        markers = evidence.get("markers")
        assert markers == expected_markers
        assert process.exitcode == 0
        assert not process.is_alive()
        assert connection.closed
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 2.0)
        if process.exitcode is not None:
            process.close()


@pytest.mark.asyncio
async def test_restore_hash_verification_consumes_the_fixed_parent_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-hash-deadline.zip"
    artifact.write_bytes(_invoice_export_zip())
    context = _restore_preflight_context(artifact)
    _authorize_invoice_restore(monkeypatch)
    clock = [100.0]
    real_sha256 = hashlib.sha256
    validation_paths: list[Path] = []
    requests: list[httpx.Request] = []

    def forbid_parent_hash(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Restore hash verification ran synchronously in the parent")

    def record_validation(path: Path) -> tuple[object, object]:
        validation_paths.append(path)
        return object(), object()

    async def return_expired_hash_evidence(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert len(validation_paths) == 1
        path = validation_paths[0]
        status = path.stat()
        payload = path.read_bytes()
        evidence = {
            "member_count": InvoiceNinjaPlugin("invoiceninja")._validate_export(path),
            "size_bytes": status.st_size,
            "sha256": real_sha256(payload).hexdigest(),
            "device": status.st_dev,
            "inode": status.st_ino,
            "markers": plugin_module._restore_markers(path),
        }
        clock[0] += 2.0
        return evidence

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Expired staged hash verification reached network mutation")

    monkeypatch.setattr(plugin_module, "_RESTORE_MARKER_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(plugin_module, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(plugin_module.hashlib, "sha256", forbid_parent_hash)
    monkeypatch.setattr(plugin_module, "_start_validation_process", record_validation)
    monkeypatch.setattr(
        plugin_module,
        "_await_validation_process",
        return_expired_hash_evidence,
    )
    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="deadline|timed out"):
        await get_plugin("invoiceninja").restore(context)

    assert len(validation_paths) == 1
    assert requests == []


@pytest.mark.asyncio
async def test_restore_hash_verification_stops_at_cancellation_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "invoice-restore-hash-cancellation.zip"
    artifact.write_bytes(_invoice_export_zip())
    context = _restore_preflight_context(artifact)
    _authorize_invoice_restore(monkeypatch)
    validation_started = asyncio.Event()
    validation_cancelled = asyncio.Event()
    never_release_validation = asyncio.Event()
    requests: list[httpx.Request] = []

    def forbid_parent_hash(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Restore hash verification ran synchronously in the parent")

    def start_blocked_hash_verification(_path: Path) -> tuple[object, object]:
        return object(), object()

    async def block_hash_verification(
        _process: object,
        _connection: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        validation_started.set()
        try:
            await never_release_validation.wait()
        finally:
            validation_cancelled.set()
        raise AssertionError("Cancelled hash verification resumed")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Cancelled staged hash verification reached network mutation")

    monkeypatch.setattr(plugin_module.hashlib, "sha256", forbid_parent_hash)
    monkeypatch.setattr(
        plugin_module,
        "_start_validation_process",
        start_blocked_hash_verification,
    )
    monkeypatch.setattr(
        plugin_module,
        "_await_validation_process",
        block_hash_verification,
    )
    _install_transport(monkeypatch, handler)
    restore_task = asyncio.create_task(get_plugin("invoiceninja").restore(context))
    await asyncio.wait_for(validation_started.wait(), timeout=1.0)
    restore_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await restore_task

    assert validation_cancelled.is_set()
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("deadline", "cancellation"))
async def test_restore_origin_lock_wait_obeys_deadline_and_cancellation_without_unlocking_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    artifact = tmp_path / f"invoice-restore-lock-{mode}.zip"
    artifact.write_bytes(_invoice_export_zip())
    _authorize_invoice_restore(monkeypatch)
    _install_immediate_restore_validation(monkeypatch)
    monkeypatch.setattr(
        plugin_module,
        "_RESTORE_MARKER_TIMEOUT_SECONDS",
        0.01 if mode == "deadline" else 60.0,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Restore waiting for the origin lock reached network I/O")

    _install_transport(monkeypatch, handler)
    lock = plugin_module._origin_lock(plugin_module._canonical_origin(RESTORE_DESTINATION_ORIGIN))
    assert lock.acquire(blocking=False)
    task = asyncio.create_task(
        get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))
    )
    completed_by_deadline = True
    try:
        if mode == "deadline":
            await asyncio.sleep(0.15)
            completed_by_deadline = task.done()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        else:
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert lock.locked()
        assert requests == []
        assert completed_by_deadline
        if mode == "deadline":
            with pytest.raises(RuntimeError, match="deadline|timed out"):
                await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        if lock.locked():
            lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("deadline", "cancellation"))
async def test_backup_origin_lock_wait_obeys_deadline_and_cancellation_without_unlocking_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Backup waiting for the origin lock reached network I/O")

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(InvoiceNinjaPlugin, "_base_dir", lambda _self: str(tmp_path))
    if mode == "deadline":
        clock = [100.0]

        def advancing_clock() -> float:
            clock[0] += 61.0
            return clock[0]

        monkeypatch.setattr(plugin_module, "_monotonic", advancing_clock)

    origin = "https://invoice-backup-lock.local"
    lock = plugin_module._origin_lock(plugin_module._canonical_origin(origin))
    assert lock.acquire(blocking=False)
    task = asyncio.create_task(
        get_plugin("invoiceninja").backup(
            BackupContext(
                job_id=f"backup-lock-{mode}",
                target_id=f"backup-lock-{mode}",
                config={
                    "base_url": origin,
                    "token": "synthetic-token",
                    "export_timeout_seconds": 60,
                },
                metadata={"target_slug": f"backup-lock-{mode}"},
            )
        )
    )
    try:
        if mode == "deadline":
            with pytest.raises(RuntimeError, match="deadline|timed out"):
                await asyncio.wait_for(task, timeout=1.0)
        else:
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert lock.locked()
        assert requests == []
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        if lock.locked():
            lock.release()


@pytest.mark.asyncio
async def test_restore_retries_transient_pagination_drift_until_rich_snapshot_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_json = _exact_backup_json()
    clients = backup_json["clients"]
    assert isinstance(clients, list)
    clients.append(
        {
            "id": 2,
            "hashed_id": "stable-decoy",
            "name": "stable-decoy",
            "id_number": "stable-decoy",
        }
    )
    artifact = tmp_path / "invoice-restore-transient-pagination.zip"
    artifact.write_bytes(_invoice_export_zip(backup_json=backup_json))
    _authorize_invoice_restore(monkeypatch)
    _install_immediate_restore_validation(monkeypatch)
    monkeypatch.setattr(plugin_module, "_RESTORE_POLL_INTERVAL_SECONDS", 0.0)
    uploaded = False
    client_snapshot = 0

    def page_response(
        rows: list[object],
        *,
        page: int,
        total: int,
        total_pages: int,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": rows,
                "meta": {
                    "pagination": {
                        "total": total,
                        "count": len(rows),
                        "per_page": 100,
                        "current_page": page,
                        "total_pages": total_pages,
                        "links": [],
                    }
                },
            },
        )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal client_snapshot, uploaded
        if request.method == "POST":
            uploaded = True
            return httpx.Response(200, json={"message": "Processing", "success": True})
        if request.url.path == "/api/v1/ping":
            return _ping_response()
        if not uploaded:
            return _empty_resource_response()
        page = int(request.url.params.get("page", "1"))
        if request.url.path == "/api/v1/clients":
            if page == 1:
                client_snapshot += 1
            if client_snapshot == 1:
                total = 2 if page == 1 else 3
                return page_response(
                    [{"id": f"transient-client-{page}"}],
                    page=page,
                    total=total,
                    total_pages=2,
                )
            return page_response(
                (
                    [
                        {
                            "id": "stable-decoy",
                            "name": "stable-decoy",
                            "id_number": "stable-decoy",
                            "contacts": [],
                        }
                    ]
                    if page == 1
                    else [_restored_client_row()]
                ),
                page=page,
                total=2,
                total_pages=2,
            )
        if request.url.path == "/api/v1/invoices":
            return page_response(
                [_restored_invoice_row()],
                page=1,
                total=1,
                total_pages=1,
            )
        raise AssertionError(f"Unexpected marker request: {request.url}")

    _install_transport(monkeypatch, handler)

    result = await get_plugin("invoiceninja").restore(_restore_preflight_context(artifact))

    assert result["status"] == "partial"
    assert client_snapshot == 2
