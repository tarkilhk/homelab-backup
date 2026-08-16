from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_CALCOM_DOCKER_DRILL") != "1",
    reason="set RUN_CALCOM_DOCKER_DRILL=1 for the isolated Cal.com 6.2.0 drill",
)

_CALCOM_IMAGE = (
    "calcom/cal.com@" "sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001"
)
_POSTGRES_IMAGE = (
    "postgres@" "sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00"
)
_CALCOM_SOURCE_REVISION = "1c193cca8682b33b9866c792186033f7ef886682"
_LABEL = "asia.hollinger.homelab-backup.calcom-drill"
_DATABASE = "calendso"
_BACKUP_USER = "calcom_backup"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SYNTHETIC_SECRETS: set[str] = set()


def _redact(value: str) -> str:
    result = value
    for secret in sorted(_SYNTHETIC_SECRETS, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result[-6000:]


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stdout = getattr(exc, "stdout", "") or ""
        stderr = getattr(exc, "stderr", "") or ""
        raise RuntimeError(
            "Disposable Cal.com Docker command failed:\n"
            f"{_redact(str(stderr))}\n{_redact(str(stdout))}"
        ) from None


def _psql(container: str, database: str, sql: str, *, user: str = "postgres") -> str:
    completed = _docker(
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-U",
        user,
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=on",
        "-tA",
        "-f",
        "-",
        input_text=sql,
    )
    return completed.stdout.strip()


def _wait_for_postgresql(container: str, *, user: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        ).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stdout
            raise RuntimeError(f"Disposable PostgreSQL exited: {_redact(logs)}")
        logs = _docker("logs", container, check=False).stdout
        ready = _docker("exec", container, "pg_isready", "-U", user, check=False)
        if "PostgreSQL init process complete" in logs and ready.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("Disposable Cal.com PostgreSQL did not become ready")


def _wait_for_app(app: str, network: str, runner_image: str) -> None:
    deadline = time.monotonic() + 240
    probe = (
        "import urllib.request; "
        f"r=urllib.request.urlopen('http://{app}:3000/auth/login',timeout=5); "
        "assert r.status == 200"
    )
    while time.monotonic() < deadline:
        state = _docker("inspect", "--format", "{{.State.Status}}", app, check=False).stdout.strip()
        if state == "exited":
            logs = _docker("logs", app, check=False).stdout
            raise RuntimeError(f"Disposable Cal.com exited: {_redact(logs)}")
        completed = _docker(
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            runner_image,
            "-c",
            probe,
            check=False,
            timeout=15,
        )
        if completed.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("Disposable Cal.com did not become ready")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _assert_image_identity() -> None:
    calcom = json.loads(_docker("image", "inspect", _CALCOM_IMAGE).stdout)[0]
    postgres = json.loads(_docker("image", "inspect", _POSTGRES_IMAGE).stdout)[0]
    assert _CALCOM_IMAGE in calcom["RepoDigests"]
    assert _POSTGRES_IMAGE in postgres["RepoDigests"]
    labels = calcom["Config"]["Labels"]
    assert labels["org.opencontainers.image.version"] == "v6.2.0"
    assert labels["org.opencontainers.image.revision"] == _CALCOM_SOURCE_REVISION


@contextmanager
def _managed_resources(
    *,
    suffix: str,
    network: str,
    volumes: list[str],
    backup_root: Path,
) -> Iterator[str]:
    runner_image = f"hlb-cal18-runner:{suffix}"
    try:
        _docker("pull", _CALCOM_IMAGE)
        _docker("pull", _POSTGRES_IMAGE)
        _assert_image_identity()
        _docker(
            "build",
            "--tag",
            runner_image,
            "--file",
            str(_BACKEND_ROOT / "Dockerfile"),
            str(_BACKEND_ROOT),
            timeout=900,
        )
        _docker("network", "create", "--internal", "--label", f"{_LABEL}=1", network)
        for volume in volumes:
            _docker("volume", "create", "--label", f"{_LABEL}=1", volume)
        yield runner_image
    finally:
        owned = _docker(
            "ps",
            "--all",
            "--filter",
            f"label={_LABEL}",
            "--format",
            "{{.Names}}",
            check=False,
        ).stdout.splitlines()
        if owned:
            _docker("rm", "-f", *owned, check=False)
        for volume in volumes:
            _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", network, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
        shutil.rmtree(backup_root, ignore_errors=True)
        _SYNTHETIC_SECRETS.clear()
        assert not _docker(
            "ps", "--all", "--filter", f"label={_LABEL}", "--quiet", check=False
        ).stdout.strip()
        assert not _docker(
            "network", "ls", "--filter", f"label={_LABEL}", "--quiet", check=False
        ).stdout.strip()
        assert not _docker(
            "volume", "ls", "--filter", f"label={_LABEL}", "--quiet", check=False
        ).stdout.strip()
        assert _docker("image", "inspect", runner_image, check=False).returncode != 0
        assert not backup_root.exists()
        assert not _SYNTHETIC_SECRETS
        assert not any(
            name.startswith("hlb-cal18-")
            for name in _docker(
                "ps", "--all", "--format", "{{.Names}}", check=False
            ).stdout.splitlines()
        )
        assert not any(
            name.startswith("hlb-cal18-")
            for name in _docker(
                "network", "ls", "--format", "{{.Name}}", check=False
            ).stdout.splitlines()
        )
        assert not any(
            name.startswith("hlb-cal18-")
            for name in _docker(
                "volume", "ls", "--format", "{{.Name}}", check=False
            ).stdout.splitlines()
        )


def _start_postgresql(
    *,
    name: str,
    network: str,
    volume: str,
    admin_user: str,
    admin_password: str,
    database: str,
) -> None:
    _docker(
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        f"{_LABEL}=postgresql",
        "--network",
        network,
        "--mount",
        f"source={volume},target=/var/lib/postgresql/data",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec",
        "--tmpfs",
        "/var/run/postgresql:rw,nosuid,nodev,noexec",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "768m",
        "--memory-swap",
        "768m",
        "-e",
        f"POSTGRES_USER={admin_user}",
        "-e",
        f"POSTGRES_PASSWORD={admin_password}",
        "-e",
        f"POSTGRES_DB={database}",
        _POSTGRES_IMAGE,
    )
    assert _docker(
        "inspect", "--format", "{{json .HostConfig.PortBindings}}", name
    ).stdout.strip() in {"null", "{}"}
    _wait_for_postgresql(name, user=admin_user)
    assert _psql(name, database, "SHOW server_version_num;", user=admin_user) == "160014"
    assert _psql(name, database, "SHOW server_version;", user=admin_user).startswith("16.14")


def _start_app(
    *,
    name: str,
    database_host: str,
    database: str,
    database_user: str,
    database_password: str,
    network: str,
    encryption_key: str,
    nextauth_secret: str,
    runner_image: str,
) -> None:
    database_url = (
        f"postgresql://{database_user}:{database_password}@{database_host}:5432/{database}"
    )
    _SYNTHETIC_SECRETS.add(database_url)
    _docker(
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        f"{_LABEL}=calcom",
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "512",
        "--memory",
        "1536m",
        "--memory-swap",
        "1536m",
        "-e",
        f"DATABASE_URL={database_url}",
        "-e",
        f"DATABASE_DIRECT_URL={database_url}",
        "-e",
        f"NEXT_PUBLIC_WEBAPP_URL=http://{name}:3000",
        "-e",
        f"NEXTAUTH_URL=http://{name}:3000",
        "-e",
        f"NEXTAUTH_SECRET={nextauth_secret}",
        "-e",
        f"CALENDSO_ENCRYPTION_KEY={encryption_key}",
        "-e",
        "NODE_ENV=production",
        _CALCOM_IMAGE,
    )
    assert _docker(
        "inspect", "--format", "{{json .HostConfig.PortBindings}}", name
    ).stdout.strip() in {"null", "{}"}
    assert json.loads(_docker("inspect", "--format", "{{json .Mounts}}", name).stdout) == []
    _wait_for_app(name, network, runner_image)


def _create_first_user(
    *,
    app: str,
    network: str,
    runner_image: str,
    password: str,
) -> None:
    script = r"""
import json, os, urllib.request
payload = json.dumps({
  "username": "phase-a",
  "full_name": "Synthetic Phase A",
  "email_address": "phase-a@example.invalid",
  "password": os.environ["SETUP_PASSWORD"],
}).encode()
request = urllib.request.Request(
  os.environ["SETUP_URL"], data=payload,
  headers={"Content-Type": "application/json"}, method="POST"
)
with urllib.request.urlopen(request, timeout=30) as response:
  assert response.status == 200
"""
    _docker(
        "run",
        "--rm",
        "--interactive",
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        f"SETUP_PASSWORD={password}",
        "-e",
        f"SETUP_URL=http://{app}:3000/api/auth/setup",
        runner_image,
        "python",
        "-c",
        script,
    )


def _encrypt_marker(*, key: str, plaintext: str) -> str:
    script = r"""
import { symmetricEncrypt } from "./packages/lib/crypto";
process.stdout.write(symmetricEncrypt(process.env.PLAINTEXT, process.env.KEY));
"""
    ciphertext = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        f"KEY={key}",
        "-e",
        f"PLAINTEXT={plaintext}",
        "--entrypoint",
        "/calcom/node_modules/.bin/ts-node",
        _CALCOM_IMAGE,
        "--transpile-only",
        "-e",
        script,
    ).stdout.strip()
    assert len(ciphertext) > 32 and ":" in ciphertext
    _SYNTHETIC_SECRETS.add(ciphertext)
    return ciphertext


