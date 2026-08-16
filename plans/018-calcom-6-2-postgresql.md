# Plan 018: Revalidate Cal.com 6.2.0 PostgreSQL recovery

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 017 generic PostgreSQL 16 foundation
- **State**: IN PROGRESS
- **Restore capability**: `partial`
- **Production status**: local work only; every production restore is forbidden
- **Fixed point**: `2e317c7`

## Outcome

Replace the legacy URL-shaped `calcom` plugin with a thin strict adapter over
the committed PostgreSQL 16 archive module. Prove two distinct online backups,
two independently fresh database restores, exact Cal.com application content,
and restart persistence using immutable Linux/amd64 manifests:

```text
calcom/cal.com@sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001
postgres@sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00
```

The named `calendso` database is the complete local durable Cal.com state in the
declared deployment. Cluster roles, PostgreSQL configuration, the original
`CALENDSO_ENCRYPTION_KEY`, NextAuth/deployment secrets, OAuth configuration,
SMTP, external calendars/video systems, DNS, and application lifecycle are
external recovery prerequisites. The plugin therefore reports `partial` even
when the named database is restored and verified completely.

Primary-source research, exact image identities, consistency analysis, and
production gates are in `plans/research/calcom.md`.

## Deep-module boundary

Keep PostgreSQL process, credential, archive, provenance, and transactional
restore mechanics inside `app.core.plugins.postgresql`. The Cal.com adapter owns
only:

- exact v6.2.0 migration and normalized catalog identity;
- Cal.com-specific typed marker queries and equality proof;
- app version and adapter validation evidence in the sidecar;
- Cal.com-specific local restore authorization/sentinel; and
- an honest `partial` restore result naming external prerequisites.

Do not copy the generic PostgreSQL process implementation into the adapter. Add
the smallest profile/query seam needed to reuse descriptor-bound capture and
restore. Do not weaken generic PostgreSQL checks or preserve the old Cal.com URL,
`database_direct_url`, `PGPASSWORD`, destructive restore, or automatic-capability
behavior without explicit compatibility approval.

## Confirmed public test seams

The user-approved service program and this plan fix three public seams:

1. loader plus `/api/v1/plugins`, schema, target persistence, `/test`, and
   `get_status()` for configuration and exact read-only identity;
2. a real Target/Job/Run/TargetRun scheduled backup returning one private
   artifact and valid sidecar; and
3. `RestoreService` staging/audit into a fresh destination, followed by exact
   Cal.com image boot, content checks, and restart persistence.

Private helper tests are reserved for bounded malicious TOC/catalog/marker input
and child-process lifecycle cases that cannot safely be produced through those
public seams.

## Exact configuration and probe

Use the same flat clean-breaking fields as the generic PostgreSQL adapter:

- source: `mode`, `host`, `port`, `database`, `user`, `password`;
- restore destination: the same fields with `mode=restore_destination`.

Reject URLs, aliases, unknown keys, coercions, empty/control-character values,
unsafe ports/names, inactive fallback fields, and source/restore confusion. No
credential has a default.

The real bounded probe must first satisfy the generic PG16 read-only/privilege
contract, then require exact Cal.com v6.2.0 evidence:

- database name matches configuration; encoding/collation evidence is explicit;
- Prisma migration history is complete, successful, contains no future/failed
  row, and ends at
  `20260219000000_add_fallback_action_to_queued_form_response`;
- normalized schema, extension, relation, sequence, index, constraint, routine,
  type, RLS, and large-object evidence matches the pinned exact-image inventory;
- typed marker counts/hashes cover users, schedules, event types, attendees,
  bookings, credentials, selected/destination calendars, workflows, webhooks,
  and API-key-shaped state without returning or logging values; and
- the backup role can read every required object but cannot write data, mutate
  sequences, create schema/database/role objects, bypass RLS, signal backends,
  access server files/programs, or connect to unrelated databases.

Before locking constants, boot the exact local app/database pair, run first-party
migrations, and capture the full non-secret migration/catalog inventory. Compare
it with exact v6.2.0 source. Stop on disagreement rather than inventing a
compatibility fingerprint.

## Backup contract

Use Plan 017's version-addressable PG16 clients, private mode-0600 PGPASSFILE,
fixed no-shell environment, external file-size limiter, one cumulative deadline,
held descriptor, strict TOC/catalog binding, cancellation-safe child teardown,
bounded hashing, private artifact publication, and generic size/SHA sidecar.

The Cal.com adapter additionally requires a stable pre/post application profile:

- exact migration head and schema fingerprint;
- exact marker-count/hash projection before and after capture;
- no migration/catalog/marker drift across the dump boundary; and
- sidecar fields limited to app version, migration head, schema/profile digests,
  generic PG evidence, and non-secret counts.

Any drift retries as one complete attempt within the same deadline; bounded
exhaustion fails with no artifact or sidecar. Never include row values, emails,
credentials, API keys, webhook URLs, private paths, connection details, or the
Cal.com encryption key in metadata, errors, or logs.

## Restore contract

