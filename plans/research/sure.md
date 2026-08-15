# Sure v0.7.1-hotfix.1 backup and restore research

Research date: 2026-08-15
Scope: the Sure deployment declared in `homelab-infra`, the exact vendor image
and source revision it pins, Rails 7.2.3.1 Active Storage, and PostgreSQL 16
documentation. No production endpoint or host was contacted. No production
write or restore was performed.

## Decision summary

Sure is a **composite, quiescence-required workload**. Its authoritative
recoverable state is:

1. the complete `sure_production` PostgreSQL database; and
2. the complete local Rails Active Storage tree mounted at `/rails/storage`.

The deployment's original `SECRET_KEY_BASE` is an external restore
prerequisite. In this self-hosted configuration, the exact Sure release derives
all three Active Record encryption keys deterministically from it. The secret
must remain in the infrastructure secret-recovery process and must never be
copied into a backup artifact.

Redis is queue, cache, rate-limit, Action Cable, and scheduler state. It does
not contain the authoritative financial records or attachment bytes. Restore it
empty, recreate the scheduler from database settings, and explicitly reconcile
or re-run interrupted imports and account syncs.

There is **no provably consistent online PostgreSQL-plus-filesystem boundary**.
The exact Rails version commits an Active Storage blob and attachment to the
database before its after-commit callback writes the file, while purge removes
the database record before deleting the file. `pg_dump` can consistently
capture PostgreSQL under concurrent writes, but it cannot coordinate that
snapshot with `/rails/storage`.

Classification: **CONDITIONAL / STOP before plugin implementation**. A safe
plugin requires a short scheduled window that quiesces both `sure-web` and
`sure-worker`, plus a narrow Sure-only lifecycle mechanism. The user must
explicitly accept that downtime and choose the mechanism. Do not give Homelab
Backup the Docker socket or general host/Portainer administration. The core
artifact and create-only restore logic can be prototyped locally, but a
production-capable plugin contract is not fully buildable without that decision.

## Exact deployed topology and source identity

The deployment declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/misc/sure/sure.yaml`:

| Component | Exact declaration | State and backup relevance |
| --- | --- | --- |
| Web | `ghcr.io/we-promise/sure:stable@sha256:b5248f97fa40d35a017ebb9a6c5876c19f0acc04258c2a0b614dbf343eb17905` | Rails server; writes PostgreSQL and shared `/rails/storage` |
| Worker | same immutable Sure digest | Sidekiq; writes PostgreSQL and shared `/rails/storage` |
| Database | `postgres:16.14` | authoritative relational state at `/var/lib/postgresql/data` |
| Redis | `redis:8.10-alpine` | operational queue/cache state in a named volume |

Evidence: the web image, port and storage bind are at
`docker.compose/misc/sure/sure.yaml:7-40`; the worker and shared bind are at
`:41-62`; PostgreSQL is at `:63-83`; Redis is at `:84-100`. The non-secret
environment declaration selects database `sure_production`, local Active
Storage by omission of an override, and Redis database 1
(`docker.compose/misc/sure/sure.env:1-16`). No secret value was read or copied
into this note.

Local, network-free inspection of the pinned vendor image reports:

- OCI version `v0.7.1-hotfix.1`;
- source revision
  [`2f50e7b0b5419e860affd55c1a155e3fb45b8581`](https://github.com/we-promise/sure/commit/2f50e7b0b5419e860affd55c1a155e3fb45b8581);
- Rails and Active Storage 7.2.3.1;
- Ruby 3.4.7; and
- runtime UID:GID `1000:1000`.

The OCI labels are the authoritative mapping between the deployed digest and
source. Current `main` is not the plugin contract.

For a reproducible Linux/amd64 development drill, the declared mutable
dependency tags resolved on 2026-08-15 to:

- `postgres:16.14@sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b`;
- `redis:8.10-alpine@sha256:978f0e01593e65eed801f2402944efcd936d43b5027e4908a7897baf88ed6241`.

These registry resolutions make the local drill repeatable. They do not prove
which historical bytes a production host previously pulled from a mutable tag;
the later infrastructure change should pin those digests.

The deployed Homelab Backup backend currently has neither the Sure network nor
the Sure storage bind. It joins only its own network and the external Standard
Notes network and mounts only backups, its own database, and Jellyfin backup
state (`docker.compose/system/homelab-backup/homelab-backup.yaml:1-26`). It also
has no narrow Sure lifecycle control. Those are deployment gates, not reasons
to weaken the backup contract.

## Authoritative and reproducible state

### PostgreSQL: include the complete application database

Dump the entire configured database rather than maintaining a table allowlist.
The exact schema contains users and authentication state, encrypted identity
and provider fields, API keys, families, accounts, balances, transactions,
holdings, imports, settings, sync state, Active Storage metadata, and migration
history. A whole-database dump also captures future vendor tables by default.

Use the PostgreSQL 16.14 client and custom archive format:

```text
pg_dump --format=custom --no-owner --no-privileges \
  --file=database.dump sure_production
