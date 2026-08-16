# Cal.com 6.2.0 PostgreSQL backup and restore research

Research date: 2026-08-16

Scope: the existing `calcom` plugin, the deployment declared in
`homelab-infra`, exact Cal.com v6.2.0 source and OCI metadata, and PostgreSQL 16
backup/restore contracts. No production endpoint, host, container, database,
artifact, or credential was contacted. No secret value is reproduced here.

## Decision summary

The complete local implementation and proof are doable now. Cal.com's durable
application state in the declared deployment is one PostgreSQL database, so an
online custom-format logical dump is the correct artifact. PostgreSQL documents
that `pg_dump` takes a consistent single-database backup while readers and
writers continue, so routine backup needs no Cal.com downtime
([PostgreSQL 16 `pg_dump`](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-DESCRIPTION)).

This milestone should have two deliberately separate layers:

1. a generic PostgreSQL archive foundation for private credentials, fixed
   client execution, deadlines/cancellation, atomic artifact publication,
   bounded TOC inspection, and fresh transactional restore; and
2. a thin Cal.com adapter that pins app v6.2.0, PostgreSQL major 16, the exact
   Prisma migration/schema fingerprint, Cal.com marker queries, and exact-image
   readiness/content proof.

Do not solve Cal.com by weakening the generic PostgreSQL plugin or by keeping
the current URL/fallback behavior as compatibility. The adapter must fail
closed on version or schema drift.

