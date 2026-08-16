from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import stat
import threading
import time
import zipfile
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

_LOGGER = logging.getLogger(__name__)

_ORIGIN_LOCKS: dict[str, threading.Lock] = {}
_ORIGIN_LOCKS_GUARD = threading.Lock()
_PROTECTED_DOWNLOAD_PATH = re.compile(
    r"^/api/v1/protected_download/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-" r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_MEMBER_COMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 100.0
_MAX_MEMBER_PATH_DEPTH = 16
_MAX_BACKUP_JSON_BYTES = 128 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 1024 * 1024 * 1024
_VALIDATION_TIMEOUT_SECONDS = 120.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
_RESTORE_MARKER_TIMEOUT_SECONDS = 120.0
_RESTORE_POLL_INTERVAL_SECONDS = 2.0
_MAX_RESTORE_MARKER_ROWS = 100_000
_monotonic = time.monotonic
_ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
_ISOLATED_RESTORE_ORIGINS_ENV = "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS"
_FRESH_RESTORE_RESOURCE_PATHS = tuple(
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
_ARRAY_FIELDS = (
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
_BACKUP_JSON_FIELDS = frozenset({"app_version", "storage_url", "company", *_ARRAY_FIELDS})


@dataclass(frozen=True)
class _ClientMarker:
    source_id: str
    name: str
    id_number: str
    contact_emails: tuple[str, ...]


@dataclass(frozen=True)
class _InvoiceLineMarker:
    product_key: str
    notes: str


@dataclass(frozen=True)
class _InvoiceMarker:
    source_client_id: str
    number: str
    public_notes: str
    private_notes: str
    lines: tuple[_InvoiceLineMarker, ...]


@dataclass(frozen=True)
class _RestoreMarkers:
    company_name: str
    clients: tuple[_ClientMarker, ...]
    invoices: tuple[_InvoiceMarker, ...]


class _TransientMarkerSnapshot(RuntimeError):
    """The asynchronous import changed while one paginated snapshot was read."""


def _safe_archive_name(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        return False
    parts = name.split("/")
    return (
        bool(parts)
        and len(parts) <= _MAX_MEMBER_PATH_DEPTH
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _zip_has_no_trailing_data(path: Path) -> bool:
    size = path.stat().st_size
    tail_size = min(size, 65_557)
    with path.open("rb") as archive_file:
        archive_file.seek(size - tail_size)
        tail = archive_file.read(tail_size)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or marker + 22 > len(tail):
        return False
    comment_length = int.from_bytes(tail[marker + 20 : marker + 22], "little")
    return marker + 22 + comment_length == len(tail)


def _validation_process_worker(path: Path, connection: Connection) -> None:
    descriptor = -1
    try:
        flags = os.O_RDONLY
        parent_descriptor_path = re.fullmatch(r"/proc/[0-9]+/fd/[0-9]+", str(path))
        if parent_descriptor_path is None and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("Invoice Ninja validation input is not a regular file")
        bound_path = Path(f"/proc/self/fd/{descriptor}")
        plugin = InvoiceNinjaPlugin("invoiceninja")
        member_count = plugin._validate_export(bound_path)
        markers = _restore_markers(bound_path)
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        completed = os.fstat(descriptor)
        if (
            opened.st_dev != completed.st_dev
            or opened.st_ino != completed.st_ino
            or opened.st_size != completed.st_size
        ):
            raise RuntimeError("Invoice Ninja validation input changed")
        connection.send(
            (
                "ok",
                {
                    "member_count": member_count,
                    "size_bytes": completed.st_size,
                    "sha256": digest.hexdigest(),
                    "device": completed.st_dev,
                    "inode": completed.st_ino,
                    "markers": markers,
                },
            )
        )
    except BaseException as exc:
        try:
            connection.send(("error", str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        connection.close()


def _start_validation_process(path: Path) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_validation_process_worker,
        args=(path, sending),
        name="invoiceninja-validation",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _join_worker_process(process: BaseProcess, timeout_seconds: float) -> None:
    await asyncio.to_thread(process.join, timeout_seconds)


async def _stop_worker_process(process: BaseProcess) -> None:
    if not process.is_alive():
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError("Invoice Ninja validation worker could not be reaped")
        return
    process.terminate()
    await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError("Invoice Ninja validation worker could not be stopped")


async def _stop_worker_process_before_return(process: BaseProcess) -> None:
    stop_task = asyncio.create_task(_stop_worker_process(process))
    cancellation_seen = False
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            cancellation_seen = True
    stop_task.result()
    if cancellation_seen:
        raise asyncio.CancelledError


async def _await_validation_process(
    process: BaseProcess,
    connection: Connection,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    receive_task = asyncio.create_task(asyncio.to_thread(connection.recv))
    try:
        if timeout_seconds is None:
            timeout_seconds = _VALIDATION_TIMEOUT_SECONDS
        await _join_worker_process(process, timeout_seconds)
        if process.is_alive():
            await _stop_worker_process_before_return(process)
            raise TimeoutError("Invoice Ninja validation timed out")
        result: tuple[object, object] | None = None
        try:
            received = await asyncio.wait_for(
                asyncio.shield(receive_task),
                timeout=_WORKER_STOP_TIMEOUT_SECONDS,
            )
        except (EOFError, OSError, TimeoutError):
            received = None
        if isinstance(received, tuple) and len(received) == 2:
            result = received
        if result is None:
            raise RuntimeError("Invoice Ninja validation worker returned no result")
        kind, payload = result
        if kind != "ok":
            raise RuntimeError(
                payload
                if isinstance(payload, str) and payload
                else "Invoice Ninja validation failed"
            )
        if not isinstance(payload, dict):
            raise RuntimeError("Invoice Ninja validation worker returned an invalid result")
        if process.exitcode != 0:
            raise RuntimeError("Invoice Ninja validation worker failed")
        return payload
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process)
        raise
    finally:
        connection.close()
        if not receive_task.done():
            receive_task.cancel()
        try:
            await receive_task
        except BaseException:
            pass


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("Invoice Ninja origin has no hostname")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return f"{parsed.scheme.lower()}://{normalized_host}:{port}"


def _require_clean_http_origin(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Invoice Ninja origin must be a nonempty string")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        raise ValueError("Invoice Ninja origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed_port is not None
        and not 1 <= parsed_port <= 65535
    ):
        raise ValueError("Invoice Ninja origin is invalid")
    return _canonical_origin(value)


def _origin_lock(origin: str) -> threading.Lock:
    with _ORIGIN_LOCKS_GUARD:
        return _ORIGIN_LOCKS.setdefault(origin, threading.Lock())


@asynccontextmanager
async def _hold_lock(
    lock: threading.Lock,
    *,
    deadline: float,
) -> AsyncIterator[None]:
    while not lock.acquire(blocking=False):
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise RuntimeError("Invoice Ninja operation lock deadline expired")
        try:
            async with asyncio.timeout(remaining):
                await asyncio.sleep(min(0.05, remaining))
        except TimeoutError:
            raise RuntimeError("Invoice Ninja operation lock deadline expired") from None
    try:
        yield
    finally:
        lock.release()


async def _stream_bounded_export(
    response: httpx.Response,
    destination: Any,
    *,
    deadline: float,
) -> int:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            raise RuntimeError("Invoice Ninja export download returned invalid media") from None
        if declared_bytes < 0 or declared_bytes > _MAX_ARCHIVE_BYTES:
            raise RuntimeError("Invoice Ninja export exceeds its archive bound")
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise RuntimeError("Invoice Ninja export download timed out")
    written = 0
    try:
        async with asyncio.timeout(remaining):
            async for chunk in response.aiter_bytes():
                if _monotonic() >= deadline:
                    raise RuntimeError("Invoice Ninja export download timed out")
                written += len(chunk)
                if written > _MAX_ARCHIVE_BYTES:
                    raise RuntimeError("Invoice Ninja export exceeds its archive bound")
                destination.write(chunk)
    except TimeoutError:
        raise RuntimeError("Invoice Ninja export download timed out") from None
    return written


async def _sleep_before_deadline(seconds: float, *, deadline: float) -> None:
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise RuntimeError("Invoice Ninja export download timed out")
    try:
        async with asyncio.timeout(remaining):
            await asyncio.sleep(min(seconds, remaining))
    except TimeoutError:
        raise RuntimeError("Invoice Ninja export download timed out") from None


async def _download_and_validate_export_attempt(
    client: httpx.AsyncClient,
    download_url: str,
    headers: dict[str, str],
    artifact: Any,
    *,
    deadline: float,
    poll_interval: float,
) -> dict[str, object] | None:
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise RuntimeError("Invoice Ninja export download timed out")
    try:
        async with asyncio.timeout(remaining):
            async with client.stream(
                "GET",
                download_url,
                headers=headers,
                timeout=min(30.0, remaining),
            ) as response:
                if response.status_code in {401, 403}:
                    raise RuntimeError("Invoice Ninja export download authorization expired")
                if response.status_code == 404:
                    await _sleep_before_deadline(poll_interval, deadline=deadline)
                    return None
                if response.status_code != 200:
                    raise RuntimeError(
                        "Invoice Ninja export download returned an unexpected status "
                        f"{response.status_code}"
                    )
                content_type = str(response.headers.get("content-type", "")).lower()
                if "text/html" in content_type:
                    await _sleep_before_deadline(poll_interval, deadline=deadline)
                    return None
                if not (
                    "application/zip" in content_type or "application/octet-stream" in content_type
                ):
                    raise RuntimeError("Invoice Ninja export download returned invalid media")
                artifact.publication_fd = os.open(
                    artifact.temporary_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(os.dup(artifact.publication_fd), "wb") as artifact_file:
                    await _stream_bounded_export(
                        response,
                        artifact_file,
                        deadline=deadline,
                    )
                validation_process, validation_connection = _start_validation_process(
                    artifact.temporary_path
                )
                validation_remaining = deadline - _monotonic()
                if validation_remaining <= 0:
                    await _stop_worker_process_before_return(validation_process)
                    validation_connection.close()
                    raise RuntimeError("Invoice Ninja export download timed out")
                return await _await_validation_process(
                    validation_process,
                    validation_connection,
                    timeout_seconds=min(
                        _VALIDATION_TIMEOUT_SECONDS,
                        validation_remaining,
                    ),
                )
    except TimeoutError as exc:
        if str(exc) == "Invoice Ninja validation timed out":
            raise
        raise RuntimeError("Invoice Ninja export download timed out") from None


def _require_signed_download_url(base_url: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Invoice Ninja export returned an invalid download URL")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        raise RuntimeError("Invoice Ninja export returned an invalid download URL") from None
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    signed_query = {key: item for key, item in query_pairs}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed_port is not None
        and not 1 <= parsed_port <= 65535
        or _PROTECTED_DOWNLOAD_PATH.fullmatch(parsed.path) is None
        or _canonical_origin(value) != _canonical_origin(base_url)
        or len(query_pairs) != 2
        or set(signed_query) != {"expires", "signature"}
        or not all(signed_query.values())
    ):
        raise RuntimeError(
            "Invoice Ninja export returned an unsafe download URL; it must remain on the same origin"
        )
    return value


def _require_isolated_restore_authorization(
    base_url: str,
    metadata: dict[str, Any],
) -> None:
    if os.getenv(_ISOLATED_RESTORE_ENV) != "1":
        raise RuntimeError("Invoice Ninja restore is disabled outside an isolated local drill")
    allowed_value = os.getenv(_ISOLATED_RESTORE_ORIGINS_ENV, "")
    try:
        allowed_origins = {
            _require_clean_http_origin(value.strip())
            for value in allowed_value.split(",")
            if value.strip()
        }
        destination_origin = _require_clean_http_origin(base_url)
    except ValueError:
        raise RuntimeError("Invoice Ninja restore origin allowlist is invalid") from None
    if destination_origin not in allowed_origins:
        raise RuntimeError("Invoice Ninja restore origin is not authorized for this isolated drill")
    source_identity = metadata.get("source_database_identity")
    source_url = source_identity.get("base_url") if isinstance(source_identity, dict) else None
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("Invoice Ninja restore requires verified source origin metadata")
    try:
        source_origin = _require_clean_http_origin(source_url)
    except ValueError:
        raise ValueError("Invoice Ninja restore source origin metadata is invalid") from None
    if source_origin == destination_origin:
        raise ValueError("Invoice Ninja restore source and destination origins must be different")


def _open_verified_restore_artifact(path: Path, metadata: dict[str, Any]) -> tuple[int, int, str]:
    expected_bytes = metadata.get("artifact_bytes")
    expected_sha256 = metadata.get("artifact_sha256")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("Invoice Ninja restore requires verified artifact identity metadata")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("Invoice Ninja restore artifact could not be opened safely") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size != expected_bytes:
            raise ValueError("Invoice Ninja restore artifact size is not verified")
        return descriptor, status.st_size, expected_sha256
    except BaseException:
        os.close(descriptor)
        raise


def _require_bound_validation_evidence(
    evidence: dict[str, object],
    descriptor: int,
    *,
    expected_sha256: str | None = None,
) -> tuple[int, str]:
    member_count = evidence.get("member_count")
    size_bytes = evidence.get("size_bytes")
    sha256 = evidence.get("sha256")
    device = evidence.get("device")
    inode = evidence.get("inode")
    opened = os.fstat(descriptor)
    if (
        isinstance(member_count, bool)
        or not isinstance(member_count, int)
        or member_count <= 0
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino, opened.st_size) != (device, inode, size_bytes)
        or expected_sha256 is not None
        and sha256 != expected_sha256
    ):
        raise RuntimeError("Invoice Ninja validation evidence does not match the bound artifact")
    return member_count, sha256


def _restore_markers(path: Path) -> _RestoreMarkers:
    try:
        with zipfile.ZipFile(path) as archive:
            payload = json.loads(archive.read("backup.json"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        raise RuntimeError("Invoice Ninja restore markers could not be read") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Invoice Ninja restore markers are invalid")
    company = payload.get("company")
    clients = payload.get("clients")
    client_contacts = payload.get("client_contacts")
    invoices = payload.get("invoices")
    company_settings = company.get("settings") if isinstance(company, dict) else None
    company_name = company_settings.get("name") if isinstance(company_settings, dict) else None
    if (
        not isinstance(company_name, str)
        or not company_name
        or not isinstance(clients, list)
        or not isinstance(client_contacts, list)
        or not isinstance(invoices, list)
    ):
        raise RuntimeError("Invoice Ninja restore markers are invalid")

    contacts_by_client: dict[str, list[str]] = {}
    for contact in client_contacts:
        client_id = contact.get("client_id") if isinstance(contact, dict) else None
        email = contact.get("email") if isinstance(contact, dict) else None
        if not isinstance(client_id, str) or not client_id or not isinstance(email, str):
            raise RuntimeError("Invoice Ninja restore markers are invalid")
        contacts_by_client.setdefault(client_id, []).append(email)

    client_markers: list[_ClientMarker] = []
    client_ids: set[str] = set()
    for client in clients:
        if not isinstance(client, dict):
            raise RuntimeError("Invoice Ninja restore markers are invalid")
        source_id = client.get("hashed_id")
        name = client.get("name")
        id_number = client.get("id_number")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in client_ids
            or not isinstance(name, str)
            or not name
            or not isinstance(id_number, str)
        ):
            raise RuntimeError("Invoice Ninja restore markers are invalid")
        client_ids.add(source_id)
        client_markers.append(
            _ClientMarker(
                source_id=source_id,
                name=name,
                id_number=id_number,
                contact_emails=tuple(sorted(contacts_by_client.pop(source_id, []))),
            )
        )
    if contacts_by_client:
        raise RuntimeError("Invoice Ninja restore markers are invalid")

    invoice_markers: list[_InvoiceMarker] = []
    for invoice in invoices:
        if not isinstance(invoice, dict):
            raise RuntimeError("Invoice Ninja restore markers are invalid")
        source_client_id = invoice.get("client_id")
        number = invoice.get("number")
        public_notes = invoice.get("public_notes")
        private_notes = invoice.get("private_notes")
        line_items = invoice.get("line_items")
        if (
            not isinstance(source_client_id, str)
            or source_client_id not in client_ids
            or not isinstance(number, str)
            or not number
            or not isinstance(public_notes, str)
            or not isinstance(private_notes, str)
            or not isinstance(line_items, list)
        ):
            raise RuntimeError("Invoice Ninja restore markers are invalid")
        lines: list[_InvoiceLineMarker] = []
        for line in line_items:
            product_key = line.get("product_key") if isinstance(line, dict) else None
            notes = line.get("notes") if isinstance(line, dict) else None
            if not isinstance(product_key, str) or not isinstance(notes, str):
                raise RuntimeError("Invoice Ninja restore markers are invalid")
            lines.append(_InvoiceLineMarker(product_key=product_key, notes=notes))
        invoice_markers.append(
            _InvoiceMarker(
                source_client_id=source_client_id,
                number=number,
                public_notes=public_notes,
                private_notes=private_notes,
                lines=tuple(lines),
            )
        )
    return _RestoreMarkers(
        company_name=company_name,
        clients=tuple(client_markers),
        invoices=tuple(invoice_markers),
    )


def _resource_page(
    response: httpx.Response,
    *,
    operation: str,
    expected_page: int,
) -> tuple[list[dict[str, Any]], int, int]:
    if response.status_code != 200:
        raise RuntimeError(f"Invoice Ninja {operation} returned status {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"Invoice Ninja {operation} response is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"data", "meta"}:
        raise RuntimeError(f"Invoice Ninja {operation} response is invalid")
    data = payload.get("data")
    meta = payload.get("meta")
    pagination = meta.get("pagination") if isinstance(meta, dict) else None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise RuntimeError(f"Invoice Ninja {operation} response is invalid")
    if not isinstance(pagination, dict):
        raise RuntimeError(f"Invoice Ninja {operation} response is invalid")
    total = pagination.get("total")
    count = pagination.get("count")
    current_page = pagination.get("current_page")
    total_pages = pagination.get("total_pages")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or isinstance(current_page, bool)
        or not isinstance(current_page, int)
        or isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total < 0
        or count != len(data)
        or current_page != expected_page
        or total_pages < 1
        or total_pages > _MAX_RESTORE_MARKER_ROWS
        or total > _MAX_RESTORE_MARKER_ROWS
    ):
        raise RuntimeError(f"Invoice Ninja {operation} response is invalid")
    return data, total_pages, total


async def _all_resource_rows(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    include: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    expected_pages: int | None = None
    page = 1
    while expected_pages is None or page <= expected_pages:
        params = {"page": str(page), "per_page": "100"}
        if include is not None:
            params["include"] = include
        response = await client.get(
            f"{base_url}/api/v1/{resource}",
            headers=headers,
            params=params,
        )
        page_rows, total_pages, total = _resource_page(
            response,
            operation=f"{resource} marker check",
            expected_page=page,
        )
        if expected_pages is None:
            expected_pages = total_pages
            expected_total = total
        elif total_pages != expected_pages or total != expected_total:
            raise _TransientMarkerSnapshot(
                f"Invoice Ninja {resource} pagination changed during verification"
            )
        rows.extend(page_rows)
        if len(rows) > _MAX_RESTORE_MARKER_ROWS:
            raise RuntimeError(f"Invoice Ninja {resource} marker result exceeds its bound")
        page += 1
    if expected_total is None or len(rows) != expected_total:
        raise _TransientMarkerSnapshot(f"Invoice Ninja {resource} marker result is incomplete")
    return rows


def _restored_content_matches(
    markers: _RestoreMarkers,
    clients: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
) -> bool:
    source_invoices: dict[
        str,
        list[tuple[str, str, str, tuple[tuple[str, str], ...]]],
    ] = {}
    for invoice_marker in markers.invoices:
        source_invoices.setdefault(invoice_marker.source_client_id, []).append(
            (
                invoice_marker.number,
                invoice_marker.public_notes,
                invoice_marker.private_notes,
                tuple((line.product_key, line.notes) for line in invoice_marker.lines),
            )
        )

    source_units = Counter(
        (
            client_marker.name,
            client_marker.id_number,
            client_marker.contact_emails,
            tuple(sorted(source_invoices.get(client_marker.source_id, []))),
        )
        for client_marker in markers.clients
    )

    destination_invoices: dict[
        str,
        list[tuple[str, str, str, tuple[tuple[str, str], ...]]],
    ] = {}
    for invoice in invoices:
        if not isinstance(invoice, dict):
            return False
        client_id = invoice.get("client_id")
        number = invoice.get("number")
        public_notes = invoice.get("public_notes")
        private_notes = invoice.get("private_notes")
        line_items = invoice.get("line_items")
        if (
            not isinstance(client_id, str)
            or not client_id
            or not isinstance(number, str)
            or not isinstance(public_notes, str)
            or not isinstance(private_notes, str)
            or not isinstance(line_items, list)
        ):
            return False
        observed_lines: list[tuple[str, str]] = []
        for line in line_items:
            product_key = line.get("product_key") if isinstance(line, dict) else None
            notes = line.get("notes") if isinstance(line, dict) else None
            if not isinstance(product_key, str) or not isinstance(notes, str):
                return False
            observed_lines.append((product_key, notes))
        destination_invoices.setdefault(client_id, []).append(
            (number, public_notes, private_notes, tuple(observed_lines))
        )

    destination_units: Counter[
        tuple[
            str,
            str,
            tuple[str, ...],
            tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...],
        ]
    ] = Counter()
    destination_ids: set[str] = set()
    for client in clients:
        if not isinstance(client, dict):
            return False
        destination_id = client.get("id")
        name = client.get("name")
        id_number = client.get("id_number")
        contacts = client.get("contacts")
        if (
            not isinstance(destination_id, str)
            or not destination_id
            or destination_id in destination_ids
            or not isinstance(name, str)
            or not isinstance(id_number, str)
            or not isinstance(contacts, list)
        ):
            return False
        destination_ids.add(destination_id)
        observed_emails: list[str] = []
        for contact in contacts:
            email = contact.get("email") if isinstance(contact, dict) else None
            if not isinstance(email, str):
                return False
            observed_emails.append(email)
        destination_units[
            (
                name,
                id_number,
                tuple(sorted(observed_emails)),
                tuple(sorted(destination_invoices.pop(destination_id, []))),
            )
        ] += 1
    if destination_invoices:
        return False
    return destination_units == source_units


class InvoiceNinjaPlugin(BackupPlugin):
    """Invoice Ninja backup plugin using export API.

    Research summary:
    - `GET /api/v1/ping` returns company and user info, used for connectivity tests.
    - `POST /api/v1/export` queues a `CompanyExport` job and responds with a
      signed temporary URL for `GET /api/v1/protected_download/<hash>`.
    - The job writes a zip containing JSON data, documents and backups; the
      URL becomes valid once the job completes so polling is required.
    Authentication uses the `X-API-Token` header.
    """

    restore_capability = "partial"
    _EXPECTED_APP_VERSION = "5.13.31"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)

    # ---- helpers -----------------------------------------------------------------
    def _base_dir(self) -> str:
        return "/backups"

    def _validate_export(self, path: Path) -> int:
        try:
            archive_size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError("Invoice Ninja export archive is unavailable") from exc
        if archive_size <= 0 or archive_size > _MAX_ARCHIVE_BYTES:
            raise RuntimeError("Invoice Ninja export exceeds its archive bounds")
        if not _zip_has_no_trailing_data(path):
            raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive boundary")

        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive")
                normalized_names: set[str] = set()
                member_by_name: dict[str, zipfile.ZipInfo] = {}
                total_compressed = 0
                total_expanded = 0
                for member in members:
                    name = member.filename
                    normalized = name.casefold()
                    if (
                        not _safe_archive_name(name)
                        or normalized in normalized_names
                        or member.is_dir()
                        or member.flag_bits & 0x1
                        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    ):
                        raise RuntimeError("Invoice Ninja export contains an unsafe ZIP member")
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in {0, stat.S_IFREG}:
                        raise RuntimeError("Invoice Ninja export contains an unsafe ZIP member")
                    if not (
                        name in {"backup.json", "company_logo.png"}
                        or name.startswith("documents/")
                        or name.startswith("backups/")
                    ):
                        raise RuntimeError("Invoice Ninja export contains an unexpected ZIP member")
                    if (
                        member.compress_size > _MAX_MEMBER_COMPRESSED_BYTES
                        or member.file_size > _MAX_MEMBER_EXPANDED_BYTES
                    ):
                        raise RuntimeError("Invoice Ninja export member exceeds its size bounds")
                    ratio = member.file_size / max(1, member.compress_size)
                    if ratio > _MAX_EXPANSION_RATIO:
                        raise RuntimeError("Invoice Ninja export exceeds its expansion ratio")
                    total_compressed += member.compress_size
                    total_expanded += member.file_size
                    normalized_names.add(normalized)
                    member_by_name[name] = member
                if (
                    total_compressed > _MAX_TOTAL_COMPRESSED_BYTES
                    or total_expanded > _MAX_TOTAL_EXPANDED_BYTES
                ):
                    raise RuntimeError("Invoice Ninja export exceeds its total size bounds")
                backup_member = member_by_name.get("backup.json")
                if backup_member is None:
                    raise RuntimeError("Invoice Ninja export archive is missing backup.json")
                if backup_member.file_size > _MAX_BACKUP_JSON_BYTES:
                    raise RuntimeError("Invoice Ninja backup.json exceeds its size bound")
                if archive.testzip() is not None:
                    raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive")
                backup_data = archive.read(backup_member)
        except RuntimeError:
            raise
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive") from exc
        try:
            parsed = json.loads(backup_data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Invoice Ninja export contains invalid backup.json") from exc
        if not isinstance(parsed, dict) or set(parsed) != _BACKUP_JSON_FIELDS:
            raise RuntimeError("Invoice Ninja export contains invalid backup.json")
        if parsed.get("app_version") != self._EXPECTED_APP_VERSION:
            raise RuntimeError("Invoice Ninja export has the wrong application version")
        if not isinstance(parsed.get("storage_url"), str) or not isinstance(
            parsed.get("company"), dict
        ):
            raise RuntimeError("Invoice Ninja export contains invalid backup.json")
        if any(not isinstance(parsed.get(field), list) for field in _ARRAY_FIELDS):
            raise RuntimeError("Invoice Ninja export contains invalid backup.json")

        expected_document_members: set[str] = set()
        documents = parsed["documents"]
        assert isinstance(documents, list)
        for document in documents:
            if not isinstance(document, dict):
                raise RuntimeError("Invoice Ninja export contains invalid document metadata")
            url = document.get("url")
            size = document.get("size")
            if (
                not isinstance(url, str)
                or not _safe_archive_name(url)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or size > _MAX_DOCUMENT_BYTES
            ):
                raise RuntimeError("Invoice Ninja export contains invalid document metadata")
            member_name = f"documents/{url}"
            document_member = member_by_name.get(member_name)
            if (
                document_member is None
                or document_member.file_size != size
                or member_name in expected_document_members
            ):
                raise RuntimeError("Invoice Ninja export has incomplete document data")
            expected_document_members.add(member_name)
        actual_document_members = {name for name in member_by_name if name.startswith("documents/")}
        if actual_document_members != expected_document_members:
            raise RuntimeError("Invoice Ninja export has incomplete document data")
        return len(members)

    # ---- interface implementation -------------------------------------------------
    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        """Return whether config is the exact supported Invoice Ninja shape."""
        if not isinstance(config, dict):
            return False
        if set(config) - {"base_url", "token", "export_timeout_seconds"}:
            return False
        base_url = config.get("base_url")
        token = config.get("token")
        if not isinstance(base_url, str) or not base_url:
            return False
        try:
            parsed = urlsplit(base_url)
            parsed_port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed_port is not None
            and not 1 <= parsed_port <= 65535
        ):
            return False
        if not isinstance(token, str) or not token:
            return False
        timeout = config.get("export_timeout_seconds", 3300)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            return False
        if not 60 <= timeout <= 3300:
            return False
        return True

    async def _probe(self, config: Dict[str, Any]) -> tuple[str, str]:
        base_url = config["base_url"]
        url = f"{base_url}/api/v1/ping"
        headers = {
            "X-API-Token": config["token"],
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            _LOGGER.warning("invoiceninja_probe_connection_failed | url=%s", url)
            raise ConnectionError("Failed to connect to Invoice Ninja server") from None

        if response.status_code // 100 != 2:
            _LOGGER.warning(
                "invoiceninja_probe_http_error | url=%s status=%s",
                url,
                response.status_code,
            )
            raise RuntimeError(f"Invoice Ninja API returned status {response.status_code}")
        if response.headers.get("X-APP-VERSION") != self._EXPECTED_APP_VERSION:
            raise RuntimeError(f"Invoice Ninja must report version {self._EXPECTED_APP_VERSION}")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("Invoice Ninja ping returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Invoice Ninja ping returned an invalid response")
        for field in ("company_name", "user_name"):
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError("Invoice Ninja ping returned an invalid response")
        return payload["company_name"], payload["user_name"]

    async def test(self, config: Dict[str, Any]) -> bool:
        """Ping the Invoice Ninja API to verify credentials."""
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: base_url and token are required")
        await self._probe(config)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Create and validate one bounded native Invoice Ninja export."""
        started = _monotonic()
        _LOGGER.info(
            "invoiceninja_backup_start | job_id=%s target_id=%s",
            context.job_id,
            context.target_id,
        )
        try:
            result = await self._backup_operation(context)
            artifact_path = str(result["artifact_path"])
            artifact_bytes = os.path.getsize(artifact_path)
        except BaseException:
            _LOGGER.warning(
                "invoiceninja_backup_failure | job_id=%s target_id=%s duration_seconds=%.3f",
                context.job_id,
                context.target_id,
                max(0.0, _monotonic() - started),
            )
            raise
        _LOGGER.info(
            "invoiceninja_backup_success | job_id=%s target_id=%s artifact=%s "
            "bytes=%s duration_seconds=%.3f",
            context.job_id,
            context.target_id,
            artifact_path,
            artifact_bytes,
            max(0.0, _monotonic() - started),
        )
        return result

    async def _backup_operation(self, context: BackupContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invoice Ninja config must include base_url and token")
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        token = cfg.get("token")
        headers = {"X-API-Token": str(token)}
        export_url = f"{base_url}/api/v1/export"
        poll_interval = 5.0
        timeout_seconds = min(float(cfg.get("export_timeout_seconds", 55 * 60)), 55 * 60)
        attempts = max(1, math.ceil(timeout_seconds / poll_interval))
        deadline = _monotonic() + timeout_seconds

        lock = _origin_lock(_canonical_origin(base_url))
        async with _hold_lock(lock, deadline=deadline):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("Invoice Ninja backup deadline expired")
            try:
                async with asyncio.timeout(remaining):
                    await self._probe(cfg)
            except TimeoutError:
                raise RuntimeError("Invoice Ninja backup deadline expired") from None
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                # 1) trigger export
                _LOGGER.info(
                    "invoiceninja_backup_request | job_id=%s target_id=%s url=%s",
                    context.job_id,
                    context.target_id,
                    export_url,
                )
                post_headers = {
                    **headers,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                }
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    raise RuntimeError("Invoice Ninja backup deadline expired")
                try:
                    async with asyncio.timeout(remaining):
                        response = await client.post(export_url, headers=post_headers)
                except TimeoutError:
                    raise RuntimeError("Invoice Ninja backup deadline expired") from None
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Invoice Ninja export returned status {response.status_code}"
                    )
                try:
                    data = response.json()
                except ValueError:
                    raise RuntimeError("Invoice Ninja export returned invalid JSON") from None
                if not isinstance(data, dict) or set(data) != {"message", "url"}:
                    raise RuntimeError("Invoice Ninja export returned an invalid response")
                if data.get("message") != "Processing":
                    raise RuntimeError("Invoice Ninja export was not accepted for processing")
                download_url = _require_signed_download_url(base_url, data.get("url"))

                # 2) poll for archive readiness
                get_headers = {"Accept": "application/zip, application/octet-stream"}
                with create_backup_artifact(
                    self,
                    context,
                    prefix="invoiceninja-export",
                    suffix=".zip",
                    backup_root=self._base_dir(),
                ) as artifact:
                    try:
                        for attempt in range(attempts):
                            _LOGGER.info("invoiceninja_poll_download | attempt=%s", attempt + 1)
                            validation_evidence = await _download_and_validate_export_attempt(
                                client,
                                download_url,
                                get_headers,
                                artifact,
                                deadline=deadline,
                                poll_interval=poll_interval,
                            )
                            if validation_evidence is None:
                                continue
                            if _monotonic() >= deadline:
                                raise RuntimeError("Invoice Ninja export download timed out")
                            if artifact.publication_fd is None:
                                raise RuntimeError(
                                    "Invoice Ninja export has no bound publication descriptor"
                                )
                            member_count, artifact.publication_sha256 = (
                                _require_bound_validation_evidence(
                                    validation_evidence,
                                    artifact.publication_fd,
                                )
                            )
                            artifact.sidecar_metadata.update(
                                {
                                    "application_version": self._EXPECTED_APP_VERSION,
                                    "archive_member_count": member_count,
                                    "validation": "passed",
                                }
                            )
                            break
                        else:
                            raise RuntimeError("export download not ready")
                    except httpx.HTTPError:
                        raise ConnectionError(
                            "Failed to download the Invoice Ninja export"
                        ) from None
                artifact_path = str(artifact.final_path)

        return {"artifact_path": artifact_path}

    async def _require_fresh_restore_destination(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
    ) -> None:
        for path in _FRESH_RESTORE_RESOURCE_PATHS:
            response = await client.get(f"{base_url}{path}", headers=headers)
            if response.status_code != 200:
                raise RuntimeError(
                    "Invoice Ninja fresh-destination check returned status "
                    f"{response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError("Invoice Ninja fresh-destination response is invalid") from None
            if not isinstance(payload, dict) or set(payload) != {"data", "meta"}:
                raise RuntimeError("Invoice Ninja fresh-destination response is invalid")
            data = payload.get("data")
            meta = payload.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            total = pagination.get("total") if isinstance(pagination, dict) else None
            if (
                not isinstance(data, list)
                or not isinstance(pagination, dict)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
            ):
                raise RuntimeError("Invoice Ninja fresh-destination response is invalid")
            if data or total != 0:
                raise RuntimeError("Invoice Ninja restore destination is not fresh and empty")

    async def _wait_for_restore_markers(
        self,
        client: httpx.AsyncClient,
        config: dict[str, Any],
        headers: dict[str, str],
        markers: _RestoreMarkers,
        *,
        deadline: float,
    ) -> None:
        base_url = config["base_url"]
        while True:
            snapshot_stable = True
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("Invoice Ninja restored marker deadline expired")
            try:
                async with asyncio.timeout(remaining):
                    company_name, _user_name = await self._probe(config)
                    clients = await _all_resource_rows(
                        client,
                        base_url=base_url,
                        headers=headers,
                        resource="clients",
                        include="contacts",
                    )
                    invoices = await _all_resource_rows(
                        client,
                        base_url=base_url,
                        headers=headers,
                        resource="invoices",
                    )
            except _TransientMarkerSnapshot:
                snapshot_stable = False
                company_name = ""
                clients = []
                invoices = []
            except TimeoutError:
                raise RuntimeError("Invoice Ninja restored marker deadline expired") from None
            if (
                snapshot_stable
                and company_name == markers.company_name
                and _restored_content_matches(
                    markers,
                    clients,
                    invoices,
                )
            ):
                return
            if _monotonic() >= deadline:
                raise RuntimeError("Invoice Ninja restored marker deadline expired")
            remaining = deadline - _monotonic()
            try:
                async with asyncio.timeout(remaining):
                    await asyncio.sleep(_RESTORE_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                raise RuntimeError("Invoice Ninja restored marker deadline expired") from None

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a verified export into one authorized fresh local destination."""
        started = _monotonic()
        _LOGGER.info(
            "invoiceninja_restore_start | job_id=%s source_target_id=%s "
            "destination_target_id=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
        )
        try:
            result = await self._restore_operation(context)
        except BaseException:
            _LOGGER.warning(
                "invoiceninja_restore_failure | job_id=%s source_target_id=%s "
                "destination_target_id=%s duration_seconds=%.3f",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                max(0.0, _monotonic() - started),
            )
            raise
        _LOGGER.info(
            "invoiceninja_restore_success | job_id=%s source_target_id=%s "
            "destination_target_id=%s status=%s duration_seconds=%.3f",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            result.get("status"),
            max(0.0, _monotonic() - started),
        )
        return result

    async def _restore_operation(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invoice Ninja config must include base_url and token")
        base_url = str(cfg["base_url"])
        _require_isolated_restore_authorization(base_url, context.metadata or {})
        if context.source_target_id == context.destination_target_id:
            raise ValueError(
                "Invoice Ninja restore source and destination must be different targets"
            )
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.isfile(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        restore_deadline = _monotonic() + _RESTORE_MARKER_TIMEOUT_SECONDS
        artifact = Path(artifact_path)
        artifact_descriptor, artifact_bytes, artifact_sha256 = _open_verified_restore_artifact(
            artifact,
            context.metadata or {},
        )
        headers = {
            "X-API-Token": str(cfg["token"]),
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        lock = _origin_lock(_canonical_origin(base_url))
        try:
            validation_process, validation_connection = _start_validation_process(
                Path(f"/proc/{os.getpid()}/fd/{artifact_descriptor}")
            )
            remaining = restore_deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("Invoice Ninja restore deadline expired")
            validation_evidence = await _await_validation_process(
                validation_process,
                validation_connection,
                timeout_seconds=min(_VALIDATION_TIMEOUT_SECONDS, remaining),
            )
            _require_bound_validation_evidence(
                validation_evidence,
                artifact_descriptor,
                expected_sha256=artifact_sha256,
            )
            markers = validation_evidence.get("markers")
            if not isinstance(markers, _RestoreMarkers):
                raise RuntimeError("Invoice Ninja validation worker returned invalid markers")
            if _monotonic() >= restore_deadline:
                raise RuntimeError("Invoice Ninja restore deadline expired")
            async with _hold_lock(lock, deadline=restore_deadline):
                remaining = restore_deadline - _monotonic()
                if remaining <= 0:
                    raise RuntimeError("Invoice Ninja restore deadline expired")
                try:
                    async with asyncio.timeout(remaining):
                        await self._probe(cfg)
                        try:
                            async with httpx.AsyncClient(
                                timeout=60.0,
                                follow_redirects=False,
                            ) as client:
                                await self._require_fresh_restore_destination(
                                    client,
                                    base_url,
                                    headers,
                                )
                                with os.fdopen(os.dup(artifact_descriptor), "rb") as artifact_file:
                                    response = await client.post(
                                        f"{base_url}/api/v1/import_json",
                                        headers=headers,
                                        files={
                                            "files": (
                                                artifact.name,
                                                artifact_file,
                                                "application/zip",
                                            )
                                        },
                                        data={
                                            "import_settings": "true",
                                            "import_data": "true",
                                        },
                                    )
                                if response.status_code != 200:
                                    raise RuntimeError(
                                        "Invoice Ninja import returned status "
                                        f"{response.status_code}"
                                    )
                                try:
                                    acceptance = response.json()
                                except ValueError:
                                    raise RuntimeError(
                                        "Invoice Ninja import returned an invalid response"
                                    ) from None
                                if acceptance != {"message": "Processing", "success": True}:
                                    raise RuntimeError(
                                        "Invoice Ninja import returned an invalid response"
                                    )
                                await self._wait_for_restore_markers(
                                    client,
                                    cfg,
                                    headers,
                                    markers,
                                    deadline=restore_deadline,
                                )
                        except httpx.HTTPError:
                            raise ConnectionError("Failed to restore Invoice Ninja") from None
                except TimeoutError:
                    raise RuntimeError("Invoice Ninja restore deadline expired") from None
        finally:
            os.close(artifact_descriptor)

        return {
            "status": "partial",
            "artifact_bytes": artifact_bytes,
            "artifact_sha256": artifact_sha256,
            "message": (
                "Invoice Ninja queued asynchronous import restored the expected company, "
                "client, and invoice markers; terminal whole-export status and document "
                "recovery are not exposed by the vendor API"
            ),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return an observed Invoice Ninja connectivity status."""
        try:
            await self.test(context.config or {})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok"}
