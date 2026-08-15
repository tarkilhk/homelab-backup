# Immich Power Tools v0.22.0 backup and restore research

Research date: 2026-08-16  
Scope: the Immich Power Tools declaration in `homelab-infra`, the exact
upstream release/source and locally pulled vendor image, SQLite primary
documentation, and its dependency on the separately researched Immich v3.1.0
deployment. No production endpoint, host, API, database, or filesystem was
contacted. No production write, lifecycle operation, or restore was performed.

## Decision summary

Immich Power Tools has **unique authoritative state**. Its persisted
`/app/data/app.db` is not a cache and cannot be reconstructed from Immich or
Git declarations. It contains:

- per-user Power Tools settings, including workflow/import Immich API-key
  secrets;
- Power Tools-created Immich API-key secrets and their corresponding Immich key
  IDs;
- import jobs, source connection/auth JSON, every item and its progress/result;
- workflow definitions, node/edge graphs, schedules and webhook tokens;
- workflow run history; and
- the processed-asset ledger used to prevent repeat workflow processing.

Classification: **BACKUP REQUIRED / IMMICH-COMPOSITE CHILD / BLOCKED WITH
IMMICH**. Back up one validated standalone SQLite copy, but bind it to the same
quiescence transaction and snapshot-set identity as the Immich PostgreSQL and
media artifact. Do not claim that the native Immich artifact already covers
Power Tools, and do not restore the Power Tools database against an arbitrary
Immich point in time.

A separate always-independent Power Tools restore is unsound. Local rows refer
to Immich user, asset, and API-key IDs; local secrets correspond to records in
the Immich database; workflows and imports make API-side changes before
recording some local outcomes. Restoring only one side can produce invalid
credentials, missing references, duplicate/replayed actions, or misleading run
history.

The local implementation and paired two-backup/two-restore drill are buildable
on the dev VM. Production readiness inherits the existing Immich gates: verify
the actual Immich media root, approve one brief write outage, and approve narrow
lifecycle control for all Immich writers. No additional broad privilege is
justified. A raw Docker socket remains rejected.

## Version verification: the deployment is not 0.35.1

The initial lead `0.35.1` is incorrect for this service:

- `homelab-infra` at clean/current commit
  `eeed77a76fbc23db3da8470011535ad64cf0bc75` declares
  `ghcr.io/immich-power-tools/immich-power-tools:v0.22.0` at
  `docker.compose/misc/immich/immich.yaml:85-103`;
- the upstream Git tag inventory ends at `v0.22.0` on the research date and
  contains no `v0.35.1` tag; and