def _assert_encryption_key_contract(*, ciphertext: str, key: str, plaintext: str) -> None:
    script = r"""
import { symmetricDecrypt } from "./packages/lib/crypto";
try {
  const value = symmetricDecrypt(process.env.CIPHERTEXT, process.env.KEY);
  process.exit(value === process.env.PLAINTEXT ? 0 : 3);
} catch (_) {
  process.exit(4);
}
"""

    def run(candidate_key: str) -> subprocess.CompletedProcess[str]:
        return _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-e",
            f"KEY={candidate_key}",
            "-e",
            f"PLAINTEXT={plaintext}",
            "-e",
            f"CIPHERTEXT={ciphertext}",
            "--entrypoint",
            "/calcom/node_modules/.bin/ts-node",
            _CALCOM_IMAGE,
            "--transpile-only",
            "-e",
            script,
            check=False,
        )

    assert run(key).returncode == 0
    wrong_key = hashlib.sha256(f"wrong-{key}".encode()).hexdigest()[:32]
    _SYNTHETIC_SECRETS.add(wrong_key)
    assert run(wrong_key).returncode != 0


def _seed_phase(
    container: str,
    *,
    phase: str,
    ciphertext: str,
) -> None:
    lower = phase.lower()
    ordinal = 1 if phase == "A" else 2
    if phase == "B":
        _psql(
            container,
            _DATABASE,
            f"""
INSERT INTO users (
  uuid, username, name, email, "emailVerified", "timeZone",
  "completedOnboarding", locale
) VALUES (
  '22222222-2222-4222-8222-222222222222', 'phase-b',
  'Synthetic Phase B', 'phase-b@example.invalid', now(), 'UTC', true, 'en'
);
UPDATE "Schedule" SET name = 'Synthetic Schedule A changed in B'
WHERE name = 'Synthetic Schedule A';
""",
            user="calcom",
        )
    user_email = f"phase-{lower}@example.invalid"
    schedule = f"Synthetic Schedule {phase}"
    event = f"Synthetic Event {phase}"
    event_slug = f"phase-{lower}-event"
    booking = f"phase-{lower}-booking"
    selected_uuid = f"{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}-1111-4111-8111-11111111111{ordinal}"
    webhook_uuid = f"{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}{ordinal}-2222-4222-8222-22222222222{ordinal}"
    _psql(
        container,
        _DATABASE,
        f"""
WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), inserted_schedule AS (
  INSERT INTO "Schedule" ("userId", name, "timeZone")
  SELECT id, '{schedule}', 'UTC' FROM selected_user
  RETURNING id, "userId"
), inserted_event AS (
  INSERT INTO "EventType" ("userId", title, slug, length, "scheduleId")
  SELECT selected_user.id, '{event}', '{event_slug}', {30 + ordinal}, inserted_schedule.id
  FROM selected_user, inserted_schedule
  RETURNING id, "userId"
), inserted_booking AS (
  INSERT INTO "Booking" (
    uid, "userId", "eventTypeId", title, "startTime", "endTime", "scheduledJobs"
  )
  SELECT '{booking}', "userId", id, 'Synthetic Booking {phase}',
         '2030-0{ordinal}-01T10:00:00Z', '2030-0{ordinal}-01T10:30:00Z', ARRAY[]::text[]
  FROM inserted_event
  RETURNING id
)
INSERT INTO "Attendee" ("bookingId", email, name, "timeZone")
SELECT id, 'attendee-{lower}@example.invalid', 'Synthetic Attendee {phase}', 'UTC'
FROM inserted_booking;

WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), inserted_credential AS (
  INSERT INTO "Credential" (type, key, "encryptedKey", "userId")
  SELECT 'synthetic_calendar', '{{"phase":"{phase}"}}'::jsonb,
         '{ciphertext}', id FROM selected_user
  RETURNING id, "userId"
)
INSERT INTO "SelectedCalendar" (
  id, "userId", integration, "externalId", "credentialId"
)
SELECT '{selected_uuid}'::uuid, "userId", 'synthetic_calendar',
       'calendar-{lower}', id FROM inserted_credential;

WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), selected_event AS (
  SELECT id FROM "EventType" WHERE slug = '{event_slug}'
), selected_credential AS (
  SELECT id FROM "Credential" WHERE "userId" = (SELECT id FROM selected_user)
)
INSERT INTO "DestinationCalendar" (
  integration, "externalId", "eventTypeId", "credentialId"
)
SELECT 'synthetic_calendar', 'destination-{lower}', selected_event.id,
       selected_credential.id FROM selected_event, selected_credential;

WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), inserted_workflow AS (
  INSERT INTO "Workflow" (name, "userId", trigger, time, "timeUnit")
  SELECT 'Synthetic Workflow {phase}', id, 'BEFORE_EVENT', {20 + ordinal}, 'hour'
  FROM selected_user RETURNING id
)
INSERT INTO "WorkflowStep" (
  "stepNumber", action, "workflowId", "sendTo", "reminderBody",
  "emailSubject", "numberVerificationPending"
)
SELECT 1, 'EMAIL_ATTENDEE', id, 'attendee', 'Synthetic reminder {phase}',
       'Synthetic subject {phase}', false FROM inserted_workflow;

WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), selected_event AS (
  SELECT id FROM "EventType" WHERE slug = '{event_slug}'
)
INSERT INTO "Webhook" (
  id, "userId", "eventTypeId", "subscriberUrl", active, "eventTriggers", secret
)
SELECT '{webhook_uuid}', selected_user.id, selected_event.id,
       'https://webhook-{lower}.invalid/hook', true,
       ARRAY['BOOKING_CREATED']::"WebhookTriggerEvents"[],
       'synthetic-webhook-{lower}'
FROM selected_user, selected_event;

WITH selected_user AS (SELECT id FROM users WHERE email = '{user_email}')
INSERT INTO "ApiKey" (id, "userId", note, "hashedKey")
SELECT 'phase-{lower}-api-key', id, 'Synthetic API {phase}',
       'synthetic-hash-{lower}' FROM selected_user;

WITH selected_user AS (
  SELECT id FROM users WHERE email = '{user_email}'
), selected_event AS (
  SELECT id FROM "EventType" WHERE slug = '{event_slug}'
)
INSERT INTO "_user_eventtype" ("A", "B")
SELECT selected_event.id, selected_user.id FROM selected_event, selected_user;

UPDATE users SET "completedOnboarding" = true,
  "defaultScheduleId" = (SELECT id FROM "Schedule" WHERE name = '{schedule}')
WHERE email = '{user_email}';
""",
        user="calcom",
    )