Declare `restore_capability = "partial"`, even though the database recovery is
complete. The plugin can populate and verify a separately provisioned fresh
database, but it cannot
re-provision or prove the deployment's `CALENDSO_ENCRYPTION_KEY`, NextAuth and
integration environment, external calendar/video systems, routing, or a booted
application. The encryption key is required and is used for AES-256 symmetric
encryption/decryption of stored credentials
([v6.2.0 environment contract](https://github.com/calcom/cal.diy/blob/1c193cca8682b33b9866c792186033f7ef886682/.env.example),
[exact crypto implementation](https://github.com/calcom/cal.diy/blob/1c193cca8682b33b9866c792186033f7ef886682/packages/lib/crypto.ts)).
The disposable drill supplies synthetic configuration and performs that final
boot proof; a database-only production recovery still has those explicit
prerequisites.

## Exact declared deployment and immutable identity

The tracked declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/dmz/calcom/calcom.yaml`](../../../homelab-infra/docker.compose/dmz/calcom/calcom.yaml).
Its Cal.com files were last changed by commit
`ae98933328e06bd47e1c4a54f45dc78d4752aa01`.

| Component | Declared identity and state |
| --- | --- |
| Cal.com | `calcom/cal.com:v6.2.0`, private bridge network, no app data volume |
| Database | mutable `postgres:16`, `/opt/dmz/data/calcom-postgres` mounted at `/var/lib/postgresql/data` |
| Bootstrap | one-shot PostgreSQL client creates database `calendso`, owned by role `calcom` |
| App DB role | login, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`; owns the database/public schema so Prisma can migrate |

The deployment has no Cal.com `public`, upload, or other application-state
mount. Its app points `DATABASE_URL` and `DATABASE_DIRECT_URL` at PostgreSQL.
The official v6.2.0 Prisma schema likewise declares PostgreSQL as its datasource
and contains the users, credentials, calendars, schedules, event types,
bookings, workflows, webhooks, API keys, apps, and related control-plane models
([exact Prisma schema](https://github.com/calcom/cal.diy/blob/1c193cca8682b33b9866c792186033f7ef886682/packages/prisma/schema.prisma)).
Therefore the `calendso` database is the single local consistency boundary.
Provider-side calendar events, video recordings, SMTP, OAuth client settings,
and deployment secrets remain independently recovered external/configuration
state.

Cal.com release `v6.2.0` is the signed commit
[`1c193cca8682b33b9866c792186033f7ef886682`](https://github.com/calcom/cal.diy/commit/1c193cca8682b33b9866c792186033f7ef886682)
([release](https://github.com/calcom/cal.diy/releases/tag/v6.2.0)). Read-only
registry resolution on the research date produced:

| Image | OCI index | Linux/amd64 manifest | Source |
| --- | --- | --- | --- |
| `calcom/cal.com:v6.2.0` | `sha256:ace3bb1219fb7306585ab9f4d94d41af7ee064c343db0498173436bbe857bd49` | `sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001` | Cal.com commit `1c193cca...`; OCI label version `v6.2.0` |
| local PG16.14 candidate `postgres:16.14` | `sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b` | `sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00` | [official tag metadata](https://hub.docker.com/v2/namespaces/library/repositories/postgres/tags/16.14); [`docker-library/postgres` commit `f64ed48c...`](https://github.com/docker-library/postgres/commit/f64ed48c36df300966bcbe72fee53af745045579) |

The Cal.com platform manifest is also visible on the official verified
[Docker Hub repository](https://hub.docker.com/r/calcom/cal.com/tags). The
declared `postgres:16` is mutable and had already moved to 16.15 registry
content by the end of this research
([official mutable-tag metadata](https://hub.docker.com/v2/namespaces/library/repositories/postgres/tags/16)).
Neither a Compose tag nor today's registry resolution proves the actual
production runtime digest. Use the immutable
PG16.14 platform manifest above for the first exact local candidate, but record
the production container digest and `server_version_num` read-only before later
activation; stop if it is not PostgreSQL major 16 and rerun the exact drill if
its patch/runtime identity differs materially.

The tracked Homelab Backup backend is not on the DMZ Cal.com bridge, and the
database publishes no port. Production backup is therefore not currently
reachable from that backend. Do not add a Docker socket, host mount, public DB
listener, or privileged workload as a workaround.

## Online consistency and authoritative artifact

Use a full, single-connection PostgreSQL 16 custom archive:

```text
pg_dump --format=custom --no-owner --no-privileges
```

Custom format is compressed and intended for `pg_restore`; a full dump captures
schema, table data, sequences, and large objects
([format contract](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-OPTIONS-FORMAT),
[large-object behavior](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-OPTIONS)).
It excludes cluster-global roles and tablespaces because `pg_dump` covers one
database only. Those deployment identities are prerequisites, not Cal.com
domain data.

Use PostgreSQL 16 clients for this adapter. The backend currently ships only
PostgreSQL 18 clients; while a newer `pg_dump` can read an older server,
PostgreSQL does not guarantee that newer-client output loads into an older
server. The exact-version drill should therefore add version-addressable PG16
`psql`, `pg_dump`, and `pg_restore` without replacing the PG18 tools required by
other plugins
([cross-version notes](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES)).

`test()` and `get_status()` should execute a real, bounded, read-only probe and
require all of the following:

- PostgreSQL server major 16, exact configured database identity, UTF-8, and no
  unexpected user schema/extension;
- every expected v6.2.0 Prisma migration applied successfully, latest migration
  `20260219000000_add_fallback_action_to_queued_form_response`, no failed or
  future migration, and an exact normalized catalog fingerprint;
- the complete pinned Cal.com table/sequence/constraint/index inventory, not
  merely representative table names;
- zero row-level-security-enabled Cal.com table and no unclassified large
  object; and
- representative typed markers for users, schedules, event types, attendees,
  bookings, credentials, selected/destination calendars, workflows, and
  webhooks without returning their values.

The v6.2.0 image starts by waiting for PostgreSQL, running `prisma migrate
deploy`, seeding app-store metadata, and starting the web app
([exact start script](https://github.com/calcom/cal.diy/blob/1c193cca8682b33b9866c792186033f7ef886682/scripts/start.sh),
[official Compose topology](https://github.com/calcom/cal.diy/blob/1c193cca8682b33b9866c792186033f7ef886682/docker-compose.yml)).
That makes migration identity and an exact-image post-restore boot observable
compatibility gates rather than assumptions.

### Least-privileged source identity

Production should use a dedicated backup login, not the current application
owner. It needs `CONNECT` only to `calendso`, `USAGE` on each required schema,
`SELECT` on every required table/view and sequence, and read access to any
classified large object. Future tables and sequences need equivalent default
privileges. PostgreSQL's `pg_read_all_data` can provide those table/view/
sequence and schema privileges but does **not** bypass RLS, so explicit
database-local grants are preferable once the exact inventory is proven
([predefined roles](https://www.postgresql.org/docs/16/predefined-roles.html)).

Prove denial of writes, DDL, object/database/role creation, replication, RLS
bypass, backend signalling, server-file/program roles, unrelated databases,
the Docker API, and host files. If exact v6.2.0 needs an unclassified object,
RLS bypass, ownership, or superuser to dump completely, stop and research it;
do not silently omit it.

Use strict flat `mode`, `host`, `port`, `database`, `user`, and `password`
fields. Reject URLs, unknown keys, coercions, fallback aliases, control
characters, and source/restore mode confusion. Put the password in a private
temporary `PGPASSFILE`, never argv, a connection URL, or `PGPASSWORD`;
PostgreSQL warns that environment variables may be observable and ignores a
password file with permissive Unix permissions
([libpq environment](https://www.postgresql.org/docs/16/libpq-envars.html),
[password file](https://www.postgresql.org/docs/16/libpq-pgpass.html)).

## Backup and validation contract

The generic foundation should stream the dump into
`create_backup_artifact()`'s mode-0600 temporary file with fixed argv/env,
connection/statement/lock/wall deadlines, bounded stderr, cache eviction, and
termination plus reap on failure, timeout, or cancellation. Any nonzero exit or
warning fails. No partial artifact or sidecar may publish.

The Cal.com adapter supplies the exact source fingerprint. Before publication:

1. inspect the still-open artifact descriptor, not a replaceable pathname;
2. run bounded `pg_restore --list` with the same PG16 toolset;
3. require one unambiguous TOC containing the exact v6.2.0 schemas, tables,
   table-data entries, sequences/sets, constraints, indexes, and expected
   extension/large-object entries, with no foreign service object;
4. compare the archive's normalized schema-only form/fingerprint to the pinned
   source fingerprint, because `--list` alone does not inspect executable SQL;
5. require nonzero bounded size, exact client/server majors, and a complete
   Cal.com marker inventory; and
6. attach only non-secret evidence to the sidecar: adapter/app version,
   PostgreSQL major/patch, migration head, schema/TOC fingerprint, object
   counts, and source database identity hash.

The archive contains password hashes, encrypted OAuth credentials, API keys,
booking/customer data, webhook secrets, and other private content. Never log or
store rows, marker values, connection details, object definitions, stderr,
filenames derived from user data, or encryption material. PostgreSQL also warns
that restoring a dump executes source-controlled SQL, which is why an exact
schema contract is required before the local restore
([restore trust warning](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-DESCRIPTION)).

## Fresh create-only isolated restore

Restore is local/dev-only and disabled unless the isolated-restore runtime gate
is set. `RestoreService` must supply its private staged, sidecar/hash-verified
copy. Reopen and bind a regular non-symlink artifact descriptor; revalidate its
hash, producer/target identity, PG major, migration/schema fingerprint, and TOC
before connecting anywhere.

Require distinct source and destination targets and a separately provisioned
destination owner. The disposable administrator creates a database from
`template0` with an exact name prefix such as `hlb_calcom_restore_` and comment
sentinel `homelab-backup:calcom-restore:v1`; PostgreSQL specifically recommends
`template0` for a truly empty restore destination
([fresh database guidance](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES)).
The plugin then proves major 16, the exact sentinel, and zero non-system
objects. It must reject an existing Cal.com schema, any nonempty/unsentinelled
database, the production/source identity, or a server reachable outside the
explicit disposable allowlist.

Run `pg_restore --exit-on-error --single-transaction --no-owner
--no-privileges` into that already-created empty database. Never use `--clean`,
`--create`, trigger disabling, error continuation, shell execution, or a source
owner. PostgreSQL guarantees that single-transaction restore either completes
all commands or applies none and that it implies exit-on-error
([transactional restore](https://www.postgresql.org/docs/16/app-pgrestore.html#APP-PGRESTORE-OPTIONS)).
Inside the same bounded workflow, prove exact migrations, schema/TOC identity,
constraints, sequences, row counts, and content-marker hashes. PostgreSQL
planner statistics are regenerable operational state, not an artifact invariant;
the plugin does not run a broad `ANALYZE` as part of the transaction. The exact
app boot/read/restart drill is the readiness proof. On failure leave the fresh
sentinel database for inspection; the plugin never drops, cleans, or retries
destructively. Return an honest `partial` result that names the remaining
exact-image/configuration boot proof without revealing content.

## Exact two-round disposable Docker drill

Run only on the development VM. Use synthetic credentials/data, unique labels,
private internal networks, no published ports, no host networking, no Docker
socket in workloads, and no production route. Pin Linux/amd64:

- `calcom/cal.com@sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001`
- `postgres@sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00`

Use the same synthetic `CALENDSO_ENCRYPTION_KEY` for source and restored app
boots; do not persist or print it. A runner on each internal network may drive
the first-party web setup/booking UI and inspect HTTP responses. Workloads get
no host mounts except new disposable PostgreSQL volumes.

For one authoritative sequence:

1. Create the exact source app/database pair. Let the official start script
   migrate and seed it. Record image manifests, PostgreSQL 16.14, migration
   head, exact catalog fingerprint, app readiness, and the backup role's
   positive reads plus every required denial.
2. Create the phase-A synthetic user/profile through Cal.com's supported setup
   HTTP route. With the disposable app stopped, seed the remaining schedule,
   event, attendee/booking, credential/calendar, workflow, webhook, and
   API-key-shaped test fixture directly against the exact pinned Prisma schema.
   Restart the app, record only canonical hashes/counts, and prove the public
   event and booking pages. This deterministic fixture proves recovered
   application reads; it does not claim a stable public mutation API for every
   Cal.com control-plane model. Do not contact a real external provider.
3. While the exact app remains online, create artifact A through the real
   target/job/plugin path. Require private mode, valid sidecar/hash, distinct
   source fingerprint, strict TOC, and no publication on an injected dump
   warning/failure/timeout/cancellation.
4. Stop the same source, apply the distinguishable phase-B fixture against the
   exact schema, restart it, and create artifact B. Require a different path and
   SHA-256 and prove A describes only phase A while B describes A+B.
5. Create destination A from `template0` with its own owner and sentinel.
   Restore A through `RestoreService`, prove exact database markers, then boot
   the exact Cal.com image against it with the synthetic source key/config.
   Prove readiness and UI/public-page visibility of phase A and absence of the
   phase-B markers. Restart both app and database and repeat all proofs.
6. Destroy destination A. Repeat the create-only restore, exact app boot,
   content proof, and restart on a separately created destination B. Prove the
   phase-B differences. Neither destination may be reused or pre-seeded with
   Cal.com objects.
7. Prove Cal.com-specific fail-closed behavior for wrong immutable app identity
   or PostgreSQL patch, write-capable/RLS source identities, migration/schema
   drift, corrupt or altered Cal.com artifact evidence, missing sentinel, and
   nonempty or unapproved destination. Reuse Plan 017's generic PostgreSQL unit
   and exact-drill evidence for dump lifecycle, descriptor replacement, TOC/DDL,
   same-database authorization, and transactional failure. Prove the correct
   synthetic key decrypts restored integration data and a wrong key does not;
   do not expect startup itself to fail when no encrypted integration is read.
8. Tear down by exact drill labels and audit both label and generated-name
   prefixes. Require zero containers, networks, volumes, runners, listeners,
   credentials, and temporary artifacts after each round and after the whole
   test.

The two independently created artifacts, two independently fresh databases,
two exact-image boots, and two restart proofs are the consecutive recovery
drills. Running two backups and restoring only one is insufficient.

## Pre-milestone baseline and implemented contract

At the start of this research, `backend/app/plugins/calcom/plugin.py` was a
streaming prototype rather than the selected contract:

- it accepts URL configs, unknown keys and a `database_direct_url` fallback;
- it normally uses the application owner and `PGPASSWORD`, not a dedicated
  private password file/read-only role;
- its description says PostgreSQL 16 while the backend currently invokes the
  unversioned PostgreSQL 18 binaries;
- `test()` proves only `SELECT 1`; `get_status()` is constant `unknown`;
- backup accepts raw stderr diagnostics and validates only that
  `pg_restore --list` exits zero, without bounded TOC/schema/Cal.com identity;
- path-based validation/restore leaves replacement races;
- restore uses destructive `--clean --if-exists`, has no local-runtime,
  source/destination, sentinel, emptiness, allowlist, or migration guard; and
- it reports automatic success without Cal.com content/readiness proof.

The original three unit tests covered only the happy path and did not establish
least privilege, strict schema/TOC identity, negative publication, immutable
staging, fresh-only restore, rollback, application boot, or two-round recovery.

Plan 018 now closes that baseline locally. The adapter uses strict flat
source/destination fields, the committed version-addressable PostgreSQL 16
foundation and private `PGPASSFILE`, exact Cal.com 6.2.0 migration/catalog/
marker identity, stable pre/post capture profiles, descriptor-bound private
artifacts and sidecars, create-only sentinel RestoreService destinations, and an
honest `partial` outcome. Two clean exact-image rounds each produced immutable
A/B scheduler artifacts, restored both into separate fresh databases, proved
phase-specific application pages and typed control-plane markers, and repeated
the proof after app/database restart. Production connectivity, target/job setup,
least-privilege grants, and any backup-only run remain separate rollout gates;
production restore remains forbidden.

## STOP conditions

Stop local implementation/drill work rather than improvise if:

- the immutable Cal.com image does not identify v6.2.0/source commit
  `1c193cca...`, fails its first-party migration/start path, or requires a
  production/external credential to prove synthetic recovered state;
- exact source migrations/catalog differ from the pinned v6.2.0 inventory;
- a complete dump needs owner, superuser, RLS bypass, write, server-file/
  program, Docker, or host access, or an unclassified large object cannot be
  proven readable;
- PG16 clients cannot produce and transactionally restore the exact archive
  into a fresh PG16.14 destination;
- backup warnings, TOC/schema ambiguity, application boot failure, wrong-key
  behavior, marker mismatch, restart failure, or cleanup residue remains; or
- any proposed restore could address a production or nonfresh database.

Production remains a separate approval gate. Stop before activation if the
actual runtime digest/server patch has not been read-only verified, the DMZ
network cannot expose only PostgreSQL to the dedicated backup identity, exact
least-privilege grants/default grants are absent, migrations or RLS/large-object
state drift, or a backup requires downtime. Never run a Cal.com restore,
sentinel creation, schema write, credential grant, network change, container
change, or other write operation in production through this task. The only
eventual production operation in scope is a user-approved, read-only backup
after those gates pass.
