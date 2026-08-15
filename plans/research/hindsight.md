# Hindsight 0.8.6 backup and restore research

Research date: 2026-08-15

Scope: the Hindsight deployment declared in `homelab-infra`, Hindsight's exact
v0.8.6 source, and official Hindsight and PostgreSQL documentation. No
production endpoint or host was contacted. No production state was read or
changed, and no production restore was attempted.

## Decision summary

Hindsight is locally buildable without further user approval. In the declared
deployment, its authoritative application state, including native uploaded-file
bytes, is one PostgreSQL database. An online logical dump therefore has one
transactional consistency boundary and needs no Hindsight downtime.

The selected product contract is a Hindsight-specific PostgreSQL 18
custom-format logical dump, not a raw database-volume copy and not a copy of
Hindsight's Codex OAuth directory. Restore is create-only into a new empty,
sentinel-marked PostgreSQL database on the development VM. The plugin should
declare `restore_capability = "partial"`: it can atomically restore and inspect
the database, while the external deployment configuration and Codex credential
must be re-provisioned before the original LLM-backed behavior can be proven.
The exact Hindsight image must then boot against each disposable restore and
prove recovered API content in the local drill.

Production activation remains gated on explicit user approval for two narrow
infrastructure changes: attach Homelab Backup to the Hindsight database network
and provision a dedicated read-only database identity. Neither change is
needed to implement and prove the plugin locally. No Docker socket, host mount,
Hindsight administrator/API credential, service downtime, or production restore
is justified.