def _application_state(container: str, *, database: str = _DATABASE) -> dict[str, object]:
    payload = _psql(
        container,
        database,
        r"""
SELECT json_build_object(
  'users', (SELECT json_agg(username ORDER BY username) FROM users
            WHERE username LIKE 'phase-%'),
  'schedules', (SELECT json_agg(name ORDER BY name) FROM "Schedule"
                WHERE name LIKE 'Synthetic Schedule%'),
  'events', (SELECT json_agg(title ORDER BY title) FROM "EventType"
             WHERE title LIKE 'Synthetic Event%'),
  'bookings', (SELECT json_agg(uid ORDER BY uid) FROM "Booking"
               WHERE uid LIKE 'phase-%'),
  'attendees', (SELECT json_agg(name ORDER BY name) FROM "Attendee"
                WHERE name LIKE 'Synthetic Attendee%'),
  'credentials', (SELECT json_agg(json_build_array(
                    key->>'phase', length("encryptedKey")
                  ) ORDER BY key->>'phase') FROM "Credential"
                  WHERE type = 'synthetic_calendar'),
  'selected_calendars', (SELECT json_agg("externalId" ORDER BY "externalId")
                         FROM "SelectedCalendar" WHERE integration = 'synthetic_calendar'),
  'destination_calendars', (SELECT json_agg("externalId" ORDER BY "externalId")
                            FROM "DestinationCalendar"
                            WHERE integration = 'synthetic_calendar'),
  'workflows', (SELECT json_agg(name ORDER BY name) FROM "Workflow"
                WHERE name LIKE 'Synthetic Workflow%'),
  'workflow_steps', (SELECT json_agg("emailSubject" ORDER BY "emailSubject")
                     FROM "WorkflowStep" WHERE "emailSubject" LIKE 'Synthetic subject%'),
  'webhooks', (SELECT json_agg("subscriberUrl" ORDER BY "subscriberUrl")
               FROM "Webhook" WHERE "subscriberUrl" LIKE 'https://webhook-%'),
  'api_keys', (SELECT json_agg(note ORDER BY note) FROM "ApiKey"
               WHERE note LIKE 'Synthetic API%')
);
""",
        user="calcom" if container.startswith("hlb-cal18-source") else "restore_owner",
    )
    value = json.loads(payload)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _assert_state(state: dict[str, object], *, phase_b: bool) -> None:
    phases = ["A", "B"] if phase_b else ["A"]
    assert state["users"] == [f"phase-{phase.lower()}" for phase in phases]
    expected_schedules = ["Synthetic Schedule A changed in B", "Synthetic Schedule B"]
    if not phase_b:
        expected_schedules = ["Synthetic Schedule A"]
    assert state["schedules"] == expected_schedules
    assert state["events"] == [f"Synthetic Event {phase}" for phase in phases]
    assert state["bookings"] == [f"phase-{phase.lower()}-booking" for phase in phases]
    assert state["attendees"] == [f"Synthetic Attendee {phase}" for phase in phases]
    credentials = state["credentials"]
    assert isinstance(credentials, list)
    assert [entry[0] for entry in credentials] == phases
    assert all(isinstance(entry[1], int) and entry[1] > 32 for entry in credentials)
    assert state["selected_calendars"] == [f"calendar-{phase.lower()}" for phase in phases]
    assert state["destination_calendars"] == [f"destination-{phase.lower()}" for phase in phases]
    assert state["workflows"] == [f"Synthetic Workflow {phase}" for phase in phases]
    assert state["workflow_steps"] == [f"Synthetic subject {phase}" for phase in phases]
    assert state["webhooks"] == [
        f"https://webhook-{phase.lower()}.invalid/hook" for phase in phases
    ]
    assert state["api_keys"] == [f"Synthetic API {phase}" for phase in phases]


