from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.postgresql import (
    PostgreSQLIdentity,
    PostgreSQLTarget,
    authorize_postgresql_restore,
    postgresql_archive_schema_sha256,
    postgresql_catalog_sha256,
    probe_postgresql,
    publish_postgresql_artifact,
    query_postgresql_json,
    restore_postgresql_archive,
    validate_postgresql_config,
    write_postgresql_archive,
)

CALCOM_ALLOWED_DATABASE_OBJECTS = frozenset(
    {
        "trigger:public.assignment_reason_delete_trigger_for_routing_form",
        "trigger:public.assignment_reason_insert_trigger_for_routing_form",
        "trigger:public.assignment_reason_update_trigger_for_routing_form",
        "trigger:public.booking_delete_trigger_for_routing_form",
        "trigger:public.booking_denorm_booking_delete_trigger",
        "trigger:public.booking_denorm_booking_insert_update_trigger",
        "trigger:public.booking_denorm_event_type_length_update_trigger",
        "trigger:public.booking_denorm_event_type_parent_id_update_trigger",
        "trigger:public.booking_denorm_event_type_team_id_update_trigger",
        "trigger:public.booking_denorm_user_update_trigger",
        "trigger:public.booking_insert_trigger_for_routing_form",
        "trigger:public.booking_update_trigger_for_routing_form",
        "trigger:public.event_type_update_trigger_for_routing_form",
        "trigger:public.membership_role_change_trigger",
        "trigger:public.routing_form_delete_trigger",
        "trigger:public.routing_form_name_update_trigger",
        "trigger:public.routing_form_response_delete_trigger",
        "trigger:public.routing_form_response_denormalized_insert_trigger",
        "trigger:public.routing_form_response_insert_update_trigger",
        "trigger:public.routing_form_response_update_trigger",
        "trigger:public.routing_form_team_update_trigger",
        "trigger:public.routing_form_user_update_trigger",
        "trigger:public.tracking_delete_trigger_for_routing_form",
        "trigger:public.tracking_insert_trigger_for_routing_form",
        "trigger:public.tracking_update_trigger_for_routing_form",
        "trigger:public.trigger_nullify_routing_form_response_denormalized_event_type",
        "trigger:public.user_delete_trigger_for_routing_form",
        "trigger:public.user_update_trigger_for_routing_form",
    }
)
CALCOM_MIGRATION_COUNT = 588
CALCOM_MIGRATION_HEAD = "20260219000000_add_fallback_action_to_queued_form_response"
CALCOM_MIGRATION_SHA256 = "4bab1776d3e03cdd18d6c36a8a57d5fb1243759f43717f0a3d7fa7f1561016f8"
CALCOM_CATALOG_SHA256 = "6f04bc45e021dac638c80dacca4384ebc43c7d5c0073e4a46595438733d1dc33"
CALCOM_SCHEMA_SHA256 = "f1112b98123f36ae502f39173e523545f7a41959c35351ef200a3f2b7fd66e52"
CALCOM_RESTORE_ALLOWLIST_ENV = "HOMELAB_BACKUP_ISOLATED_CALCOM_RESTORE_DESTINATIONS"
CALCOM_RESTORE_SENTINEL = "homelab-backup:calcom-restore:v1"
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0
_LOG = logging.getLogger(__name__)
_CALCOM_PROFILE_SQL = r"""
SELECT json_build_object(
  'migration_count', count(*)::integer,
  'migration_head', max(migration_name),
  'migration_sha256', encode(
    sha256(
      convert_to(
        COALESCE(string_agg(migration_name, E'\n' ORDER BY migration_name), '') || E'\n',
        'UTF8'
      )
    ),
    'hex'
  ),
  'unfinished_count', count(*) FILTER (
    WHERE finished_at IS NULL AND rolled_back_at IS NULL
  )::integer,
  'rolled_back_count', count(*) FILTER (WHERE rolled_back_at IS NOT NULL)::integer,
  'incomplete_step_count', count(*) FILTER (WHERE applied_steps_count <= 0)::integer
)
FROM public._prisma_migrations;
""".strip()
_CALCOM_MARKER_CATEGORIES = frozenset(
    {
        "api_keys",
        "attendees",
        "bookings",
        "credentials",
        "destination_calendars",
        "event_types",
        "schedules",
        "selected_calendars",
        "users",
        "webhooks",
        "workflow_steps",
        "workflows",
    }
)
_CALCOM_MARKER_SQL = r"""
WITH marker_rows(category, marker) AS (
  SELECT 'users', jsonb_build_array(id, uuid, username, name, email)::text
    FROM public.users
  UNION ALL
  SELECT 'schedules', jsonb_build_array(id, "userId", name, "timeZone")::text
    FROM public."Schedule"
  UNION ALL
  SELECT 'event_types',
         jsonb_build_array(id, "userId", title, slug, length, "scheduleId")::text
    FROM public."EventType"
  UNION ALL
  SELECT 'attendees', jsonb_build_array(id, "bookingId", email, name, "timeZone")::text
    FROM public."Attendee"
  UNION ALL
  SELECT 'bookings',
         jsonb_build_array(
           id, uid, "userId", "eventTypeId", title, "startTime", "endTime", status
         )::text
    FROM public."Booking"
  UNION ALL
  SELECT 'credentials',
         jsonb_build_array(id, type, key, "encryptedKey", "userId", "teamId", "appId")::text
    FROM public."Credential"
  UNION ALL
  SELECT 'selected_calendars',
         jsonb_build_array(id, "userId", integration, "externalId", "credentialId")::text
    FROM public."SelectedCalendar"
  UNION ALL
  SELECT 'destination_calendars',
         jsonb_build_array(
           id, integration, "externalId", "userId", "eventTypeId", "credentialId"
         )::text
    FROM public."DestinationCalendar"
  UNION ALL
  SELECT 'workflows',
         jsonb_build_array(id, name, "userId", "teamId", trigger, time, "timeUnit")::text
    FROM public."Workflow"
  UNION ALL
  SELECT 'workflow_steps',
         jsonb_build_array(
           id, "workflowId", "stepNumber", action, "sendTo", "reminderBody", "emailSubject"
         )::text
    FROM public."WorkflowStep"
  UNION ALL
  SELECT 'webhooks',
         jsonb_build_array(
           id, "userId", "teamId", "eventTypeId", "subscriberUrl", active,
           "eventTriggers", secret
         )::text
    FROM public."Webhook"
  UNION ALL
  SELECT 'api_keys',
         jsonb_build_array(id, "userId", "teamId", note, "hashedKey", "appId")::text
    FROM public."ApiKey"
), categories(category) AS (
  VALUES
    ('api_keys'), ('attendees'), ('bookings'), ('credentials'),
    ('destination_calendars'), ('event_types'), ('schedules'),
    ('selected_calendars'), ('users'), ('webhooks'), ('workflow_steps'), ('workflows')
)
SELECT json_object_agg(
  categories.category,
  json_build_object(
    'count', COALESCE(profile.marker_count, 0),
    'sha256', encode(
      sha256(convert_to(COALESCE(profile.markers, '') || E'\n', 'UTF8')),
      'hex'
    )
  )
)
FROM categories
LEFT JOIN LATERAL (
  SELECT count(*)::integer AS marker_count,
         string_agg(marker, E'\n' ORDER BY marker) AS markers
  FROM marker_rows
  WHERE marker_rows.category = categories.category
) AS profile ON true;
""".strip()


