# Plan 017: Revalidate PostgreSQL 16 logical archive recovery

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: IN PROGRESS
- **Restore capability**: `automatic` for the named database boundary
- **Production status**: local work only; every production restore is forbidden
- **Fixed point**: `4691e11`

## Outcome

Replace the legacy generic `postgresql` implementation with a strict PostgreSQL
16 logical-archive module and adapter. Prove two distinct online backups and two
independently precreated, fresh, transactional restores using the immutable
Linux/amd64 PostgreSQL 16.14 manifest:

```text
postgres@sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00
```

This milestone owns only a named PostgreSQL database. Application processes,
external providers, deployment configuration, cluster-global roles and
tablespaces remain outside that recovery boundary. Plan 018 will use this
foundation for exact Cal.com 6.2.0 application proof. The primary-source
contract and production gates are recorded in `plans/research/calcom.md`.

## Deep-module design

Put PostgreSQL process lifetime, private authentication, streaming, archive
inspection, provenance, and transactional restore behind one small internal
interface in `app.core.plugins`. The generic PostgreSQL plugin is the first
adapter; Cal.com becomes the second adapter in Plan 018.

The module interface must hide:

- private `PGPASSFILE` creation, mode checks and cleanup;
- fixed client argv/environment construction;
- bounded stderr/stdout and archive streaming;
- cancellation-safe terminate/kill/reap behavior;
- held-descriptor artifact validation and normalized TOC/schema evidence;
- transactional sidecar-bound publication; and
- fresh-destination authorization, validation and restore cleanup.

Do not expose subprocess, temporary-path, file-descriptor, TOC parsing, or
credential details through the public plugin interface. Keep internal seams
private and use them only for deterministic lifecycle/adversarial tests.

## Exact public contract

### Configuration

Use one flat clean-breaking schema with explicit `mode`:

- source: `mode`, `host`, `port`, `database`, `user`, `password`;
- restore destination: the same fields, with mode
  `restore_destination`.

Reject URLs, aliases, unknown/inactive keys, coercions, empty/control-character
values, unsafe ports and database names, and source/restore mode confusion.
Remove every placeholder password default. Never preserve the old optional or
URL-shaped behavior without explicit user approval.

### Probe and source evidence

`test()` and `get_status()` use the same real bounded read-only PostgreSQL 16
probe and require:

- exact requested database identity and PostgreSQL major 16;
- UTF-8 plus explicit collation/ctype evidence;
- a complete user schema, extension, relation, sequence, RLS and large-object
  inventory;
- no unclassified RLS-enabled table or large object; and
- a dedicated identity that can read required schemas/tables/sequences while
  write, DDL, role/database creation, replication, server files/programs and
  unrelated databases remain denied.

The generic adapter records observed normalized catalog evidence; it does not
invent an application schema allowlist. Application adapters must supply their
own exact fingerprint and marker contract.

### Backup

Run the version-addressable PostgreSQL 16 client with fixed no-shell argv:

```text
pg_dump --format=custom --no-owner --no-privileges
```

Stream to `create_backup_artifact()`'s private temporary descriptor. Use a
private mode-0600 `PGPASSFILE`; never use a URL, argv password, `PGPASSWORD`,
ambient libpq settings, or inherited service configuration. Apply one fixed
operation deadline covering lock wait, connection, streaming, worker teardown,
TOC/schema validation, fsync and publication.

Fail on nonzero exit, warnings, empty/oversized output, malformed custom
archive, ambiguous or unsafe TOC, client/server-major drift, replacement race,
timeout or cancellation. Before publication, inspect the still-bound artifact
descriptor and store only secret-safe evidence in the sidecar: PostgreSQL
major/patch, source identity hash, encoding/collation, normalized catalog and
TOC digests, object counts, RLS/large-object classification, validation
version, and generic artifact size/SHA-256.

### Restore

Restore only through `RestoreService` from its private staged copy. Require:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- an exact allowlist entry for destination host, port and database;
- distinct source/destination targets and database identities;
- a precreated PostgreSQL 16 database made from `template0`;
- exact database comment sentinel `homelab-backup:postgresql-restore:v1`;
- zero non-system objects and no other user connection; and
- a destination owner with no cluster-wide or unrelated-database privilege.