```

Supply the password through a mode-0600 temporary `PGPASSFILE`, never command
arguments or logs. PostgreSQL documents that `pg_dump` makes a consistent
backup while the database is concurrently used, and that a custom archive is
restored with `pg_restore`
([PostgreSQL 16 `pg_dump`](https://www.postgresql.org/docs/16/app-pgdump.html)).
Quiescing Sure removes application DDL and cross-store races; the PostgreSQL
server itself remains online.

Do not copy the live `/var/lib/postgresql/data` bind. PostgreSQL documents SQL
dumps, filesystem backups, and continuous archiving as distinct techniques;
an arbitrary live directory walk is not a logical database backup
([PostgreSQL 16 backup chapter](https://www.postgresql.org/docs/16/backup.html)).

### Active Storage: include the complete local tree

The exact release selects `ACTIVE_STORAGE_SERVICE=local` by default in
[production configuration](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/config/environments/production.rb#L33-L37),
and its
[`local` service root](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/config/storage.yml#L1-L4)
is `Rails.root/storage`. The compose bind therefore makes
`/docker-apps/sure/storage` authoritative.

This is not just cosmetic cache data. The exact source attaches files to
transactions, family documents, account statements, imports and exports, user
profiles, accounts, and numerous provider items. Examples are
[`Transaction`](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/app/models/transaction.rb),
[`FamilyDocument`](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/app/models/family_document.rb),
and
[`AccountStatement`](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/app/models/account_statement.rb).
Rails describes Active Storage as the association between Active Record
objects and uploaded files and documents the local Disk service
([Rails 7.2 Active Storage guide](https://guides.rubyonrails.org/v7.2/active_storage_overview.html)).

Archive every regular file and directory under the storage root, including
unreferenced completed blobs and generated variants. Reject symlinks, hard
links, devices, FIFOs, sockets, absolute paths, traversal, and mount escapes.
Record each relative path, size, mode, and SHA-256 in a versioned internal
manifest. Restoring variants is not strictly necessary for reconstruction, but
including the complete small tree avoids silently misclassifying a future
authoritative subdirectory.

Additionally query all `active_storage_blobs` rows after quiescence and require:

- `service_name = 'local'` for every blob;
- the deterministic Disk-service path for every key exists as one regular file;
- its size equals `byte_size`; and
- its base64 MD5 equals the stored `checksum` where a checksum is present.

Fail on missing, short, changing, or mismatched blob files. Do not publish a
partial artifact. Fail and classify the target as unsupported if any blob uses
S3, GCS, Cloudflare R2, or another service; those bytes are outside this
filesystem-mode boundary.

### Restore prerequisites outside the artifact

The exact
[Active Record encryption initializer](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/config/initializers/active_record_encryption.rb#L1-L44)
uses explicit encryption variables when all three exist. Otherwise, in
self-hosted mode, it derives the primary key, deterministic key, and derivation
salt from `SECRET_KEY_BASE`. The deployed environment provides only the latter
path. Restoring the database with a different secret would make encrypted
email, names, MFA state, API/provider credentials, device identifiers, and raw
provider payloads unreadable.

External recovery must therefore preserve, without embedding in the artifact:

- the original `SECRET_KEY_BASE` (or the exact explicit encryption-key trio if
  the deployment later migrates to it);
- database and Redis connection configuration;
- `APP_DOMAIN`, WebAuthn relying-party ID and allowed origins; and
- the immutable container identities and Compose/network declarations.

The sidecar may identify required variable **names**, image digests, schema
version, and application version, but never values. The database and storage
contain sensitive personal financial data and credentials; publish the outer
artifact as a private regular file (`0600`).

### Redis: intentionally recreate empty

The exact production config uses Redis as the Rails cache and Action Cable
backend, and Sidekiq uses the same configured URL. Sure's own user-facing text
describes Redis as powering background jobs such as account sync and import
processing. Its auto-sync cron definition is recreated from database-backed
settings whenever Sidekiq starts
([`AutoSyncScheduler`](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/app/services/auto_sync_scheduler.rb),
[`sidekiq.rb`](https://github.com/we-promise/sure/blob/2f50e7b0b5419e860affd55c1a155e3fb45b8581/config/initializers/sidekiq.rb)).

Do not restore old caches, rate-limit counters, live Action Cable state, or
serialized jobs into a new runtime. A fresh Redis intentionally loses sessions,
queued/retry jobs, and unfinished work. The drill must prove that scheduled
sync is recreated and must identify interrupted imports/syncs from PostgreSQL
for an operator-visible re-run. If later evidence shows an irrecoverable domain
transition exists only in Redis, stop and expand the boundary rather than
silently discard it.

## Why an online composite backup is not sound

PostgreSQL's consistent snapshot covers only PostgreSQL. The database and Disk
service do not share a transaction.

In the exact Rails 7.2.3.1 dependency:

- an attachment is saved in the database during `after_save`, but file upload
  runs only in an `after_commit` callback
  ([Active Storage model callbacks](https://github.com/rails/rails/blob/v7.2.3.1/activestorage/lib/active_storage/attached/model.rb#L140-L142));
- Disk service writes directly to the final key path with `IO.copy_stream`, so
  a concurrent reader can observe a partial file
  ([Disk service](https://github.com/rails/rails/blob/v7.2.3.1/activestorage/lib/active_storage/service/disk_service.rb#L21-L26)); and
- purge destroys the blob record before deleting its file
  ([Active Storage blob](https://github.com/rails/rails/blob/v7.2.3.1/activestorage/app/models/active_storage/blob.rb#L325-L345)).

Consequently:

- DB-first can capture a committed blob reference before its file upload
  completes, or a blob whose file is deleted before the filesystem walk.
- Files-first can omit a new file whose committed DB reference appears in the
  later dump.
- Before/after directory manifests plus retries are not a proof: an upload or
  purge can cross both observation boundaries, and an A -> B -> A sequence can
  hide an intermediate state.
- A database read lock cannot coordinate the after-commit filesystem write.

The only declared safe boundary is to quiesce **both** writers. Stopping only
the web service is insufficient because Sidekiq creates exports, processes
imports, purges attachments, and updates financial state.

## Selected safe backup contract

Subject to explicit user approval of the STOP condition, the backup flow is:

1. Acquire a globally serialized Sure backup lease.
2. Reject new Sure ingress. Gracefully drain active web requests and uploads.
3. Quiet `sure-worker`, wait for busy jobs to reach zero within a bounded
   timeout, then stop the worker. Stop the web process. If either process fails
   to drain or stop, abort before artifact creation.
4. Prove both writers are stopped. Leave `sure-db` and `sure-redis` running.
5. Query a sorted Active Storage blob manifest, run the complete PostgreSQL
   custom-format dump, archive `/rails/storage`, and query the manifest again.
6. Require the two database manifests to be byte-identical and validate every
   local blob against path, size, and checksum. Validate the PostgreSQL archive
   with `pg_restore --list`.
7. Package and privately fsync `manifest.json`, `database.dump`, and
   `storage.tar`; publish the single non-empty artifact and sidecar atomically
   through `create_backup_artifact()`.
8. Resume web, require `/up`, resume the worker, and confirm the scheduled sync
   definition exists. Always attempt resume in a `finally` path. A failure to
   resume or recover health records a failed/partial attempt, not success, even
   if a valid artifact was produced.

The internal manifest records format version, UTC timestamp, Sure image digest
and source revision, Rails/Active Storage version, PostgreSQL server/client
versions, database name, schema migration version, dump size/SHA-256, storage
file count/bytes/archive hash, Active Storage blob count, and explicit
quiescence evidence. It contains no environment dump, credentials, cookies,
tokens, decrypted fields, filenames from user content where avoidable, or file
contents.

There is no online fallback. If quiescence cannot be proven, fail the run.

## Least-privilege production shape

### Database

Create a dedicated login restricted to the Sure database and deployment
network. It needs `CONNECT` on `sure_production`, `USAGE` on application
schemas, and `SELECT` on all current tables and sequences. The application
owner must also set equivalent default privileges for future objects.
PostgreSQL documents privileges per object and the implications of the broad
`pg_read_all_data` predefined role
([privileges](https://www.postgresql.org/docs/16/ddl-priv.html),
[predefined roles](https://www.postgresql.org/docs/16/predefined-roles.html)).
Use explicit database/schema grants rather than cluster-wide
`pg_read_all_data`.

Prove the reduced role against the exact PostgreSQL 16.14 server and client.
It must not have superuser, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, write, DDL,
replication, or another database's privileges. Creating the role and secret is
a one-time operator-approved production mutation, never plugin behavior.

### Storage and network

The backend needs only:

- a dedicated read-only bind of `/docker-apps/sure/storage` at an unambiguous
  source path;
- network access to `sure-db:5432` with the dedicated dump identity;
- write access to `/backups`; and
- a narrow, authenticated Sure-only quiesce/resume interface whose state can be
  verified.

Do not grant the Docker socket, a host root mount, Portainer administrator
token, SSH shell, PostgreSQL application/root credentials, writable Sure
storage, Sure user/API credentials, or production restore privileges. A small
host-side allowlisted coordinator or equivalent orchestrator may own lifecycle
authority, but the user must choose and approve it. Its only accepted actions
are drain/stop/status/start for `sure-web` and `sure-worker`, with an auditable
state machine and bounded timeouts.

The plugin's `test()` remains non-destructive: verify config shape, exact
PostgreSQL connectivity/version, read-only access, local storage identity, and
the coordinator's read-only status endpoint. It must not quiesce the service or
trigger a backup.

## Create-only isolated restore contract

Declare `restore_capability = "partial"`. The plugin restores and validates the
composite state, but it does not overwrite an existing deployment or manage an
application boot. The integration harness performs the exact-image functional
proof. Production restore is forbidden.

Restore must:

1. Snapshot the artifact to a private immutable file, validate the outer
   sidecar size/hash, and validate every inner member before destination
   mutation. Enforce an exact manifest schema, bounded member/total sizes,
   safe relative paths, no duplicate names, and no links or special files.
2. Require a newly created PostgreSQL database name and a new empty storage
   directory carrying a fixed restore sentinel. Refuse symlinks, existing
   tables/files, `/backups`, `/rails/storage`, PostgreSQL data directories,
   source paths, and artifact/destination overlap.
3. Create the disposable database from `template0`, then run
   `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges`
   into it. Never use `--clean`, `--create`, or a production connection.
   PostgreSQL documents restoring a custom archive into a separately created
   empty database and recommends `template0`
   ([PostgreSQL 16 `pg_restore`](https://www.postgresql.org/docs/16/app-pgrestore.html)).
4. Extract storage privately on the destination filesystem, verify every
   internal path/hash and every restored Active Storage blob size/checksum,
   fsync, then publish the directory create-only.
5. Run database structural checks, compare table/row and blob counts to the
   manifest, and run `ANALYZE`; `pg_dump` does not preserve optimizer
   statistics.
6. On failure, drop only the database created by this invocation and remove
   only the sentinel-marked directory it created. Never clean, merge, or
   overwrite a pre-existing destination.
7. Return `partial` with explicit external requirements: original encryption
   secret/configuration, fresh Redis, exact-version Sure boot, scheduler/job
   reconciliation, and authenticated functional verification.

Restore uses a locally generated disposable database owner. No production
credential or network is available to the restore process.

## Exact disposable two-backup/two-restore drill

The implementation gate requires one automated integration test that performs
all of the following on the development Docker daemon only.

### Fixed inputs

- Sure web and worker:
  `ghcr.io/we-promise/sure:stable@sha256:b5248f97fa40d35a017ebb9a6c5876c19f0acc04258c2a0b614dbf343eb17905`
- PostgreSQL:
  `postgres:16.14@sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b`
- Redis:
  `redis:8.10-alpine@sha256:978f0e01593e65eed801f2402944efcd936d43b5027e4908a7897baf88ed6241`
- unique test networks, volumes, database names, storage paths, credentials,
  and `SECRET_KEY_BASE` generated for this test run;
- no production DNS, routes, credentials, mounts, or reused container names.

### Source state A and backup A

1. Start source PostgreSQL and Redis, then the exact Sure web and worker images.
   Let the exact entrypoint run `db:prepare` and require `/up` plus a real
   database query.
2. Load the upstream deterministic demo data locally, create a uniquely marked
   synthetic family/account/transaction, and attach at least two deterministic
   files through Sure/Active Storage: one transaction attachment and one
   profile, statement, or import file. Include a value encrypted by Active
   Record encryption and record only expected hashes/identifiers in test code.
3. Exercise one background job and wait for its persisted result. Quiet and
   stop both local writers through the same coordinator contract the plugin
   expects.
4. Invoke the real plugin backup path to create **artifact A** and its sidecar.
   Validate dump structure, exact internal member set, private modes, hashes,
   blob/file correspondence, quiescence evidence, and absence of secret values.
5. Resume the source and prove `/up`, decryption, attachment reads, and worker
   scheduling still function.

### Source state B and backup B

6. Mutate the running source through supported application behavior: change
   the financial marker, delete one A attachment, add a different deterministic
   attachment, and complete another background mutation. Require the expected
   A -> B delta and no unfinished job before quiescence.
7. Repeat the identical quiesce/backup/resume path to create **artifact B**.
   Require a different outer digest, later timestamp, expected blob/storage
   delta, and a healthy resumed source.

### Independent restores A and B

8. Restore A into fresh `sure_restore_a` PostgreSQL and storage destinations;
   restore B into different fresh `sure_restore_b` destinations. Use the real
   restore path. Start neither application until import, extraction, and all
   structural/hash checks pass.
9. Start two isolated exact-digest Sure stacks with fresh empty Redis and the
   same **local test** encryption secret used for the source. Do not let either
   stack see the source or the other restore.
10. For A, prove the A financial marker and both A attachments exist and their
    bytes hash correctly, while B-only state is absent. For B, prove the changed
    marker and new attachment exist and the deleted A attachment is absent.
    Query every restored Active Storage blob through the exact application and
    require integrity verification, not just filesystem existence.
11. Require a real authenticated page/API read, successful decryption of the
    synthetic encrypted field, `/up`, database connectivity, fresh Redis, and
    recreation of the auto-sync schedule in each restore. Container health
    alone is insufficient because `/up` only proves Rails booted.
12. Destroy only the uniquely named disposable resources after assertions.

The same test suite must also reject a corrupted dump, changed storage member,
unsafe archive path/link, missing sidecar, wrong encryption secret at the
functional-boot step, non-empty destination, and cancellation/timeout. No
failure path may publish an artifact, leave a writer stopped, or remove a
pre-existing destination.

## STOP conditions and implementation gate

Stop and do not report backup success when any of these applies:

- the user has not explicitly accepted a short Sure maintenance window;
- no narrow Sure-only drain/stop/status/start mechanism has been selected;
- either `sure-web` or `sure-worker` cannot be proven quiesced;
- the database is not PostgreSQL 16-compatible with the pinned client, a dump
  role lacks complete read access, or `pg_restore --list` rejects the archive;
- the storage root is missing, writable by the plugin, contains unsafe file
  types/paths, changes while quiesced, or disagrees with Active Storage rows;
- any blob uses a non-local service;
- the original encryption secret/configuration is not recoverable outside the
  artifact;
- resume or application/worker readiness fails;
- restore does not target a fresh isolated local database and directory; or
- any production restore, production overwrite, Docker socket, general admin
  credential, or broad host privilege would be required.

Before coding, ask one compound decision: **may Sure be unavailable for a
short scheduled window during each backup, and which narrow Sure-only
quiesce/resume mechanism should Homelab Backup rely on?** Until answered, leave
Sure classified as conditional. Do not substitute a database-only dump or
best-effort live storage copy and label it reliable recovery.