def _require_calcom_profile(value: Mapping[str, object]) -> None:
    expected = {
        "migration_count": CALCOM_MIGRATION_COUNT,
        "migration_head": CALCOM_MIGRATION_HEAD,
        "migration_sha256": CALCOM_MIGRATION_SHA256,
        "unfinished_count": 0,
        "rolled_back_count": 0,
        "incomplete_step_count": 0,
    }
    if value != expected:
        raise RuntimeError("Cal.com migration inventory did not match exact v6.2.0")


def _require_calcom_catalog(identity: PostgreSQLIdentity) -> None:
    if postgresql_catalog_sha256(identity) != CALCOM_CATALOG_SHA256:
        raise RuntimeError("Cal.com catalog inventory did not match exact v6.2.0")


def _require_calcom_marker_profile(value: Mapping[str, object]) -> None:
    if set(value) != _CALCOM_MARKER_CATEGORIES:
        raise RuntimeError("Cal.com marker profile was incomplete")
    for evidence in value.values():
        if not isinstance(evidence, dict) or set(evidence) != {"count", "sha256"}:
            raise RuntimeError("Cal.com marker profile was invalid")
        count = evidence.get("count")
        digest = evidence.get("sha256")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("Cal.com marker profile was invalid")


def _profile_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_calcom_restore_sidecar(metadata: Mapping[str, object]) -> Mapping[str, object]:
    sidecar = metadata.get("artifact_sidecar")
    if not isinstance(sidecar, dict):
        raise ValueError("Cal.com restore requires complete archive provenance")
    if (
        sidecar.get("validation") != "calcom-postgresql-v1"
        or sidecar.get("application_version") != "6.2.0"
        or sidecar.get("migration_head") != CALCOM_MIGRATION_HEAD
        or sidecar.get("migration_sha256") != CALCOM_MIGRATION_SHA256
        or sidecar.get("schema_sha256") != CALCOM_SCHEMA_SHA256
    ):
        raise ValueError("Cal.com restore archive provenance is invalid")
    marker_digest = sidecar.get("marker_profile_sha256")
    marker_counts = sidecar.get("marker_counts")
    if (
        not isinstance(marker_digest, str)
        or len(marker_digest) != 64
        or any(character not in "0123456789abcdef" for character in marker_digest)
        or not isinstance(marker_counts, dict)
        or set(marker_counts) != _CALCOM_MARKER_CATEGORIES
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in marker_counts.values()
        )
    ):
        raise ValueError("Cal.com restore marker provenance is invalid")
    return sidecar