Restore only through `RestoreService` from its private staged copy. Require:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- an exact Cal.com-specific destination allowlist entry;
- distinct source/destination targets and database identities;
- a PostgreSQL 16 database created from `template0`;
- exact comment sentinel `homelab-backup:calcom-restore:v1`;
- zero non-system objects and no other user connection; and
- a destination owner without cluster-wide/unrelated-database privilege.

Bind the staged descriptor and revalidate generic artifact identity/provenance,
the exact Cal.com app/migration/schema/profile evidence, and TOC before mutation.
Run only the generic fixed `pg_restore --exit-on-error --single-transaction
--no-owner --no-privileges` path. Never clean, create, drop, disable triggers,
continue after error, or use shell execution.

After restore, require exact migration/catalog identity and exact marker
count/hash equality with the source artifact. Return `partial` with a secret-safe
message explaining that exact-image boot and external encryption/deployment
configuration remain operator prerequisites. A failed restore leaves the fresh
sentinel database for inspection and never retries destructively.

## Vertical TDD slices

1. Discovery, flat schema, strict mode configuration, partial capability, and
   target/API persistence.
2. Exact local v6.2.0 migration/catalog/profile fixture plus real public
   `test()`/`get_status()` and least-privilege failures.
3. Scheduled private backup using the generic PG16 capture with exact Cal.com
   sidecar/profile evidence and stable pre/post fence.
4. Malformed/wrong-version/migration/catalog/profile, drift, replacement,
   warning, bound, timeout, repeated-cancellation, and no-publication cases.
5. Cal.com-specific isolated authorization, source provenance, exact sentinel,
   freshness, same-target, wrong-major, and unapproved-destination refusal.
6. RestoreService descriptor-bound transactional restore, exact post-restore
   marker equality, partial audit, rollback, timeout, and cancellation.
7. Immutable two-round Cal.com 6.2.0/PG16.14 Docker drill with application-level
   A/B content, two fresh destinations, restarts, negatives, and cleanup audit.

Work red to green one vertical slice at a time. Do not bulk-write imagined tests
or test internal call ordering.

## Exact local drill

Use synthetic credentials/data on unique internal Docker networks with no
published ports, host networking, Docker socket, privileged workload, production
route, or host mount except the central artifact directory exposed narrowly to
the runner. Use the same synthetic encryption key only inside each disposable
source/destination topology and never print or retain it.

For each of two clean parameterized rounds:

1. Boot exact PG16.14 and Cal.com 6.2.0, allow first-party migration/seed, and
   prove immutable image/source identity plus app readiness.
2. Create a dedicated denied-write backup role and prove every required positive
   read and negative authority boundary.
3. Through supported Cal.com paths seed phase A user/profile, schedule, event
   type, attendee/booking, credential/calendar, workflow, webhook, and API-key
   markers without contacting an external provider.
4. Back up through real Target/Job/Run/TargetRun; independently inspect private
   artifact/sidecar/hash, exact migration/catalog/profile, and absence of secrets.
5. Mutate supported state to phase B and create immutable artifact B. Prove B is
   distinct and A stayed byte-identical with phase-A-only evidence.
6. Create separate destination A from `template0` with its own owner/sentinel,
   restore A through RestoreService, and prove exact database markers.
7. Boot the exact app against destination A with the synthetic external config;
   prove readiness, phase-A content, phase-B absence, then restart database and
   app on unchanged volumes and repeat.
8. Destroy destination A and repeat restore/boot/restart proof for B on a
   separately fresh destination, including phase-B differences.
9. Exercise representative wrong app/PG/migration/schema, overprivileged/RLS,
   dump warning/failure/cancellation, corrupt/replaced/wrong-plugin/altered
   artifact, missing sentinel, same/nonfresh/unapproved destination,
   transactional failure, wrong encryption key at boot, and marker mismatch.
10. Remove and independently audit every labeled/prefixed container, network,
    volume, runner image, listener, temporary artifact, and synthetic credential.

Two backups with one restore, database-only SQL queries without exact app boot,
or two restores into a reused destination are not completion evidence.

## Verification and completion

- focused Cal.com, generic PostgreSQL, HTTP, scheduler, and RestoreService tests;
- two complete exact Cal.com 6.2.0/PG16.14 clean drill rounds;
- full backend and frontend gates;
- application mypy, changed-file Black/isort, SemVer, diff, and secret scan;
- exact image/tool health and independent resource cleanup audit;
- Standards and Spec reviews with no unresolved P0-P3 issue;
- compatibility, recovery, changelog, ledger, and plan evidence current; and
- one independent focused milestone commit, with no push or deploy.

Mark `DONE (local)` only after every item passes. Production remains
rollout-pending until actual app/database runtime digests, the DMZ database-only
network path, exact dedicated role/default grants, target/job, and one approved
backup-only run are separately verified.

## STOP conditions

Stop rather than weaken the contract if exact v6.2.0 identity/migrations/catalog
cannot be bound; a complete dump needs ownership, write, superuser, RLS bypass,
unclassified large-object, server-file/program, Docker, host, or broad network
authority; PG16 cannot transactionally restore the archive; exact app boot needs
a real external provider/production secret; the source key cannot decrypt
synthetic restored credentials; cleanup cannot complete before return; a
compatibility path is requested without approval; or any production write,
restore, credential grant, network change, downtime, or broader privilege would
be required.