def _assert_app_content(*, app: str, network: str, runner_image: str, phase_b: bool) -> None:
    expected = ["A", "B"] if phase_b else ["A"]
    unexpected = [] if phase_b else ["B"]
    request = {
        "base_url": f"http://{app}:3000",
        "expected": expected,
        "unexpected": unexpected,
    }
    script = r"""
import json, sys, urllib.error, urllib.request
request = json.load(sys.stdin)
for phase in request["expected"]:
    lower = phase.lower()
    event = urllib.request.urlopen(
      f'{request["base_url"]}/phase-{lower}/phase-{lower}-event', timeout=30
    ).read().decode("utf-8")
    booking = urllib.request.urlopen(
      f'{request["base_url"]}/booking/phase-{lower}-booking', timeout=30
    ).read().decode("utf-8")
    assert f"Synthetic Event {phase}" in event
    assert f"Synthetic Booking {phase}" in booking
for phase in request["unexpected"]:
    lower = phase.lower()
    try:
        urllib.request.urlopen(
          f'{request["base_url"]}/phase-{lower}/phase-{lower}-event', timeout=30
        )
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("phase-absent event unexpectedly exists")
"""
    _docker(
        "run",
        "--rm",
        "--interactive",
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "python",
        runner_image,
        "-c",
        script,
        input_text=json.dumps(request),
    )