async def _read_calcom_profiles(
    target: PostgreSQLTarget,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    migration_profile = await query_postgresql_json(
        target,
        _CALCOM_PROFILE_SQL,
        operation="Cal.com migration profile probe",
    )
    _require_calcom_profile(migration_profile)
    marker_profile = await query_postgresql_json(
        target,
        _CALCOM_MARKER_SQL,
        operation="Cal.com marker profile probe",
    )
    _require_calcom_marker_profile(marker_profile)
    return migration_profile, marker_profile


class _CalcomProfileDrift(RuntimeError):
    """Signal one retryable source-profile change across a dump boundary."""


class CalcomPlugin(BackupPlugin):
    """Back up a Cal.com PostgreSQL database using PostgreSQL 16 custom format."""

    restore_capability = "partial"

    def __init__(self, name: str, version: str = "0.2.1", base_dir: str = "/backups") -> None:
        super().__init__(name=name, version=version)
        self.base_dir = base_dir

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate the exact flat Cal.com source or destination shape."""
        return validate_postgresql_config(config)

    async def test(self, config: Dict[str, Any]) -> bool:
        """Probe an exact source or an empty authorized restore destination."""
        if not await self.validate_config(config):
            raise ValueError("Invalid Cal.com source or restore-destination configuration")
        target = PostgreSQLTarget.from_config(config)
        if target.mode == "restore_destination":
            await probe_postgresql(
                target,
                expected_state="fresh_destination",
                restore_sentinel=CALCOM_RESTORE_SENTINEL,
            )
            return True
        identity = await probe_postgresql(
            target,
            allowed_unsupported_database_objects=CALCOM_ALLOWED_DATABASE_OBJECTS,
        )
        _require_calcom_catalog(identity)
        await _read_calcom_profiles(target)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Publish one stable, exact Cal.com PostgreSQL archive."""
        started = time.monotonic()
        _LOG.info(
            "calcom_backup_start | job_id=%s target_id=%s",
            context.job_id,
            context.target_id,
        )
        config = context.config or {}
        if not await self.validate_config(config) or config.get("mode") != "source":
            raise ValueError("Cal.com backup requires an exact source configuration")
        target = PostgreSQLTarget.from_config(config)
        try:
            async with asyncio.timeout(BACKUP_TIMEOUT_SECONDS):
                artifact_path: str | None = None
                for attempt in range(2):
                    try:
                        artifact_path = await self._capture_backup_attempt(
                            target,
                            context,
                        )
                    except _CalcomProfileDrift as exc:
                        if attempt == 0:
                            continue
                        raise RuntimeError(
                            "Cal.com source profile did not stabilize during capture"
                        ) from exc
                    break
                if artifact_path is None:
                    raise RuntimeError("Cal.com backup returned no artifact")
        except TimeoutError as exc:
            _LOG.exception(
                "calcom_backup_failed | job_id=%s target_id=%s duration_ms=%d",
                context.job_id,
                context.target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(
                f"Cal.com backup timed out after {BACKUP_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        except BaseException:
            _LOG.exception(
                "calcom_backup_failed | job_id=%s target_id=%s duration_ms=%d",
                context.job_id,
                context.target_id,
                int((time.monotonic() - started) * 1000),
            )
            raise
        _LOG.info(
            "calcom_backup_success | job_id=%s target_id=%s artifact_path=%s duration_ms=%d",
            context.job_id,
            context.target_id,
            artifact_path,
            int((time.monotonic() - started) * 1000),
        )
        return {"artifact_path": artifact_path}

    async def _capture_backup_attempt(
        self,
        target: PostgreSQLTarget,
        context: BackupContext,
    ) -> str:
        identity = await probe_postgresql(
            target,
            allowed_unsupported_database_objects=CALCOM_ALLOWED_DATABASE_OBJECTS,
        )
        _require_calcom_catalog(identity)
        migration_profile, marker_profile = await _read_calcom_profiles(target)
        with create_backup_artifact(
            self,
            context,
            prefix="calcom-postgresql",
            suffix=".dump",
            backup_root=self.base_dir,
        ) as artifact:
            evidence = await write_postgresql_archive(
                target,
                identity,
                artifact,
                allowed_unsupported_database_objects=CALCOM_ALLOWED_DATABASE_OBJECTS,
            )
            if artifact.publication_fd is None:
                raise RuntimeError("Cal.com archive was not bound for validation")
            schema_sha256 = await postgresql_archive_schema_sha256(artifact.publication_fd)
            if schema_sha256 != CALCOM_SCHEMA_SHA256:
                raise RuntimeError("Cal.com archive schema did not match exact v6.2.0")
            stable_migration_profile, stable_marker_profile = await _read_calcom_profiles(target)
            if (
                stable_migration_profile != migration_profile
                or stable_marker_profile != marker_profile
            ):
                raise _CalcomProfileDrift("Cal.com source profile changed during capture")
            artifact.sidecar_metadata.update(
                {
                    "application_version": "6.2.0",
                    "migration_head": CALCOM_MIGRATION_HEAD,
                    "migration_sha256": CALCOM_MIGRATION_SHA256,
                    "schema_sha256": schema_sha256,
                    "marker_profile_sha256": _profile_sha256(marker_profile),
                    "marker_counts": {
                        category: marker_profile[category]["count"]  # type: ignore[index]
                        for category in sorted(_CALCOM_MARKER_CATEGORIES)
                    },
                    "postgresql_server_version": identity.server_version,
                    "postgresql_server_version_num": identity.server_version_num,
                    "server_encoding": identity.server_encoding,
                    "lc_collate": identity.lc_collate,
                    "lc_ctype": identity.lc_ctype,
                    "rls_table_count": len(identity.catalog["rls_tables"]),  # type: ignore[arg-type]
                    "source_identity_sha256": evidence.source_identity_sha256,
                    "source_catalog_sha256": evidence.source_catalog_sha256,
                    "archive_catalog_sha256": evidence.archive_catalog_sha256,
                    "toc_sha256": evidence.toc_sha256,
                    "catalog_counts": dict(evidence.catalog_counts),
                    "validation": "calcom-postgresql-v1",
                }
            )
            await publish_postgresql_artifact(artifact, self, context)
        return str(artifact.final_path)

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore one verified archive into an isolated fresh Cal.com database."""
        started = time.monotonic()
        _LOG.info(
            "calcom_restore_start | job_id=%s source=%s destination=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
        )
        try:
            async with asyncio.timeout(RESTORE_TIMEOUT_SECONDS):
                result = await self._restore_operation(context)
        except TimeoutError as exc:
            _LOG.exception(
                "calcom_restore_failed | job_id=%s duration_ms=%d",
                context.job_id,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(
                f"Cal.com restore timed out after {RESTORE_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        except BaseException:
            _LOG.exception(
                "calcom_restore_failed | job_id=%s duration_ms=%d",
                context.job_id,
                int((time.monotonic() - started) * 1000),
            )
            raise
        _LOG.info(
            "calcom_restore_success | job_id=%s status=%s duration_ms=%d",
            context.job_id,
            result["status"],
            int((time.monotonic() - started) * 1000),
        )
        return result

    async def _restore_operation(self, context: RestoreContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config) or config.get("mode") != "restore_destination":
            raise ValueError("Cal.com restore requires an exact restore-destination configuration")
        metadata = context.metadata or {}
        sidecar = _require_calcom_restore_sidecar(metadata)
        target = PostgreSQLTarget.from_config(config)
        authorize_postgresql_restore(
            target,
            source_identity=metadata.get("source_database_identity"),
            source_target_id=context.source_target_id,
            destination_target_id=context.destination_target_id,
            restore_allowlist_env=CALCOM_RESTORE_ALLOWLIST_ENV,
        )
        pre_restore_identity = await probe_postgresql(
            target,
            expected_state="fresh_destination",
            restore_sentinel=CALCOM_RESTORE_SENTINEL,
        )
        restored = await restore_postgresql_archive(
            target,
            pre_restore_identity,
            Path(context.artifact_path),
            metadata,
            validation="calcom-postgresql-v1",
            restore_sentinel=CALCOM_RESTORE_SENTINEL,
            allowed_unsupported_database_objects=CALCOM_ALLOWED_DATABASE_OBJECTS,
            expected_schema_sha256=CALCOM_SCHEMA_SHA256,
        )
        _require_calcom_catalog(restored)
        _, marker_profile = await _read_calcom_profiles(target)
        marker_counts = sidecar["marker_counts"]
        if (
            _profile_sha256(marker_profile) != sidecar["marker_profile_sha256"]
            or {
                category: marker_profile[category]["count"]  # type: ignore[index]
                for category in sorted(_CALCOM_MARKER_CATEGORIES)
            }
            != marker_counts
        ):
            raise RuntimeError("Cal.com restored markers did not match the archive")
        artifact_bytes = metadata.get("artifact_bytes")
        artifact_sha256 = metadata.get("artifact_sha256")
        if (
            isinstance(artifact_bytes, bool)
            or not isinstance(artifact_bytes, int)
            or artifact_bytes <= 0
            or not isinstance(artifact_sha256, str)
        ):
            raise ValueError("Cal.com restore requires verified staged artifact metadata")
        return {
            "status": "partial",
            "message": (
                "Cal.com database restored; exact-image boot and external deployment "
                "configuration remain required"
            ),
            "restored_path": context.artifact_path,
            "artifact_bytes": artifact_bytes,
            "sha256": artifact_sha256,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return status observed through the exact Cal.com PostgreSQL probe."""
        config = context.config or {}
        try:
            await self.test(config)
        except (ConnectionError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return {"status": "error", "error": str(exc)}
        except Exception:
            return {"status": "error", "error": "Cal.com status check failed"}
        if config.get("mode") == "restore_destination":
            return {"status": "ok", "database_state": "fresh_destination"}
        return {"status": "ok", "application_version": "6.2.0"}