The reviewed baseline (`homelab-backup` commit
`0fa49f691c94533458f1e4238b895ee6c442dd88`) shipped PostgreSQL 16 client
tools while Hindsight runs PostgreSQL 18.
PostgreSQL explicitly refuses to let an older-major `pg_dump` dump a newer
server. Implementation therefore moved the backend client stage to PostgreSQL
18 and added a repository guard before the plugin was enabled
([PostgreSQL `pg_dump` notes](https://www.postgresql.org/docs/current/app-pgdump.html#APP-PGDUMP-NOTES)).

## Exact declared deployment

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/misc/hindsight-db/hindsight-db.yaml`:

| Component | Declared identity | Durable connectivity/state |
| --- | --- | --- |
| Hindsight API | `ghcr.io/vectorize-io/hindsight:0.8.6` | Private Hindsight DB network, ports 8888/9999 published, only `/docker-apps/hindsight/codex` mounted at `/home/hindsight/.codex` |
| Migration job | the same Hindsight 0.8.6 image | Runs `hindsight-admin run-db-migration` once against the same database |
| Database | `pgvector/pgvector:pg18-trixie` | Internal-only database network; `/docker-apps/hindsight-db` at `/var/lib/postgresql` |

Evidence: the DB image, data bind and private network are at
`docker.compose/misc/hindsight-db/hindsight-db.yaml:1-26`; the migration and
application images, command and OAuth-directory mounts are at `:28-67`.
`hindsight.env` declares the database URL, pgvector backend, fixed worker ID,
and OpenAI Codex LLM roles, but no custom database schema, tenant extension or
external file-storage backend. No secret values are reproduced here.

The tag is exact upstream release v0.8.6 at commit
[`08995e3013858e705fb4ca27c0ade3a286ef4750`](https://github.com/vectorize-io/hindsight/commit/08995e3013858e705fb4ca27c0ade3a286ef4750)
([release](https://github.com/vectorize-io/hindsight/releases/tag/v0.8.6)). On
2026-08-15, public registry metadata resolved its Linux/amd64 manifest to
`sha256:47eba343fe1cc0feb30839fa9bae4d1bb592676a2e7a7c3b8c80689ac93fbf8c`.
The pgvector tag resolved for Linux/amd64 to
`sha256:ff8da7b0714e5efa413d77f43e24d93064dd66469d418d12608c1bbc91fcf045`.
These public-registry results provide reproducible drill inputs; they do not
claim to inspect the production daemon's local image ID. The later
infrastructure change should pin the appropriate multi-architecture digest.

The existing `homelab-infra` Hindsight prose still mentions 0.7.2 in one
place, but the executable compose declaration is 0.8.6. Treat the compose file
as authoritative and correct the stale prose in a later infrastructure PR.

## Local exact-image evidence

Two final clean development-VM drills against the pinned images passed in
114.02 and 101.12 seconds. They observed PostgreSQL server/client 18.6, pgvector 0.8.6,
`pg_trgm` 1.6, and Alembic head `c7d1e9a4b3f2`. The exact source archive
contains 21 application tables with data, four functions, two sequences, one
materialized view, 20 primary/unique constraints, 62 fixed indexes, 17 foreign
keys, and both required extensions. Hindsight also creates one complete trio
of per-bank vector indexes whose 16-hex suffix is intentionally generated; the
validator permits any number of complete `expr`/`obsv`/`worl` trios while
fingerprinting every other object exactly.

A fresh sentinel destination with only preinstalled pgvector produced exactly
the vector extension and its comment in its schema TOC. Both drills produced
different private artifacts with valid sidecars, restored them transactionally
into two fresh databases, and proved phase-specific retained/curated/deleted
memories, documents, native uploads/bytes, directives, webhook-secret recovery
with API redaction, and a supported write overlapping backup A. Exact-image
boots and restarts proved persistence. Real underprivileged/RLS sources,
corrupt provenance, a nonempty destination, and an injected tail SQL failure
all failed closed; the latter rolled the entire restore transaction back. No
production system was contacted, and every disposable Docker resource was
removed after each run.

Exact Hindsight 0.8.6 exposes native file upload but no HTTP file-download route
in its OpenAPI contract; the exact image returns 404 for the candidate route.
The drill therefore proves recovered file-backed documents through supported
APIs and validates native bytes at the authoritative PostgreSQL boundary. It
explicitly refuses to describe the direct database check as an API download.

## Authoritative state and exclusions

### Include: the complete Hindsight PostgreSQL database

Hindsight's official storage design puts vector search, full-text search,
relational state, JSON documents and graph data in PostgreSQL and calls out a
single backup/restore strategy and ACID transaction boundary
([official storage documentation](https://hindsight.vectorize.io/developer/storage)).

The complete database includes, at minimum:

- banks and their configuration;
- documents, chunks, entities and relationships;
- memory units, invalidated units, links and co-occurrences;
- observations/history, mental models/history and directives;
- async operations, audit and LLM-request history, maintenance queues;
- webhooks and their signing secrets; and
- the `file_storage` table.

That boundary agrees with v0.8.6's own backup table list and its upstream test
that fails when a persistent table is omitted
([exact admin source](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/admin/cli.py#L38-L74),
[exact coverage test](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/tests/test_admin_backup_restore.py#L66-L106)).

The deployment does not override `HINDSIGHT_API_FILE_STORAGE_TYPE`. Exact
v0.8.6 defaults it to `native`, which stores file bytes as PostgreSQL `BYTEA`;
the first-party implementation explicitly notes that such files are included
in `pg_dump`
([exact configuration](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/config.py#L1113-L1125),
[exact storage implementation](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/engine/storage/postgresql.py#L14-L32),
[official configuration](https://hindsight.vectorize.io/developer/configuration#file-storage)).
There is consequently no second live filesystem/object-store state to align
with the database in this deployment.

### Exclude: Codex OAuth and deployment secrets

`/docker-apps/hindsight/codex/auth.json` is an external LLM credential, not
Hindsight domain data. Hindsight reads and refreshes it from
`/home/hindsight/.codex`; the database dump neither contains nor needs it to
restore stored memories. Exclude the entire `.codex` directory from the plugin
artifact. Copying it would turn a database backup into a bearer-credential
backup and would couple recovery to a refresh token that may already have been
rotated or revoked.

Re-provision these independently before a recovered production-like instance
can resume its original behavior:

- the Codex OAuth credential;
- the database password and connection URL;
- Hindsight worker ID, provider/model and concurrency environment;
- container image identities, network, ports and reverse proxy; and
- any future tenant/API-authentication or external-storage credential.

The app can still be booted for a credential-free local restore drill with
`HINDSIGHT_API_LLM_PROVIDER=none`; exact v0.8.6 then uses chunks mode, leaves
recall available, and disables LLM-dependent reflect/consolidation
([exact v0.8 documentation](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-docs/versioned_docs/version-0.8/developer/configuration.md#L351-L356),
[exact enforcement](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/config.py#L2479-L2486)).

### Encryption and key prerequisites

There is no Hindsight backup-encryption key to preserve. Exact v0.8.6's native
backup writes raw binary table streams into an ordinary deflated ZIP; it does
not encrypt them
([exact backup implementation](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/admin/cli.py#L179-L236)).
A PostgreSQL custom archive is likewise not encrypted by `pg_dump`.

The artifact is therefore **secret-bearing plaintext**. Memory text, original
document/chunk text and uploaded-file bytes can be sensitive, and Hindsight's
schema stores webhook signing secrets as ordinary text
([exact migration](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/alembic/versions/e4f5a6b7c8d9_add_webhooks_tables.py#L28-L47)).
The plugin must publish it mode 0600, never log SQL/TOC content, row values,
URLs, secrets or connection strings, and rely on the separately managed backup
storage/transport protections. In-application artifact encryption remains
outside the agreed Homelab Backup product scope; if the destination cannot
safely hold this plaintext artifact, stop rather than claim coverage.

## Consistency boundary and selected backup contract

PostgreSQL documents that `pg_dump` makes a consistent export while the
database is used concurrently and does not block readers or writers
([PostgreSQL 18 `pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html#APP-PGDUMP-DESCRIPTION)).
Because native uploaded bytes live in the same database, this one snapshot is
also the file-state snapshot. Hindsight does not need to be stopped or paused.
Raw copying `/docker-apps/hindsight-db` while PostgreSQL is live is forbidden.

Hindsight also ships a supported PostgreSQL-only admin backup/restore CLI. Its
backup uses one `REPEATABLE READ` transaction across every persistent table,
and its restore validates column types before truncation then imports inside
one transaction
([official admin CLI](https://hindsight.vectorize.io/developer/admin-cli),
[exact backup source](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/admin/cli.py#L179-L236),
[exact restore source](https://github.com/vectorize-io/hindsight/blob/08995e3013858e705fb4ca27c0ade3a286ef4750/hindsight-api-slim/hindsight_api/admin/cli.py#L241-L304)).
That confirms the vendor's logical-backup boundary.

The native CLI is not the selected runtime mechanism for Homelab Backup. It is
not exposed over Hindsight's HTTP API; upstream recommends running it in the API
container, and embedding it would either add Hindsight's very large application
dependency tree or require broad Docker/container-exec control. Neither is
least privilege. A full custom-format `pg_dump` uses the PostgreSQL 18 tools the
backend already conceptually supports, captures both schema and data for clean
disaster recovery, and needs only a narrow read-only DB connection.

The plugin backup should:

1. Validate fixed, typed `host`, `port`, `database`, `user`, `password` and
   expected `server_major=18`; reject URL-form connection strings so errors and
   process arguments cannot echo embedded credentials.
2. `test()` with PostgreSQL 18 `psql -X`: require `SELECT 1`, server major 18,
   database identity, `vector` extension, exact Hindsight Alembic revision and
   the exact required table set. It must be read-only and secret-safe.
3. Put the password in a private temporary `PGPASSFILE` (0600), not an argument
   or `PGPASSWORD`. PostgreSQL warns that environment passwords may be visible
   to other users and documents strict password-file permissions
   ([libpq environment variables](https://www.postgresql.org/docs/current/libpq-envars.html),
   [password-file contract](https://www.postgresql.org/docs/current/libpq-pgpass.html)).
4. Stream `pg_dump --format=custom --no-owner --no-privileges` from PostgreSQL
   18 directly to `create_backup_artifact()`'s temporary path. Set hard
   connection, statement and wall-clock deadlines; terminate and reap the
   process on timeout/cancellation.
5. Treat any stderr warning or nonzero exit as failure. Independently run
   PostgreSQL 18 `pg_restore --list`, parse a bounded TOC, and require all exact
   v0.8.6 Hindsight tables, `alembic_version`, the vector extension and expected
   schema objects. Reject an empty, oversized, malformed or wrong-major archive.
6. Keep only non-secret operational evidence: Hindsight compatibility version,
   PostgreSQL server/client major, pgvector version, Alembic revision and
   required schema-object names. The normal sidecar/backup record supplies the
   producer, target, timestamp, artifact bytes and SHA-256. Never record row
   values, webhook URLs or connection details.
7. Publish only after validation. The normal artifact helper supplies atomic
   rename, fsync, non-empty-file validation and the normal sidecar.

This deliberately produces one Hindsight artifact rather than asking the user
to model a generic PostgreSQL target. It allows service-specific version,
schema, native-file and restore validation while reusing the hardened
PostgreSQL subprocess/artifact seams.

## Least privilege and production gate

The production source credential should be a dedicated login restricted to the
single Hindsight database with `CONNECT` and read access to its schemas,
tables, views and sequences. PostgreSQL's predefined `pg_read_all_data` role
provides `SELECT` on tables/views/sequences and `USAGE` on schemas, but does not
bypass row-level security
([predefined roles](https://www.postgresql.org/docs/current/predefined-roles.html)).
Prove the exact grant set against PostgreSQL 18 and Hindsight 0.8.6, verify that
no relevant table has RLS enabled, then reduce further to database-local grants
if practical.

The backup identity must not have `SUPERUSER`, `CREATEDB`, `CREATEROLE`,
replication, write, execute-program, server-file, schema-create, Docker or host
filesystem privileges. PostgreSQL specifically warns that its server-file and
execute-program roles can bypass database permission checks and approach
superuser-level access
([predefined roles](https://www.postgresql.org/docs/current/predefined-roles.html)).

The later infrastructure change should only:

- declare the resolved Hindsight network external to the Homelab Backup stack
  and join the backend to it;
- inject the dedicated backup credential through the existing secret system;
  and
- pin the Hindsight/pgvector images used by the compatibility contract.

The inspected Homelab Backup deployment currently joins only its own network
and the Standard Notes network and has no Hindsight DB access
(`docker.compose/system/homelab-backup/homelab-backup.yaml:1-26`). Creating the
DB identity and network attachment are production mutations and require the
user's explicit approval after local completion. No production restore role is
created or stored.

## Create-only isolated restore contract

Declare `restore_capability = "partial"`. The plugin performs a complete
database materialization and structural verification, but the original Codex
credential and deployment configuration remain external and exact
application-level proof is performed by the local drill harness.

Restore must:

1. Accept only the staged, sidecar/hash-verified regular artifact from
   `RestoreService`. Re-run bounded `pg_restore --list` validation before any
   destination connection. PostgreSQL warns that restoring a dump executes
   code chosen by the source database's superusers, so accept only an artifact
   created by this plugin from the trusted Hindsight source target
   ([PostgreSQL 18 warning](https://www.postgresql.org/docs/current/app-pgdump.html#APP-PGDUMP-DESCRIPTION)).
2. Require a fresh PostgreSQL 18/pgvector destination database created from
   `template0`, with an exact fixed restore-name prefix, a versioned database
   comment sentinel, and only the exact expected `vector` extension installed
   by the disposable harness administrator. Refuse the production DB name,
   missing/wrong sentinel, an incompatible vector version, any other non-system
   object, any prior Hindsight table, source/destination identity equality, and
   artifact/destination ambiguity. PostgreSQL recommends `template0` for a
   truly empty restore target
   ([`pg_dump` notes](https://www.postgresql.org/docs/current/app-pgdump.html#APP-PGDUMP-NOTES)).
3. Use a disposable destination owner credential supplied only by the local
   drill. Never accept or reuse the source read-only credential. No production
   restore credential may be configured.
4. Generate a bounded exact TOC allowlist that omits only the archive's
   `CREATE EXTENSION vector` entry (and its extension comment), because the
   local administrator already installed the exact extension. Reject any other
   filtering. Run PostgreSQL 18 `pg_restore --use-list <allowlist>
   --single-transaction --exit-on-error --no-owner --no-privileges` with no
   `--clean`, `--create`, trigger disabling or error continuation. PostgreSQL
   documents both TOC-list selection and that single-transaction restore either
   completes entirely or applies no changes
   ([PostgreSQL 18 `pg_restore`](https://www.postgresql.org/docs/current/app-pgrestore.html#APP-PGRESTORE-OPTIONS)).
5. On failure, rely on transaction rollback and leave the newly created
   sentinel database for inspection; never clean, drop or overwrite an existing
   database. The drill owns teardown of the disposable database/container.
6. On success, require the exact table/column/index/constraint set,
   `alembic_version`, vector extension/version, no invalid indexes and
   `ANALYZE`. Validate selected synthetic IDs/hashes through parameterized
   queries without logging content.
7. Return `partial` with explicit remaining work: boot exact Hindsight 0.8.6,
   attach independently managed configuration/auth, and prove API behavior. A
   separate exact-image local drill performs that proof; production restore is
   always forbidden.

## Exact-version two-run disposable drill

Run the real plugin path entirely on the development VM. Use fresh private
Docker networks, synthetic credentials/content, new volumes/databases and the
Linux/amd64 image manifests recorded above. Do not use a production hostname,
credential, OAuth file or data export.

1. Build Homelab Backup with PostgreSQL 18 `psql`, `pg_dump` and `pg_restore`;
   assert all three client versions before testing.
2. Start exact pgvector PostgreSQL 18 and exact Hindsight 0.8.6. Configure
   `HINDSIGHT_API_LLM_PROVIDER=none`, native file storage and, for the fixture,
   `HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN=false`. Run the exact migration job.
   Require Hindsight readiness, exact version, server major, pgvector version
   and Alembic head.
3. Provision a source owner for Hindsight and a distinct read-only backup role.
   Prove the backup role cannot insert/update/delete, create objects, read
   server files, signal backends or connect to any unrelated test database.
4. Through first-party Hindsight APIs, create synthetic banks A and B, retain
   distinct chunks/documents, add a directive and a webhook with a synthetic
   signing secret, and upload a small synthetic file retained in native
   storage. Record only generated IDs, expected counts and fixture SHA-256.
5. Keep supported retain/update traffic active while the plugin creates
   artifact A. Prove the write traffic completes and the dump reports no
   warning. Independently verify artifact mode 0600, sidecar identity/size/hash,
   `PGDMP`/TOC validity, exact required objects and nonzero table data.
6. Mutate the source through supported APIs: change bank configuration, add and
   delete memories, update the directive/webhook, and replace/add file-backed
   content. Create artifact B online through the same real path. Require a
   distinct path and hash and independently validate it.
7. Create two new PostgreSQL 18 databases from `template0`, each with a unique
   correct restore sentinel, exact preinstalled vector extension and disposable
   owner. Restore A and B through the plugin, never reusing a destination.
   Prove schema, counts, vector values, file bytes and A-versus-B differences
   directly in each database.
8. Boot two fresh exact Hindsight manifests against the restored databases with
   provider `none`. Require readiness, exact version, list/recall of expected
   banks/content and directives/webhooks through redacted API responses. Prove
   recovered native bytes at the PostgreSQL storage boundary, assert the exact
   version's absence of an HTTP download route, and prove A lacks B-only state
   while B contains it.
9. Restart each restored Hindsight and PostgreSQL container and repeat the
   readiness, recall and file-hash checks to prove persistence rather than
   process cache behavior.
10. Exercise fail-closed paths: PostgreSQL 16 client against PG18, unreachable
    DB, bad credential, insufficient grants/RLS, dump warning/failure,
    timeout/cancellation, truncated/corrupt/wrong-plugin archive, missing
    required table/extension/revision, untrusted sidecar, source-equals-target,
    production-like DB name, absent/wrong sentinel, non-empty destination and
    mid-restore SQL failure. None may publish success or alter a pre-existing
    destination.
11. Tear down only disposable resources, then repeat the entire two-artifact,
    two-restore sequence from clean state. Record exact image/client/server/app
    identities, timings, counts, paths, sizes and hashes, never credentials or
    content.

## STOP conditions

Stop rather than weaken the contract if any of these is true:

- the actual declared/runtime service is not Hindsight 0.8.6 on PostgreSQL 18
  with the expected pgvector backend;
- the Homelab Backup runtime still has a PostgreSQL client older than the
  source server major, or the client/server identity cannot be proven;
- Hindsight uses S3, GCS, Azure or another external/extension-owned durable
  store not captured by the database;
- a custom tenant extension owns durable state outside the dumped database, or
  any Hindsight schema/table is omitted;
- any relevant table uses row-level security that the read-only role cannot
  completely export, or the exact least-privilege dump cannot succeed;
- production access would require a Docker socket, container exec, host root,
  database superuser/owner, writable application credential or host data mount;
- any dump warning occurs, required object/extension/revision is missing, or
  bounded artifact validation fails;
- the exact vector extension cannot be preinstalled in the isolated restore
  database or the restore TOC cannot omit only that already-provisioned object;
- plaintext secret-bearing artifacts cannot be protected by the separately
  managed backup destination, or someone requires app-managed encryption
  contrary to the current product boundary;
- the Codex OAuth credential and required deployment configuration are not
  independently recoverable;
- the restore destination is not provably new, empty, sentinel-marked and local,
  or any production hostname/database/credential appears;
- either exact-version local run fails restore atomicity, Hindsight readiness,
  recovered content/file verification, restart persistence or cleanup; or
- any production restore is requested. Production restores remain forbidden.

If a later deployment switches to external file storage, reclassify Hindsight
as a composite service and research that store's consistency/export boundary
before changing this plugin. Do not silently retain the DB-only contract.