_BACKUP_SCRIPT = r"""
import asyncio, json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.db import Base
from app.core.scheduler import run_job_immediately
from app.core.plugins.base import BackupContext
from app.models import Job, Tag, Target, TargetRun, TargetTag
from app.plugins.calcom import CalcomPlugin

request = json.load(sys.stdin)

async def main():
    plugin = CalcomPlugin(name="calcom", base_dir="/backups")
    assert await plugin.test(request["config"]) is True
    status = await plugin.get_status(BackupContext(
      job_id="calcom-drill-status", target_id="calcom-source",
      config=request["config"], metadata={"target_slug": "calcom-source"}
    ))
    engine = create_engine(
      "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as session:
        tag = Tag(display_name="Cal.com exact drill")
        target = Target(
          name="Cal.com exact source", slug="calcom-source", plugin_name="calcom",
          plugin_config_json=json.dumps(request["config"])
        )
        session.add_all([tag, target]); session.flush()
        session.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
        job = Job(
          tag_id=tag.id, name="Cal.com exact backup",
          schedule_cron="0 0 * * *", enabled=True
        )
        session.add(job); session.commit()
        run = run_job_immediately(session, job.id, triggered_by="exact_calcom_drill")
        target_run = session.query(TargetRun).filter(TargetRun.run_id == run.id).one()
        if run.status != "success" or target_run.status != "success":
            raise RuntimeError(target_run.message or run.message or "backup run failed")
        sys.stdout.write(json.dumps({
          "status": status,
          "artifact_path": target_run.artifact_path,
          "artifact_bytes": target_run.artifact_bytes,
          "sha256": target_run.sha256,
          "source_identity": json.loads(target_run.source_identity_json),
          "run_status": run.status,
          "target_status": target_run.status,
        }) + "\n")

if __name__ == "__main__":
    asyncio.run(main())
"""


_RESTORE_SCRIPT = r"""
import json, sys
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.db import Base
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService

request = json.load(sys.stdin)
engine = create_engine(
  "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
with Session() as session:
    source = Target(
      name="Cal.com drill source", slug="calcom-source", plugin_name="calcom",
      plugin_config_json=json.dumps(request["source_config"])
    )
    destination = Target(
      name="Cal.com drill destination", slug="calcom-destination", plugin_name="calcom",
      plugin_config_json=json.dumps(request["destination_config"])
    )
    session.add_all([source, destination]); session.flush()
    now = datetime.now(timezone.utc)
    run = Run(
      status="success", operation="backup", started_at=now, finished_at=now
    )
    session.add(run); session.flush()
    source_run = TargetRun(
      run_id=run.id, target_id=source.id, status="success", operation="backup",
      artifact_path=request["artifact_path"],
      artifact_bytes=request["artifact_bytes"], sha256=request["artifact_sha256"],
      source_identity_json=json.dumps(request["source_identity"]),
      started_at=now, finished_at=now
    )
    session.add(source_run); session.commit()
    destination_id = source.id if request.get("same_target") else destination.id
    restored = RestoreService(session).restore(
      source_target_run_id=source_run.id,
      destination_target_id=destination_id,
      triggered_by="exact_calcom_drill",
    )
    target_run = restored.target_runs[0]
    sys.stdout.write(json.dumps({
      "status": restored.status,
      "target_status": target_run.status,
      "artifact_path": target_run.artifact_path,
      "artifact_bytes": target_run.artifact_bytes,
      "sha256": target_run.sha256,
    }) + "\n")
"""


def _run_backup(
    *,
    name: str,
    network: str,
    source: str,
    backup_root: Path,
    password: str,
    runner_image: str,
) -> dict[str, object]:
    request = json.dumps(
        {
            "config": {
                "mode": "source",
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
                "password": password,
            }
        }
    )
    _docker(
        "create",
        "--name",
        name,
        "--interactive",
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=192m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "160",
        "--memory",
        "768m",
        "--memory-swap",
        "768m",
        "-v",
        f"{backup_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        _BACKUP_SCRIPT,
    )
    try:
        mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", name).stdout)
        assert len(mounts) == 1 and mounts[0]["Destination"] == "/backups"
        assert mounts[0]["RW"] is True
        completed = _docker(
            "start", "--attach", "--interactive", name, input_text=request, timeout=300
        )
    finally:
        _docker("rm", "-f", name, check=False)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return cast(dict[str, object], value)
    raise RuntimeError(f"Cal.com backup runner emitted no result: {_redact(completed.stderr)}")


