# Standard Notes self-hosted backup and restore research

Research date: 2026-08-15
Scope: the declared homelab deployment and the exact first-party server image it
pins. No production endpoint or host was contacted.

## Decision summary

Standard Notes must be treated as one **composite backup**:

1. the single MySQL application schema; and
2. the filesystem uploads tree.

Redis and LocalStack are not part of the durable backup. In this deployment,
Redis contains expiring sessions, locks, tokens, metrics, and upload
coordination; LocalStack provides only SNS/SQS queues and topics, with no S3
service or persistent data volume. Both should be recreated empty at restore.

There is a blocking consistency decision: the exact server has no maintenance,
snapshot, drain, or global read-only API. MySQL can make an internally
consistent online InnoDB dump, but that transaction cannot be coordinated with
the separate uploads filesystem. A backup that is *provably consistent across
both stores* therefore requires the Standard Notes server to be quiesced for a
short maintenance window. The official update workflow likewise stops the
stack with `docker compose down`; it does not document an online maintenance
mode ([official update guide](https://standardnotes.com/help/self-hosting/updating)).
Per the service-coverage program's stop conditions, production implementation
must not silently weaken this to an online best-effort copy: obtain explicit
downtime approval first.

## Exact deployed declaration and source mapping

The deployment declaration inspected at homelab-infra commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`/home/dev/projects/homelab-infra/docker.compose/work/standardnotes/standardnotes.yaml`.
It declares:

| Component | Declared image | Backup relevance |
|---|---|---|
| Server | `standardnotes/server:latest@sha256:6b371bc0c3ae755500b82f4c580fae7fd768e5f9214cdc2f195fc130cf6ffdcd` | Immutable server bits; authoritative DB and uploads client |
| MySQL | `mysql:8.4.0-oraclelinux8` | Authoritative relational state |
| LocalStack | `localstack/localstack:4.14` | SNS/SQS transport only |
| Redis | `redis:8.10-alpine` | Ephemeral/cache state |
| Web | `standardnotes/web:latest@sha256:9db2acbd6c7c11bfb8183342c61ff30996ab054219da298aba788f1c39cee20c` | Static/rebuildable; no persistent mount |

Only the Standard Notes server and web declarations are immutable digests.
The MySQL, LocalStack, and Redis declarations are tags, so their exact pulled
image digests cannot be proven from Git alone without contacting the runtime.
The versions above are the strongest exact statements available from the
requested read-only evidence.

Inspection of the pinned server image on the local development Docker daemon,
with networking disabled, reports these packaged versions:

- `@standardnotes/api-gateway` 1.92.2
- `@standardnotes/auth-server` 1.178.6
- `@standardnotes/files-server` 1.38.3
- `@standardnotes/syncing-server` 1.136.5
- `@standardnotes/revisions-server` 1.51.19

All five annotated upstream tags peel to the same source commit,
[`162a63ae2bb926e1f061d3967dbe98878f0aedb7`](https://github.com/standardnotes/server/commit/162a63ae2bb926e1f061d3967dbe98878f0aedb7).
The exact package manifests are available for
[API Gateway](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/api-gateway/package.json),
[auth](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/auth/package.json),
[files](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/package.json),
[syncing](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/syncing-server/package.json),
and [revisions](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/revisions/package.json).
This commit, rather than current `main`, is the source contract for the plugin
and local drills.

## Authoritative state boundary

### 1. One MySQL schema

The deployment supplies one database name to every service. The exact
[Docker entrypoint](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/docker/docker-entrypoint.sh)
fans the same `DB_*` values into auth, syncing, files, revisions, and the API
gateway. The auth, syncing, and revisions
[data-source definitions](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/auth/src/Bootstrap/DataSource.ts)
all select the same configured MySQL database and automatically run each
service's migrations when its server process starts. The syncing and revisions
variants are
[here](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/syncing-server/src/Bootstrap/DataSource.ts)
and
[here](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/revisions/src/Bootstrap/DataSource.ts).

The entire configured schema is authoritative; do not select a hand-maintained
table subset. It includes users and authentication data, sessions, roles and
permissions, settings and subscriptions, encrypted synced `items`, shared
vault/message state, revisions, and TypeORM migration history. A full logical
schema dump also prevents newly added vendor tables from disappearing silently.

Use the pinned MySQL client contract already shipped by Homelab Backup:
`mysqldump --single-transaction --quick --skip-lock-tables --routines --events
--triggers --hex-blob --no-tablespaces --set-gtid-purged=OFF <database>`.
MySQL documents that `--single-transaction` gives a consistent snapshot for
InnoDB, that DDL during the dump can invalidate it, and that `--quick` streams
large tables ([MySQL 8.4 `mysqldump` reference](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html#option_mysqldump_single-transaction)).

### 2. Filesystem uploads

The deployment binds the host uploads directory to
`/opt/server/packages/files/dist/uploads`. That is the exact default chosen by
the files service when no S3 region or endpoint is configured. The storage
selection is explicit in the exact
[files container](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/src/Bootstrap/Container.ts):
without S3 configuration it wires the filesystem uploader/downloader/remover;
with S3 configured it wires the S3 implementations instead. The
[filesystem uploader](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/src/Infra/FS/FSFileUploader.ts)
creates parent directories and appends encrypted chunks to the resolved file
path. Standard Notes' self-hosting guide independently identifies `uploads/`
as the default durable upload directory
([official files guide](https://standardnotes.com/help/self-hosting/files)).

The uploads tree is therefore authoritative even if it is currently empty.
Archive all regular files and directories, preserve their relative paths, and
reject symlinks, devices, FIFOs, sockets, absolute paths, and traversal.
Capture per-file size and SHA-256 in the artifact manifest so restore can prove
byte equality.

### 3. There is no deployed S3 object store

Although the exact source supports S3 for file bytes, the deployment env
declares no files-service S3 region, endpoint, or bucket. Its LocalStack service
explicitly enables only `sns,sqs`, and the compose file mounts no LocalStack
data volume. The exact first-party
[LocalStack bootstrap](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/docker/localstack_bootstrap.sh)
only creates SNS topics, SQS queues, and subscriptions. Consequently:

- LocalStack contains no authoritative uploaded objects in this deployment;
- its queue/topic topology is rebuildable and should not enter the artifact;
- a future target that enables S3 must be detected and refused by this
  filesystem-mode contract until bucket export and restore are implemented.

The host path bound as `localstack_bootstrap.sh` is not tracked in the inspected
homelab-infra tree, so disaster recovery should separately pin the exact
upstream script (or declare its contents in infrastructure-as-code). This is a
configuration gap, not a reason to back up transient queues.

### 4. Redis is intentionally excluded

The exact source uses Redis for expiring cross-service-token cache entries,
login/OTP locks, PKCE state, ephemeral sessions, subscription tokens, upload
session bookkeeping, used valet-token markers, and short-lived metrics. For
example, the
[upload repository](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/src/Infra/Redis/RedisUploadRepository.ts)
uses two-hour TTLs, and the
[valet-token repository](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/src/Infra/Redis/RedisValetTokenRepository.ts)
uses expiring markers. This is volatile operational state, not durable notes or
file content. Restore with a fresh empty Redis. Expected consequences are
session/token invalidation and loss of unfinished uploads, which is safer than
reviving stale locks or one-use tokens.

### 5. External keys and configuration

The artifact must not embed deployment secrets. Recovery nevertheless requires
the externally managed configuration to be available:

- `AUTH_SERVER_ENCRYPTION_SERVER_KEY` must remain the same. The exact
  [auth crypter](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/auth/src/Domain/Encryption/CrypterNode.ts)
  uses it to decrypt each user's encrypted server key; changing it makes that
  server-encrypted data unreadable.
- `AUTH_JWT_SECRET` and `VALET_TOKEN_SECRET` are required by the exact
  entrypoint and sign cross-service/session and file-operation tokens. Preserve
  them for continuity; deliberate rotation invalidates outstanding tokens.
- DB connection values, `PUBLIC_FILES_SERVER_URL`, and cookie-domain settings
  are required deployment configuration but are not application backup data.
- `AUTH_SERVER_PSEUDO_KEY_PARAMS_KEY` and the legacy JWT secret are generated
  at startup when absent in this deployment. They already change on ordinary
  restarts and are not part of the current persistent contract.

The SQL dump itself remains sensitive: it contains account identifiers,
password verifiers, encrypted user/server material, and encrypted notes.
Uploads are client-encrypted but still sensitive. Artifacts should be private
regular files (`0600`), never expose credentials in argv/logs, and never print
restored content or secret values during drills.

## Consistency and maintenance requirement

`mysqldump --single-transaction` coordinates only MySQL's transactional tables;
it has no transaction with the separately mounted uploads tree. The files
service writes upload chunks in memory and appends them to the filesystem on
finish, while database item metadata is handled by the syncing service. An
online database dump followed by an online filesystem walk can therefore
capture file metadata and bytes from different moments.

No vendor maintenance/quiesce API exists in the exact source or official
self-hosting documentation. The only read-only feature is per-user session
policy, not a global maintenance mode, and does not stop all ingress, file
writes, registrations, or background work. The exact
[supervisor configuration](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/docker/supervisord.conf)
runs API, auth, syncing, files, revisions, and workers in one server container;
the official operational stop primitive is Docker Compose.

The safe production sequence is therefore, **only after explicit downtime
approval**:

1. reject new client ingress and allow active requests/uploads to finish;
2. stop the Standard Notes server container (leave MySQL running for logical
   export); verify ports 3000/3104 no longer accept requests;
3. run the full single-schema `mysqldump` and archive the uploads tree while
   the writer remains stopped;
4. validate non-empty dump structure, archive safety, hashes, counts, and the
   composite manifest;
5. publish the artifact and sidecar atomically; then restart Standard Notes and
   confirm functional readiness.

The backup plugin should not receive a Docker socket. Quiescing is an external
operator/orchestrator responsibility. Its minimum production privileges are
MySQL read/dump access, a read-only uploads mount, and write access to
`/backups`. If the service is not demonstrably quiesced, fail rather than label
the result consistent.

## Restore workflow

Restore is restricted to a fresh isolated local destination. Production
restore remains forbidden.

1. Validate the artifact and sidecar before mutation: exact manifest version,
   source image digest/component versions, expected members only, safe paths,
   per-member sizes/hashes, and no links or special files.
2. Create a new isolated network, an empty MySQL 8.4 destination schema, an
   empty uploads staging directory, fresh Redis, and LocalStack with the exact
   pinned bootstrap script. Do not start the Standard Notes server yet.
3. Extract uploads into staging and verify every file hash/count.
4. Import the SQL dump into the empty schema using the MySQL client. MySQL's
   documented restore contract is `mysql <database> < dump.sql`
   ([MySQL 8.4 reload guide](https://dev.mysql.com/doc/refman/8.4/en/reloading-sql-format-dumps.html)).
5. Query schema/table counts and selected synthetic sentinel rows. Atomically
   publish the staged uploads directory into the isolated server mount.
6. Supply local test keys/configuration without logging them and start the
   **same server digest**. Starting a newer image is not part of restore: each
   server process automatically runs migrations, so a cross-version boot could
   mutate the restored schema before it is validated.
7. Verify application behavior, then leave upgrade testing to a separate copy.

The plugin's honest `restore_capability` should be `partial` unless it can
perform the final application boot and functional checks without broad Docker
privileges. It can safely restore the database and uploads to an isolated
destination; the local integration harness can then launch and verify the exact
image.

## Readiness and two-drill evidence

The exact API Gateway and files-server `/healthcheck/` controllers return the
literal string `OK` and do not query MySQL, Redis, or the uploads filesystem
([API Gateway controller](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/api-gateway/src/Controller/HealthCheckController.ts),
[files controller](https://github.com/standardnotes/server/blob/162a63ae2bb926e1f061d3967dbe98878f0aedb7/packages/files/src/Infra/InversifyExpress/AnnotatedHealthCheckController.ts)).
Health alone is liveness evidence, not restore proof.

Each of two consecutive local drills should therefore:

- build a disposable source stack at the declared MySQL version and exact
  Standard Notes server digest, with no production network or mounts;
- create a synthetic account and encrypted note/item through the public API,
  plus a deterministic non-secret file sentinel in the exact uploads layout;
- quiesce the local server, invoke the real plugin backup path, and verify the
  artifact plus sidecar;
- restore into a *different fresh* MySQL schema and uploads directory;
- independently compare dump/artifact size and SHA-256, source/destination row
  counts and sentinel identifiers, upload file count/size/SHA-256, and manifest
  values;
- boot the exact server digest with fresh Redis/LocalStack and the restored
  state; require API Gateway and files health plus a real login/sync read that
  returns the synthetic item, and verify the restored upload bytes by hash;
- never execute against or attach to a production target.

If the local API fixture cannot exercise a vendor-supported file download
without granting a subscription, direct restored-file hash equality remains
valid content evidence, but database readiness must still be proved through a
real authenticated API read rather than SQL and health alone.

## Recommended composite artifact contract

Use one transactional artifact, for example a tar container with exactly:

- `manifest.json`
- `database.sql`
- `uploads.tar`

The manifest should include format version, timestamp, exact server image
digest and five component versions, declared MySQL version, database name only
if it is not secret, schema/table counts, dump size/hash, uploads member
count/aggregate bytes/archive hash, and an explicit `quiesced: true` evidence
field. It must never contain passwords, keys, cookies, tokens, note plaintext,
or raw environment contents. The outer artifact is valid when there are no
uploads, provided the SQL member is a structurally valid non-empty complete
schema dump and the manifest truthfully records zero upload files.

## Implementation gate

Before coding or production validation, ask one concrete question: **may
Standard Notes be unavailable for a short scheduled window during each
backup?** If yes, implement the composite plugin and external quiescence check.
If no, classify exact point-in-time composite coverage as blocked; an online
database-only or independently timed filesystem copy is not equivalent and
must not be presented as reliable Standard Notes recovery.