Bind the staged artifact descriptor and independently verify its recorded
size/SHA-256, producer/target provenance, PostgreSQL major, normalized TOC and
schema evidence before database mutation. Run only:

```text
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges
```

Never use `--clean`, `--create`, `--if-exists`, trigger disabling, shell
execution or error continuation. Validate the restored catalog, constraints,
sequences, RLS/large-object classification and expected sidecar fingerprint
before returning success. On failure preserve the fresh sentinel database for
inspection; never drop or retry destructively.

## Test-first slices

1. Exact discovery/schema/mode configuration and clean-breaking rejection.
2. Private PGPASSFILE, fixed PostgreSQL 16 argv/env, real probe and truthful
   status.
3. Source fingerprint, read-only/denied-privilege proof and secret-safe errors.
4. Private streamed custom archive, sidecar, held-descriptor TOC/schema
   validation and unique publication.
5. Bounds, malformed TOC/archive, warnings, partial writes, replacement races,
   timeout, repeated cancellation and terminate-to-kill escalation.
6. Local-only authorization, exact destination allowlist/provenance, sentinel,
   freshness and same-target refusal.
7. Descriptor-bound single-transaction restore, exact post-restore catalog
   proof, rollback and audit outcomes through `RestoreService`.
8. Exact PostgreSQL 16.14 two-backup/two-independent-fresh-restore Docker drill
   with phase-separated relational, sequence, extension and large-object
   markers plus restart evidence.

Each slice starts RED at the public plugin/HTTP/RestoreService seam. Internal
tests are permitted only for bounded malicious inputs and process lifecycle
conditions that cannot be produced safely through the public interface.

## Exact local drill

Use synthetic credentials and two clean rounds on internal Docker networks
with no published ports, host networking, Docker socket, privileged workloads,
or production route. In each round:

1. Boot an exact PG16.14 source and create a dedicated denied-write backup role.
2. Seed phase A through supported SQL with typed parent/child rows, constraints,
   indexes, sequence state, extension state and one classified large object.
3. Back up through the real target/job/plugin path; validate artifact, sidecar,
   privileges and cleanup.
4. Mutate to phase B and take a distinct immutable artifact B; prove A stayed
   byte-identical and A/B evidence differs.
5. Precreate two separate `template0` destination databases with distinct
   owners and the exact sentinel. Restore A and B through `RestoreService`.
6. Query exact phase-specific rows, foreign keys, sequences, extension and
   large-object bytes; restart PostgreSQL and repeat the proof.
7. Exercise wrong major, write-capable/underprivileged/RLS sources, dump warning
   or failure, corrupt/replaced/wrong-plugin artifact, altered sidecar,
   same/nonfresh/unsentinelled/external destination, restore failure and
   cancellation.
8. Remove and audit every labeled/prefixed container, network, volume, runner,
   listener, temporary artifact and synthetic credential.

Run the complete sequence twice from clean state. Two backups with only one
restore are not completion evidence.

## Verification and completion

- focused generic PostgreSQL, HTTP, scheduler and RestoreService tests pass;
- two complete exact PG16.14 drill rounds pass;
- full backend and frontend gates pass;
- application mypy, changed-file Black/isort, SemVer, diff and secret scan pass;
- exact Docker image health/runtime tools and resource cleanup pass;
- Standards and Spec reviewers find no unresolved P0-P3 issue;
- compatibility, recovery, changelog, ledger and plan evidence are current;
- the milestone is committed independently and not pushed or deployed.

Mark `DONE (local)` only after every gate passes. Production remains
rollout-pending until the actual PostgreSQL runtime digest/version, network,
dedicated role/default grants, targets/jobs and a backup-only run are separately
approved and verified.

## STOP conditions

Stop rather than weaken the contract if exact PG16 clients cannot create and
restore the archive; source data needs ownership, write, superuser, RLS bypass,
unclassified large-object, server-file/program or broad network privilege;
catalog/TOC/provenance cannot be bound; restore is not demonstrably fresh,
isolated and transactional; cleanup cannot finish before return; a compatibility
fallback is requested without approval; or any production write, restore,
credential grant, network change, downtime or broader authority would be
required.