- the exact v0.22.0 OCI image identifies source revision
  [`46768eea7a9b672ae6236c5e8f8ec9e43833d46f`](https://github.com/immich-power-tools/immich-power-tools/commit/46768eea7a9b672ae6236c5e8f8ec9e43833d46f),
  matching the dereferenced `v0.22.0` tag.

Do not silently research or implement 0.35.1 behavior. If a later read-only
runtime check shows a different production image, stop and research that exact
identity before using this contract.

The mutable v0.22.0 tag resolved during this research to OCI repository digest
`sha256:7b3530e1d0dc7f5833a50520804e5e8614b322a8c467e91461db43cedf6b0725`;
the locally inspected Linux/amd64 image ID was
`sha256:6588b0f05c6d6dc4e16e581ae887abd0c2ecf5ba9d106e309237248f5a33d3d1`.
Its OCI labels report version v0.22.0, the exact revision above, and build time
2026-07-02. It uses Node 22.23.1 and runs as non-root
`uid=1001(nextjs) gid=65533(nogroup)`. Pin the repository digest for the local
drill and at any later production rollout; the tag alone cannot prove which
bytes a host pulled.

Primary identity sources:

- [upstream v0.22.0 source tree](https://github.com/immich-power-tools/immich-power-tools/tree/46768eea7a9b672ae6236c5e8f8ec9e43833d46f);
- [exact Dockerfile](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/Dockerfile); and
- [exact package manifest](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/package.json).

## Exact deployed topology

The infrastructure declaration gives Power Tools:

- one container named `immich_power_tools` with a 256 MiB memory limit;
- `/docker-apps/immich-power-tools/data:/app/data`;
- Immich API and direct Immich PostgreSQL connectivity on `immich_network`;
- an externally recovered Immich API key and PostgreSQL password;
- the same `immich` PostgreSQL database used by Immich v3.1.0;
- host port 8001 to container port 3000; and
- a health check against `/api/health`.

The Power Tools-specific environment declares `IMMICH_URL`,
`EXTERNAL_IMMICH_URL`, `IMMICH_API_KEY`, `DB_USERNAME`, `DB_PASSWORD`,
`DB_HOST`, `DB_PORT`, and `DB_DATABASE_NAME`. It does not declare
`APP_DB_PATH`, so exact source defaults to `/app/data/app.db`. No secret value
was read or copied. Evidence:
`docker.compose/misc/immich/immich_power_tools.env` and
[`src/db/index.ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/db/index.ts).

Infrastructure declares the bind as UID:GID `1001:65533`, mode `0775`, and
describes `app.db`, `app.db-wal`, and `app.db-shm` under `/app/data`
(`ansible/inventory/group_vars/docker_hosts.yaml:201-210`). Exact image
inspection independently confirmed the runtime UID:GID. The upstream
Dockerfile creates only `/app/data`; exact source uses only `app.db` there. No
media or import payload directory exists in this boundary.

## What is authoritative, derived, or external

| State | Disposition | Reason |
| --- | --- | --- |
| `/app/data/app.db` | Required, paired with Immich | Unique Power Tools configuration, credentials, workflows, jobs, and history |
| `app.db-wal`, `app.db-shm`, journal files | Do not archive | SQLite runtime companions; create a standalone DB via Backup API |
| Unknown extra `/app/data` members | STOP | Exact v0.22.0 source does not define them; research before classifying |
| Immich PostgreSQL | Do not duplicate in a standalone Power Tools artifact | Already authoritative in the paired Immich component; Power Tools reads it directly |
| Immich managed/external media | Do not duplicate here | Owned by the paired Immich artifact |
| Environment and secret-file values | External prerequisite | Declarative infrastructure/secret recovery owns URLs, DB access, and bootstrap API key |
| Power Tools image/static files | Recreate from pinned image | Immutable application code |
| In-memory cron registry and active worker set | Recreate/reconcile | Loaded from SQLite on startup; not an independent data source |

### Exact local SQLite schema

The v0.22.0 app database has nine domain tables:

| Table | Unique state |
| --- | --- |
| `settings` | arbitrary per-owner KV; currently workflow/import API-key secrets |
| `api_keys` | per-user/purpose secret, key name, and corresponding Immich API-key ID |
| `import_jobs` | source URL, auth/config JSON, options, status, counts and errors |
| `import_job_items` | source asset ID, item metadata, status, Immich asset ID and error |
| `workflows` | owner, name, schedule, enabled flag, webhook token and viewport |
| `workflow_nodes` | trigger/logic/action definitions, configuration and coordinates |
| `workflow_edges` | graph connections and branch handles |
| `workflow_runs` | trigger, status, result/error and timestamps |
| `workflow_processed_assets` | workflow/run/Immich-asset linkage used for lookback/deduplication |

The final table is especially important because it is the cross-system
execution ledger, not merely run presentation. The exact schemas and their
credential-bearing comments are primary evidence:
[`src/db/schema`](https://github.com/immich-power-tools/immich-power-tools/tree/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/db/schema).

Five bundled Drizzle migrations, `0000_vengeful_kylun` through
`0004_equal_iron_monger`, construct this schema. Startup automatically runs
pending migrations, resets interrupted imports from `processing` to `pending`,
and loads enabled scheduled workflows
([migration journal](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/db/migrations/meta/_journal.json),
[`instrumentation.ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/instrumentation.ts)).

This state is not reproducible from Git. Git carries migration and feature
definitions, not the user's rows. The app exposes one-workflow export/import,
but that omits all other workflows unless repeated manually, schedules' runtime
context, API-key pairings, per-user settings, import checkpoints, run history,
and processed assets
([workflow export route](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/pages/api/workflows/%5Bid%5D/export.ts)).
There is no v0.22.0 first-party whole-app backup/restore facility.

## Cross-service coupling and consistency

Power Tools does not own a second PostgreSQL database. It reads Immich's schema
directly for analysis and selection, and uses Immich APIs for mutations. The
upstream README requires both database and API connectivity and describes the
tool as an Immich client
([v0.22.0 README](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/README.md)).

The following exact write orders prove that independent snapshots are unsafe:

- share-key rotation deletes the prior key in Immich, creates a new Immich key,
  then upserts its secret and Immich key ID in local SQLite;
- workflow/import key generation creates an Immich key, then stores only its
  secret in local `settings`;
- an import uploads an asset through Immich, then records the returned Immich
  asset ID and increments counters in separate local statements; and
- a workflow executes one or more Immich actions, then inserts processed-asset
  rows and finally marks the run completed.

Sources:
[`api-keys/[purpose].ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/pages/api/settings/api-keys/%5Bpurpose%5D.ts),
[`generate-workflow-api-key.ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/pages/api/settings/generate-workflow-api-key.ts),
[`runner.ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/workers/import/runner.ts), and
[`workflow/engine.ts`](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/lib/workflow/engine.ts).

Even the local database has multi-statement logical updates without an explicit
transaction: saving a workflow graph deletes all edges, deletes all nodes,
inserts new nodes, inserts new edges, and optionally updates the viewport as
separate awaits
([workflow graph route](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/pages/api/workflows/%5Bid%5D/graph.ts)).
SQLite's Backup API guarantees a consistent SQLite snapshot; it cannot turn
these application operations or remote Immich writes into one transaction.

### Required quiescence transaction

Make Power Tools a member of the exact Immich quiescence boundary already
defined in `plans/research/immich.md`:

1. acquire one serialization lease and one new immutable `snapshot_set_id`;
2. preflight Power Tools for `import_jobs.status = 'processing'` and
   `workflow_runs.status = 'running'`; stop if either exists;
3. through the approved narrow coordinator, stop Power Tools before stopping
   Immich so it cannot initiate more API or database work;
4. stop the remaining declared third-party Immich writer and `immich-server`,
   then prove all writers are stopped while PostgreSQL remains available;
5. open `app.db` read-only and use SQLite's Backup API to produce a standalone
   copy while taking the paired Immich database/media snapshot;
6. validate all paired components, then restart Immich and prove readiness
   before restarting Power Tools;
7. perform authenticated Power Tools semantic readiness, not only `/api/health`;
   and
8. publish the whole snapshot set atomically only after every restart and probe
   succeeds.

After stopping, query the standalone copy again. Any `processing` import or
`running` workflow means a race occurred between preflight and stop, or the
source already contains an interrupted operation. Do not publish it. Restart
the original stack and surface an operator-visible reconciliation requirement.
Power Tools has no drain/maintenance API or signal handler in exact source, so
do not pretend a container stop completed an in-flight remote operation.

The existing Immich decision to quiesce all writers already includes Power
Tools. This note adds its own database to that boundary. It does not create a
second outage requirement.

## SQLite backup and validation contract

The exact source creates a local libSQL client for `app.db`, enables SQLite
foreign keys, and runs Drizzle migrations on startup. A newly created exact
image database locally reported `journal_mode=delete`; infrastructure also
documents WAL/SHM files, which may persist when an existing database was put
into WAL mode by an earlier release. Support either observed journal mode by
using SQLite APIs, not filesystem assumptions.

After Power Tools is stopped:

1. require `/app/data` and `app.db` to be non-symlinked, bounded, readable and
   on the approved source mount;
2. open the source SQLite database in read-only mode;
3. use the SQLite Backup API to create a private standalone `app.db` in staging;
4. never copy `-wal`, `-shm`, or rollback journal files into the artifact; and
5. validate before publication.

SQLite documents that a completed Backup API operation makes the destination a
consistent snapshot and is safer than an external file copy
([SQLite Backup API](https://www.sqlite.org/backup.html)). Stopping is still
required for application/Immich consistency, not merely database-page safety.

Validation must require:

- a non-empty regular SQLite file and no unexpected `/app/data` member;
- `PRAGMA integrity_check` returns exactly `ok`;
- `PRAGMA foreign_key_check` returns no row;
- the nine domain tables and Drizzle migration table exist;
- migration history matches all five exact v0.22.0 migrations and contains no
  later/unknown migration;
- statuses belong to their exact known enums and every stored JSON field parses
  as bounded JSON;
- every edge references nodes in its workflow and every run/processed asset
  references its workflow/run;
- no `processing` import and no `running` workflow exists; and
- bounded non-secret row counts and member hash can be recorded without
  exposing user IDs, asset IDs, URLs, graph data, errors, tokens, or secrets.

SQLite notes that `integrity_check` does not find foreign-key violations, so
both checks are mandatory
([SQLite PRAGMA documentation](https://www.sqlite.org/pragma.html#pragma_integrity_check)).

## Least privilege

The Power Tools component adds only:

- read-only access to `/docker-apps/immich-power-tools/data`;
- permission for the already-approved Immich lifecycle coordinator to report,
  stop, and start exactly `immich_power_tools`; and
- write access to the normal Homelab Backup staging/destination.

It does not require Power Tools' Immich API key, PostgreSQL password, direct
Immich database access, media mounts, arbitrary container exec, root, Portainer
administration, or a Docker socket merely to copy its SQLite state. Cross-store
semantic validation can run inside the already isolated paired restore drill.

`test()` is non-destructive: verify exact configured path, regular-file and
symlink safety, read-only SQLite open, schema/migrations, JSON bounds, and
coordinator status. It must not stop a service, trigger a workflow/import,
rotate a key, access production Immich, or write a journal beside the source.

The database contains plaintext API-key secrets, webhook tokens, source
authentication JSON, user/asset identifiers, and potentially sensitive source
URLs/errors. Make the artifact private (`0600`) and never log SQL values,
member bytes, identifiers, URLs, filenames, or credential-bearing JSON. The
external bootstrap `IMMICH_API_KEY` and PostgreSQL credentials stay in secret
recovery and are never copied into this component.

## Artifact and retention contract

Preferred: extend the Immich composite artifact with one exact component:

```text
addons/immich-power-tools/
├── manifest.json
└── app.db
```

The child manifest records:

- artifact/component contract version;
- the parent's immutable `snapshot_set_id` and manifest hash;
- application version, source revision, image digest, runtime UID:GID, and UTC
  quiescence interval;
- database engine, exact migration set, table inventory, bounded row counts;
- app database size/mode/SHA-256; and
- external restore prerequisite **names**, never values.

If the product cannot yet publish one composite archive, a linked child
artifact is acceptable only if both artifacts share the same signed/hash-bound
`snapshot_set_id`, cross-reference one another's hashes, publish as one catalog
transaction, and are retained/deleted as one unit. An independently scheduled
Power Tools artifact without its paired Immich point is not restorable under
this contract.

Use `create_backup_artifact()` or `write_backup_bytes()` and the standard
sidecar. Fixed members only; reject extra/duplicate paths, traversal, absolute
paths, links, devices, FIFOs, sockets, sparse/oversized files, compression
bombs, changing sources, and any failed cross-component validation. Publish no
artifact or sidecar if any writer fails to stop/restart or either component
fails validation/readiness.

## Restore contract

Declare `restore_capability = "partial"` and support only a paired,
create-only, local-disposable restore. The Power Tools database is complete for
this component, but the workflow intentionally refuses in-place or production
restore and requires the matching Immich snapshot.

Before mutation:

1. verify the parent and child sidecars, hashes, exact versions, member bounds,
   bidirectional snapshot-set links, and quiescence identity;
2. reject a child artifact presented without its exact Immich parent, including
   cross-pairing A's child with B's parent;
3. re-run every SQLite/schema/migration/JSON/status validation;
4. require a new/empty sentinel-labeled `/app/data` destination on the local dev
   restore harness; and
5. reject production paths, symlinks, mount escapes, overlap with source or
   artifacts, and unknown image/database versions.

Restore the matching Immich PostgreSQL/media state first using the Immich
contract. Materialize Power Tools `app.db` into a private staging directory,
set ownership for exact image UID:GID `1001:65533`, fsync, and atomically rename
the fresh tree. Do not restore WAL/SHM/journals. Boot the digest-pinned
v0.22.0 image only after paired Immich v3.1.0 is ready and its external
bootstrap credentials are available in the isolated test secret store.

The static `/api/health` handler returns healthy without checking SQLite,
Immich API, or PostgreSQL
([health route](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/pages/api/health.ts)).
It is necessary but insufficient. Require authenticated `/api/users/me`,
workflow/settings/import APIs, direct read-only SQLite semantics, and matching
Immich references/keys. Do not poll a pending import during initial validation:
the exact polling route automatically starts it.

Starting a later release or altering rows to make a mismatched pair boot is a
migration/recovery exercise, not this restore contract. Production restore is
forbidden.

## Exact local two-backup / two-fresh-restore drill

Run one fully disposable, internal-only paired stack on the dev VM:

- exact Immich v3.1.0/dependency digests from `plans/research/immich.md`;
- exact Power Tools
  `v0.22.0@sha256:7b3530e1d0dc7f5833a50520804e5e8614b322a8c467e91461db43cedf6b0725`;
- synthetic credentials and small generated media;
- unique temporary source, restore-A, restore-B and artifact roots;
- a Docker `internal: true` network with no production DNS, routes, mounts,
  names, ports, credentials, or data; and
- a local source-service stub for one supported import path if required.

### Source state A and backup A

1. Initialize Immich with a synthetic admin, second user, two assets, an album,
   metadata and API keys. Record non-secret marker hashes and IDs only in the
   private harness.
2. Start Power Tools with the synthetic bootstrap key and matching Immich DB.
   Authenticate through its supported API.
3. Through Power Tools APIs, create per-user settings, a share key, workflow
   and import keys, one disabled workflow with trigger/logic/action nodes and
   edges, a distinctive schedule/webhook marker, and one completed debug or
   safe local workflow run. Exercise one completed tiny import against an
   internal stub so job/item and returned Immich-asset linkage are real.
4. Require no `processing` import or `running` workflow. Invoke backup A through
   the real paired coordinator/plugin path: Power Tools stops first, all Immich
   writers stop, both components are captured/validated, then Immich and Power
   Tools restart in order.
5. Require one coherent snapshot-set identity, exact child member/hash/schema,
   private modes, successful authenticated readiness, and no synthetic secret
   or ID in manifests/logs/errors/metrics.

### Source state B and backup B

1. Mutate the disposable Immich source with a distinct asset/album marker.
2. Through Power Tools APIs, rename/update the workflow graph and schedule,
   rotate the share key, add a second workflow and safe completed run, and run a
   second completed import referencing the B-only Immich asset.
3. Take backup B through the identical path. Require different parent and child
   hashes, snapshot-set identity, timestamps and semantic inventories; A must
   contain no B-only marker.

### Fresh paired restore A

1. Restore Immich artifact A to fresh PostgreSQL/media A, then Power Tools child
   A to fresh data root A. Refuse any B child.
2. Boot exact Immich v3.1.0 and then exact Power Tools v0.22.0 on the internal
   network.
3. Prove SQLite integrity/schema/migrations, authenticated `/api/users/me`,
   exact A settings/workflow graph/schedule/webhook identity, completed run and
   import history, processed-asset ledger, and absence of every B marker.
4. In process memory only, verify restored local API-key secrets work against
   the matching restored Immich key records. Verify every local user/asset/key
   reference resolves. Never print secret or identifier values.

### Fresh paired restore B

Repeat into independent PostgreSQL/media/data root B and require the A baseline
plus every B delta, including rotated-key validity and B-only asset/import
linkage. The two destinations must share no volume, path, network name,
container, credentials, or mutable artifact staging.

Both independent paired restores must pass; database integrity or static health
alone is insufficient. Destroy all disposable containers, networks, volumes,
secrets and temporary roots in a bounded cleanup path.

### Mandatory negative and interruption cases

Prove locally that:

- cross-pairing parent A/child B or parent B/child A fails before extraction;
- missing/bad sidecar, parent link/hash, extra/duplicate member, traversal,
  absolute path, link, device, oversized DB, or non-fresh destination fails
  before mutation;
- corrupted/truncated SQLite, bad integrity/foreign-key result, unknown table or
  migration, malformed/bomb JSON, or unknown status fails closed;
- a `processing` import or `running` workflow before or after stop prevents
  publication and triggers bounded restart/reconciliation reporting;
- stop timeout, backup failure, cancellation, disk-full, validation failure,
  Immich restart failure, Power Tools restart failure, and semantic probe
  failure publish no successful snapshot set and do not leave a formerly
  running source stopped;
- restoring with missing/wrong external bootstrap key fails clearly without
  logging it or mutating the artifact; and
- every restore and lifecycle target is constrained to the disposable local
  harness in tests.

## STOP conditions

Stop before implementation, publication, or restore if:

- any source claims 0.35.1 or another identity instead of the verified
  v0.22.0 image/revision without new exact-version research;
- the actual runtime digest and effective `APP_DB_PATH` have not been verified
  read-only before production rollout;
- Power Tools state is proposed as “covered by Immich” without adding `app.db`
  to the same snapshot set, or as an independent restorable artifact;
- the paired Immich media-root, downtime, or narrow-lifecycle gates in
  `plans/research/immich.md` remain unresolved;
- Power Tools, `immich-server`, or another declared writer cannot be fully
  quiesced, or lifecycle access requires a raw Docker socket/general host
  control;
- any import is `processing`, workflow run is `running`, scheduled execution
  can begin during the boundary, or post-stop state indicates interruption;
- `app.db` is missing/unreadable/symlinked/changing, the plugin would write next
  to it, or `/app/data` contains unknown potentially authoritative files;
- database validation, exact migration/schema/JSON/status bounds, artifact
  secrecy, atomic snapshot-set publication, or ordered restart/readiness cannot
  be guaranteed;
- retention can delete the Immich parent and Power Tools child independently;
- the matching external bootstrap secret cannot be recovered or corresponding
  local-to-Immich references/keys cannot be validated; or
- a restore is production, in-place, cross-version, cross-paired, non-empty,
  unlabeled, path-overlapping, or connected to any production network, DNS,
  secret, database, API, or storage.

## Roadmap consequence

Do not create a free-standing “Power Tools is backed up” checkbox or schedule.
Amend the future Immich composite plan so `app.db` is a required child in every
coherent snapshot set, with paired retention and restore. This closes the gap
already called out in `plans/research/immich.md`, which intentionally excluded
third-party `/app/data` until separate research established its value.

No new user decision is needed beyond the existing Immich production gates.
If the user declines coordinated Immich downtime/lifecycle control, this
component must remain honestly blocked rather than fall back to an independently
timed SQLite backup and call it recoverable.

## Primary sources

- [Immich Power Tools v0.22.0 source](https://github.com/immich-power-tools/immich-power-tools/tree/46768eea7a9b672ae6236c5e8f8ec9e43833d46f)
- [v0.22.0 README and deployment contract](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/README.md)
- [local SQLite setup and migrations](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/db/index.ts)
- [exact local schema](https://github.com/immich-power-tools/immich-power-tools/tree/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/db/schema)
- [startup recovery and scheduler loading](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/instrumentation.ts)
- [import worker state transitions](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/workers/import/runner.ts)
- [workflow engine](https://github.com/immich-power-tools/immich-power-tools/blob/46768eea7a9b672ae6236c5e8f8ec9e43833d46f/src/lib/workflow/engine.ts)
- [Immich official backup and restore guidance](https://docs.immich.app/administration/backup-and-restore/)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [SQLite integrity and foreign-key PRAGMAs](https://www.sqlite.org/pragma.html#pragma_integrity_check)