def _run_restore(
    *,
    name: str,
    network: str,
    source: str,
    destination: str,
    destination_database: str,
    backup_root: Path,
    artifact: Path,
    source_password: str,
    restore_password: str,
    runner_image: str,
    allow: bool = True,
    same_target: bool = False,
) -> dict[str, object]:
    request = json.dumps(
        {
            "artifact_path": "/backups/" + str(artifact.relative_to(backup_root)),
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": _sha256(artifact),
            "source_config": {
                "mode": "source",
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
                "password": source_password,
            },
            "destination_config": {
                "mode": "restore_destination",
                "host": destination,
                "port": 5432,
                "database": destination_database,
                "user": "restore_owner",
                "password": restore_password,
            },
            "source_identity": {
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
            },
            "same_target": same_target,
        }
    )
    arguments = [
        "create",
        "--name",
        name,
        "--interactive",
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=192m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "160",
        "--memory",
        "768m",
        "--memory-swap",
        "768m",
        "-e",
        "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1",
    ]
    if allow:
        arguments.extend(
            [
                "-e",
                (
                    "HOMELAB_BACKUP_ISOLATED_CALCOM_RESTORE_DESTINATIONS="
                    f"{destination}:5432/{destination_database}"
                ),
            ]
        )
    arguments.extend(
        [
            "-v",
            f"{backup_root}:/backups:rw",
            runner_image,
            "python",
            "-c",
            _RESTORE_SCRIPT,
        ]
    )
    _docker(*arguments)
    try:
        completed = _docker(
            "start", "--attach", "--interactive", name, input_text=request, timeout=300
        )
    finally:
        _docker("rm", "-f", name, check=False)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return cast(dict[str, object], value)
    raise RuntimeError(f"Cal.com restore runner emitted no result: {_redact(completed.stderr)}")


def _create_restore_database(
    *,
    container: str,
    database: str,
    restore_password: str,
) -> None:
    _psql(
        container,
        "postgres",
        f"""
CREATE ROLE restore_owner LOGIN PASSWORD '{restore_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
CREATE DATABASE {database} WITH OWNER restore_owner TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE {database} FROM PUBLIC;
COMMENT ON DATABASE {database} IS 'homelab-backup:calcom-restore:v1';
""",
    )


def _assert_artifact(
    *, artifact: Path, expected_count: int, forbidden: set[str]
) -> dict[str, object]:
    sidecar_path = Path(f"{artifact}.meta.json")
    assert artifact.is_file() and artifact.stat().st_size > 0
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert isinstance(sidecar, dict)
    assert sidecar["application_version"] == "6.2.0"
    assert sidecar["validation"] == "calcom-postgresql-v1"
    assert sidecar["migration_head"] == (
        "20260219000000_add_fallback_action_to_queued_form_response"
    )
    assert sidecar["schema_sha256"] == (
        "f1112b98123f36ae502f39173e523545f7a41959c35351ef200a3f2b7fd66e52"
    )
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == _sha256(artifact)
    marker_counts = sidecar["marker_counts"]
    assert isinstance(marker_counts, dict)
    assert set(marker_counts.values()) == {expected_count}
    serialized = json.dumps(sidecar, sort_keys=True)
    assert all(secret not in serialized for secret in forbidden)
    return cast(dict[str, object], sidecar)


@pytest.mark.parametrize("drill_round", (1, 2))
def test_exact_calcom_two_backups_and_two_fresh_app_restores(
    tmp_path: Path,
    drill_round: int,
) -> None:
    """Exact A/B Cal.com state restores to fresh DBs and survives app restarts."""
    suffix = f"r{drill_round}-{uuid.uuid4().hex[:8]}"
    network = f"hlb-cal18-{suffix}"
    source_pg = f"hlb-cal18-source-pg-{suffix}"
    source_app = f"hlb-cal18-source-app-{suffix}"
    source_volume = f"hlb-cal18-source-{suffix}"
    destination_pgs = [f"hlb-cal18-destination-{phase}-pg-{suffix}" for phase in "ab"]
    destination_apps = [f"hlb-cal18-destination-{phase}-app-{suffix}" for phase in "ab"]
    destination_volumes = [f"hlb-cal18-destination-{phase}-{suffix}" for phase in "ab"]
    admin_password = f"synthetic-admin-{suffix}"
    backup_password = f"synthetic-backup-{suffix}"
    setup_password = f"Synthetic-Setup-{suffix}-A1"
    encryption_key = hashlib.sha256(f"encryption-{suffix}".encode()).hexdigest()[:32]
    nextauth_secret = hashlib.sha256(f"nextauth-{suffix}".encode()).hexdigest()
    destination_admin_passwords = [f"synthetic-dest-admin-{p}-{suffix}" for p in "ab"]
    restore_passwords = [f"synthetic-restore-{p}-{suffix}" for p in "ab"]
    _SYNTHETIC_SECRETS.update(
        {
            admin_password,
            backup_password,
            setup_password,
            encryption_key,
            nextauth_secret,
            *destination_admin_passwords,
            *restore_passwords,
        }
    )
    volumes = [source_volume, *destination_volumes]

    with _managed_resources(
        suffix=suffix,
        network=network,
        volumes=volumes,
        backup_root=tmp_path,
    ) as runner_image:
        _start_postgresql(
            name=source_pg,
            network=network,
            volume=source_volume,
            admin_user="calcom",
            admin_password=admin_password,
            database=_DATABASE,
        )
        _start_app(
            name=source_app,
            database_host=source_pg,
            database=_DATABASE,
            database_user="calcom",
            database_password=admin_password,
            network=network,
            encryption_key=encryption_key,
            nextauth_secret=nextauth_secret,
            runner_image=runner_image,
        )
        _create_first_user(
            app=source_app,
            network=network,
            runner_image=runner_image,
            password=setup_password,
        )
        ciphertexts = {
            phase: _encrypt_marker(
                key=encryption_key,
                plaintext=f"synthetic-provider-secret-{phase}-{suffix}",
            )
            for phase in "AB"
        }
        _docker("stop", source_app)
        _seed_phase(source_pg, phase="A", ciphertext=ciphertexts["A"])
        _docker("start", source_app)
        _wait_for_app(source_app, network, runner_image)
        _assert_state(_application_state(source_pg), phase_b=False)
        _assert_app_content(
            app=source_app, network=network, runner_image=runner_image, phase_b=False
        )

        _psql(
            source_pg,
            "postgres",
            f"""
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE template1 FROM PUBLIC;
""",
            user="calcom",
        )
        _psql(
            source_pg,
            _DATABASE,
            f"""
REVOKE TEMPORARY ON DATABASE {_DATABASE} FROM PUBLIC;
CREATE ROLE {_BACKUP_USER} LOGIN PASSWORD '{backup_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE {_DATABASE} TO {_BACKUP_USER};
GRANT USAGE ON SCHEMA public TO {_BACKUP_USER};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_BACKUP_USER};
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {_BACKUP_USER};
""",
            user="calcom",
        )

        _psql(source_pg, _DATABASE, f"GRANT INSERT ON users TO {_BACKUP_USER};", user="calcom")
        with pytest.raises(RuntimeError, match="write privileges"):
            _run_backup(
                name=f"hlb-cal18-negative-write-{suffix}",
                network=network,
                source=source_pg,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source_pg, _DATABASE, f"REVOKE INSERT ON users FROM {_BACKUP_USER};", user="calcom")

        _psql(source_pg, _DATABASE, "ALTER TABLE users ENABLE ROW LEVEL SECURITY;", user="calcom")
        with pytest.raises(RuntimeError, match="unsupported RLS"):
            _run_backup(
                name=f"hlb-cal18-negative-rls-{suffix}",
                network=network,
                source=source_pg,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source_pg, _DATABASE, "ALTER TABLE users DISABLE ROW LEVEL SECURITY;", user="calcom")

        _psql(
            source_pg,
            _DATABASE,
            "UPDATE _prisma_migrations SET migration_name = 'synthetic_drift' "
            "WHERE migration_name = "
            "'20260219000000_add_fallback_action_to_queued_form_response';",
            user="calcom",
        )
        with pytest.raises(RuntimeError, match="migration inventory"):
            _run_backup(
                name=f"hlb-cal18-negative-migration-{suffix}",
                network=network,
                source=source_pg,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(
            source_pg,
            _DATABASE,
            "UPDATE _prisma_migrations SET migration_name = "
            "'20260219000000_add_fallback_action_to_queued_form_response' "
            "WHERE migration_name = 'synthetic_drift';",
            user="calcom",
        )

        _psql(
            source_pg,
            _DATABASE,
            "CREATE TABLE synthetic_schema_drift (id integer PRIMARY KEY); "
            f"GRANT SELECT ON synthetic_schema_drift TO {_BACKUP_USER};",
            user="calcom",
        )
        with pytest.raises(RuntimeError, match="catalog inventory"):
            _run_backup(
                name=f"hlb-cal18-negative-schema-{suffix}",
                network=network,
                source=source_pg,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(
            source_pg,
            _DATABASE,
            "DROP TABLE synthetic_schema_drift;",
            user="calcom",
        )

        backup_a = _run_backup(
            name=f"hlb-cal18-backup-a-{suffix}",
            network=network,
            source=source_pg,
            backup_root=tmp_path,
            password=backup_password,
            runner_image=runner_image,
        )
        artifact_a = tmp_path / Path(cast(str, backup_a["artifact_path"])).relative_to("/backups")
        signature_a = (artifact_a.stat().st_size, _sha256(artifact_a))
        sidecar_a = _assert_artifact(
            artifact=artifact_a,
            expected_count=1,
            forbidden=_SYNTHETIC_SECRETS,
        )
        assert backup_a["run_status"] == "success"
        assert backup_a["target_status"] == "success"
        assert backup_a["sha256"] == signature_a[1]

        _docker("stop", source_app)
        _seed_phase(source_pg, phase="B", ciphertext=ciphertexts["B"])
        _docker("start", source_app)
        _wait_for_app(source_app, network, runner_image)
        _assert_state(_application_state(source_pg), phase_b=True)
        _assert_app_content(
            app=source_app, network=network, runner_image=runner_image, phase_b=True
        )
        backup_b = _run_backup(
            name=f"hlb-cal18-backup-b-{suffix}",
            network=network,
            source=source_pg,
            backup_root=tmp_path,
            password=backup_password,
            runner_image=runner_image,
        )
        artifact_b = tmp_path / Path(cast(str, backup_b["artifact_path"])).relative_to("/backups")
        signature_b = (artifact_b.stat().st_size, _sha256(artifact_b))
        sidecar_b = _assert_artifact(
            artifact=artifact_b,
            expected_count=2,
            forbidden=_SYNTHETIC_SECRETS,
        )
        assert artifact_a != artifact_b
        assert signature_a != signature_b
        assert (artifact_a.stat().st_size, _sha256(artifact_a)) == signature_a
        assert sidecar_a["marker_profile_sha256"] != sidecar_b["marker_profile_sha256"]

        _docker("rm", "-f", source_app)

        for index, phase in enumerate("ab"):
            destination_pg = destination_pgs[index]
            destination_app = destination_apps[index]
            destination_database = f"hlb_calcom_restore_{phase}_{drill_round}"
            _start_postgresql(
                name=destination_pg,
                network=network,
                volume=destination_volumes[index],
                admin_user="postgres",
                admin_password=destination_admin_passwords[index],
                database="postgres",
            )
            _create_restore_database(
                container=destination_pg,
                database=destination_database,
                restore_password=restore_passwords[index],
            )
            artifact = artifact_a if phase == "a" else artifact_b

            if phase == "a":
                corrupt = tmp_path / f"corrupt-{suffix}.dump"
                shutil.copyfile(artifact, corrupt)
                corrupt.chmod(0o600)
                with corrupt.open("ab") as stream:
                    stream.write(b"corrupt")
                shutil.copyfile(Path(f"{artifact}.meta.json"), Path(f"{corrupt}.meta.json"))
                Path(f"{corrupt}.meta.json").chmod(0o600)
                with pytest.raises(RuntimeError, match="hash|sidecar|identity|provenance"):
                    _run_restore(
                        name=f"hlb-cal18-corrupt-{suffix}",
                        network=network,
                        source=source_pg,
                        destination=destination_pg,
                        destination_database=destination_database,
                        backup_root=tmp_path,
                        artifact=corrupt,
                        source_password=backup_password,
                        restore_password=restore_passwords[index],
                        runner_image=runner_image,
                    )

            if phase == "b":
                _psql(
                    destination_pg,
                    "postgres",
                    f"COMMENT ON DATABASE {destination_database} IS NULL;",
                )
                with pytest.raises(RuntimeError, match="sentinel"):
                    _run_restore(
                        name=f"hlb-cal18-missing-sentinel-{suffix}",
                        network=network,
                        source=source_pg,
                        destination=destination_pg,
                        destination_database=destination_database,
                        backup_root=tmp_path,
                        artifact=artifact,
                        source_password=backup_password,
                        restore_password=restore_passwords[index],
                        runner_image=runner_image,
                    )
                _psql(
                    destination_pg,
                    "postgres",
                    (
                        f"COMMENT ON DATABASE {destination_database} IS "
                        "'homelab-backup:calcom-restore:v1';"
                    ),
                )

            with pytest.raises(RuntimeError, match="disabled|allowlist|authorized"):
                _run_restore(
                    name=f"hlb-cal18-unauthorized-{phase}-{suffix}",
                    network=network,
                    source=source_pg,
                    destination=destination_pg,
                    destination_database=destination_database,
                    backup_root=tmp_path,
                    artifact=artifact,
                    source_password=backup_password,
                    restore_password=restore_passwords[index],
                    runner_image=runner_image,
                    allow=False,
                )
            assert (
                int(
                    _psql(
                        destination_pg,
                        destination_database,
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid=c.relnamespace WHERE n.nspname='public' "
                        "AND c.relkind IN ('r','p','v','m','f','S');",
                        user="restore_owner",
                    )
                )
                == 0
            )

            restored = _run_restore(
                name=f"hlb-cal18-restore-{phase}-{suffix}",
                network=network,
                source=source_pg,
                destination=destination_pg,
                destination_database=destination_database,
                backup_root=tmp_path,
                artifact=artifact,
                source_password=backup_password,
                restore_password=restore_passwords[index],
                runner_image=runner_image,
            )
            assert restored["status"] == "partial"
            assert restored["target_status"] == "partial"
            assert restored["artifact_bytes"] == artifact.stat().st_size
            assert restored["sha256"] == _sha256(artifact)
            _assert_state(
                _application_state(destination_pg, database=destination_database),
                phase_b=phase == "b",
            )
            expected_plaintext = f"synthetic-provider-secret-{phase.upper()}-{suffix}"
            _assert_encryption_key_contract(
                ciphertext=ciphertexts[phase.upper()],
                key=encryption_key,
                plaintext=expected_plaintext,
            )

            with pytest.raises(RuntimeError, match="fresh|empty"):
                _run_restore(
                    name=f"hlb-cal18-nonfresh-{phase}-{suffix}",
                    network=network,
                    source=source_pg,
                    destination=destination_pg,
                    destination_database=destination_database,
                    backup_root=tmp_path,
                    artifact=artifact,
                    source_password=backup_password,
                    restore_password=restore_passwords[index],
                    runner_image=runner_image,
                )
            _assert_state(
                _application_state(destination_pg, database=destination_database),
                phase_b=phase == "b",
            )

            _start_app(
                name=destination_app,
                database_host=destination_pg,
                database=destination_database,
                database_user="restore_owner",
                database_password=restore_passwords[index],
                network=network,
                encryption_key=encryption_key,
                nextauth_secret=nextauth_secret,
                runner_image=runner_image,
            )
            _assert_app_content(
                app=destination_app,
                network=network,
                runner_image=runner_image,
                phase_b=phase == "b",
            )
            _docker("restart", destination_pg)
            _wait_for_postgresql(destination_pg, user="postgres")
            _docker("restart", destination_app)
            _wait_for_app(destination_app, network, runner_image)
            _assert_state(
                _application_state(destination_pg, database=destination_database),
                phase_b=phase == "b",
            )
            _assert_app_content(
                app=destination_app,
                network=network,
                runner_image=runner_image,
                phase_b=phase == "b",
            )
            _docker("rm", "-f", destination_app)
