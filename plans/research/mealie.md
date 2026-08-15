# Mealie v3.22.0 backup and restore research

## Decision

The recoverable service boundary is one coordinated capture of the `mealie`
PostgreSQL database and the authoritative content under `/app/data`. For this
deployment, the strongest honest contract requires a short Mealie-only write
outage: stop the `mealie` container, leave the shared PostgreSQL server online,
take a logical database dump and a read-only filesystem archive, then restart
and prove readiness before publishing the artifact.

Mealie v3.22.0 has a supported native ZIP backup and destructive restore, but
neither is the right production seam for this plugin. The native backup can
commit database repairs before exporting, has no explicit point-in-time
transaction spanning its table-by-table JSON export, and scans `/app/data`
afterwards. Its PostgreSQL restore drops the existing schema, later replaces
filesystem directories, and Mealie's own documentation requires temporarily
making the application database user a PostgreSQL superuser. Sources:
[tagged exporter](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/services/backups_v2/alchemy_exporter.py),
[tagged backup/restore service](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/services/backups_v2/backup_v2.py),
and [Mealie backup/restore documentation](https://github.com/mealie-recipes/mealie/blob/v3.22.0/docs/docs/documentation/getting-started/usage/backups-and-restoring.md).

**Implementation is blocked.** The current Homelab Backup deployment has no
Mealie data mount, PostgreSQL network access, or narrowly scoped lifecycle
control. Downtime and an access design must be approved before implementation.
The declared PostgreSQL image is only `postgres:16`, so its exact running minor
version and digest are also unresolved. Raw Docker-socket access and a database
superuser are not acceptable defaults.

This research made no call to a production host or endpoint, changed no
production state, contains no secret values, and authorizes no production
backup, stop, write, or restore. Every restore described below is create-only
and disposable; production restores are forbidden.

## Exact deployment researched

At infrastructure commit `eeed77a76fbc23db3da8470011535ad64cf0bc75`,
`/home/dev/projects/homelab-infra/docker.compose/misc/mealie/mealie.yaml`
declares:

- one `mealie` container using `ghcr.io/mealie-recipes/mealie:v3.22.0`;
- `/docker-apps/mealie/` bind-mounted at `/app/data/`;
- `DB_ENGINE=postgres`, database `mealie`, server `postgres`, port 5432, and a
  dedicated application username supplied through env/secret files;
- attachment to the external `system_postgres_network`; and
- host port 9925 mapped to Mealie port 9000.

The shared database declaration at
`/home/dev/projects/homelab-infra/docker.compose/system/postgres/postgres.yaml`
uses `postgres:16` and bind-mounts its cluster data directory. Do not archive
that live cluster directory: PostgreSQL says an ordinary file copy requires a
server shutdown and is cluster-wide, whereas `pg_dump` is the database-scoped
logical mechanism. See [PostgreSQL 16 file-system backup](https://www.postgresql.org/docs/16/backup-file.html)
and [pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html).

The Mealie release tag resolves to source commit
`1eea0254a35e82bf80c255909890ba49c8cb660b`; see the
[v3.22.0 release](https://github.com/mealie-recipes/mealie/releases/tag/v3.22.0).
At research time the official GHCR v3.22.0 tag resolved to OCI index digest
`sha256:36c28f0642fb6c75fae8997a2d55994631b9b4bcffba3016c208fc132a4c1e69`
(amd64 manifest
`sha256:233be76b4cbf8f2d89b11cf689779d6f63c73254cb06815ea37cdeb56c056612`).
The registry-owned package is published on the
[official Mealie GHCR package page](https://github.com/mealie-recipes/mealie/pkgs/container/mealie).
The infrastructure pins a tag, not a digest, so neither that registry result nor
the repository proves the image ID currently running in production. The same
uncertainty applies to the moving `postgres:16` tag. Before an exact-deployment
drill or implementation, an explicitly approved **read-only** inventory must
record the running Mealie image ID/platform and PostgreSQL image ID plus
`server_version`; it must not invoke a backup or write anything.

## Authoritative state

| State | Backup disposition |
| --- | --- |
| PostgreSQL database `mealie` | Required. It is the authoritative relational state: users, groups/households, recipes and ingredients, plans, shopping lists, API tokens, configuration, and schema revision. Use a whole-database logical dump, not selected tables. |
| `/app/data/recipes` | Required. Recipe images and assets live outside PostgreSQL. |
| `/app/data/users` | Required. User-owned files such as profile images live here. |
| `/app/data/groups` | Required. Group-generated/exported files are part of the managed data tree. |
| `/app/data/templates` | Required if populated. It is a first-class data directory in v3.22.0. |
| `/app/data/.secret` | Required and highly sensitive. Mealie creates it under the data root and uses it to sign and validate access/API tokens, so replacing it invalidates those tokens. Preserve it without ever logging its value. |
| `/app/data/.session_secret` | Security/session state, not durable user content. Mealie uses it for browser-session signing. Match native restore's safety behavior: omit it from the restorable payload so a fresh value is generated and all pre-restore browser sessions are revoked. Record this intentional effect in restore output. |
| `/app/data/backups` | Exclude. This is native-backup staging and including it would recurse/duplicate prior archives. |
| `/app/data/.temp`, `mealie.log*`, `*.zip`, and PostgreSQL-mode `mealie.db` | Exclude as temporary/log/native-archive/irrelevant SQLite state. Fail on unexpected top-level state rather than silently dropping an unknown future authoritative path. |
| PostgreSQL cluster bind directory | Exclude. It contains the shared cluster, not a database-scoped Mealie artifact. |
| Environment and infrastructure secret files | External bootstrap prerequisites, never artifact members. The artifact is nevertheless secret/private because `.secret`, the database, user data, integrations, and images can be sensitive. |

These paths come directly from the
[v3.22.0 directory model](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/core/settings/directories.py).
The tagged native service archives `database.json` plus regular files below
`/app/data`, while excluding its backup/temp directories, SQLite database,
logs, and ZIPs; it restores only the root `.secret` file and then replaces data
directories. The exact secret construction and uses are in
[settings.py](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/core/settings/settings.py),
[JWT security](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/core/security/security.py),
and [application session middleware](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/app.py).

## Native capability and why not to call it

The v3.22.0 admin API exposes list/create/download/upload/delete/restore
operations under `/api/admin/backups`; because its controller derives from the
admin base controller, these operations require an administrator. See the
[tagged admin routes](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/routes/admin/admin_backups.py)
and [admin dependency](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/routes/_base/base_controllers.py).

Native create produces `mealie_<version>_<timestamp>.zip` in
`/app/data/backups`, containing JSON table data and managed files. It is a
supported manual migration/recovery facility and includes recipe assets and
images, as described by the
[v3.22.0 feature documentation](https://github.com/mealie-recipes/mealie/blob/v3.22.0/docs/docs/documentation/getting-started/features.md#backups).
However, its exact source establishes four boundaries relevant here:

1. `AlchemyExporter.dump()` calls `fix_migration_data()` before reading; those
   fixers can `UPDATE`/`DELETE` and commit. Calling native create is therefore
   not a non-destructive production backup operation. See
   [the tagged fixers](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/db/fixes/fix_migration_data.py).
2. The exporter reflects and selects each table through a normal connection,
   with no explicit repeatable-read transaction or exported snapshot. The ZIP
   service then performs a separate filesystem walk. The source provides no
   single online consistency point across all tables and files.
3. Native restore drops all constraints/tables (and the `authmethod` type),
   restores database data, and only then replaces data directories. Those are
   separate destructive phases with no service-wide rollback.
4. Its PostgreSQL restore sets `session_replication_role='replica'`; the
   official guide consequently directs operators to grant and later revoke
   `SUPERUSER`, and warns that restore deletes all database data and cannot be
   undone.

Therefore `test()` and `backup()` must never call the admin create/upload/
restore APIs. The preferred contract also never uses the native restore and
never alters a production role.

## Consistency boundary and downtime

PostgreSQL documents that `pg_dump` makes a consistent single-database backup
while allowing concurrent readers and writers. That guarantee covers the
database only; it cannot make `/app/data` participate in the same transaction.
Mealie writes metadata and assets across both stores, so an online database
dump plus filesystem scan could pair a row with a missing/changed asset or an
asset with no matching database state. There is no tagged v3.22.0 maintenance
or backup primitive that proves a common snapshot across them. Source:
[PostgreSQL 16 pg_dump consistency](https://www.postgresql.org/docs/16/app-pgdump.html)
and the tagged Mealie backup source cited above.

The proposed production transaction, only after explicit approval, is:

1. acquire the target's normal Homelab Backup serialization;
2. prove the exact configured versions, database identity, and approved data
   root with read-only checks;
3. use an allowlisted lifecycle seam to stop only `mealie`, with a bounded
   timeout, and prove it is stopped; PostgreSQL remains running;
4. run PostgreSQL 16-compatible `pg_dump --format=custom` for database
   `mealie`, without `--disable-triggers`, and archive the approved `/app/data`
   members read-only while the application remains stopped;
5. validate the database archive, members, hashes, and manifest while still in
   staging;
6. restart exactly the Mealie instance stopped by this operation and require
   exact-version HTTP/database-backed readiness; and
7. atomically publish only after restart/readiness succeeds. On failure or
   cancellation, reap subprocesses, discard staging, make a bounded restart
   attempt, and return a redacted error. A restart/readiness failure must not
   publish a successful artifact.

This causes user-visible downtime and requires approval. If any other process
can write the Mealie database or data bind while `mealie` is stopped, that
writer must be identified and quiesced too; otherwise stop rather than claim a
coherent artifact.

## Minimum production access

The current backend declaration at
`/home/dev/projects/homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml`
has backup/catalog/Jellyfin mounts and unrelated networks. It has no
`/docker-apps/mealie` mount, `system_postgres_network`, database credential, or
Docker socket.

The minimum approved data plane is:

- a read-only bind of the verified `/docker-apps/mealie` root;
- reachability to `system_postgres_network` and a secret-file credential
  limited to connecting to and reading the one `mealie` database; the dump
  role needs enough `SELECT`/schema access for every current and future Mealie
  object, but no `SUPERUSER`, `CREATEDB`, `CREATEROLE`, replication, or access
  to other databases; PostgreSQL notes that `pg_dump` works by issuing
  `SELECT` statements ([diagnostics](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-DIAGNOSTICS)); and
- a purpose-built lifecycle helper or operator-owned pre/post hook allowlisted
  to inspect/stop/start only the exact `mealie` container and report state.

Using the existing Mealie database owner can be a practical first step if it
is already scoped to this database, but a tested read-only dump role is the
least-authority target. Creating/granting that role is a separate approved
infrastructure change. Do not mount the shared PostgreSQL data directory, pass
secrets in command arguments, place them in logs/metadata, request a Mealie
admin token, or mount raw `/var/run/docker.sock` merely to stop one container.
If a narrow lifecycle seam is rejected, the implementation remains blocked.

`test()` stays non-destructive: bounded `GET /api/app/about` must report exactly
3.22.0 and also exercise a database query in the route; make a separate
read-only database connection (`SELECT 1`, database identity/server version,
and the schema revision); and verify the approved filesystem root and required
directories are readable without creating anything. The endpoint and its
database dependency are defined in the
[tagged about route](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/routes/app/app_about.py).
Do not trigger native backup, perform a probe write, or stop the service from
`test()`.

## Artifact contract

Produce one private streamed archive using `create_backup_artifact()` only
after the full transaction succeeds. A concrete layout is:

```text
manifest.json
database/mealie-v3.22.0-pg16.pgcustom
data/.secret
data/recipes/...
data/users/...
data/groups/...
data/templates/...
```

The manifest records format version, Mealie semantic version/source commit and
observed immutable image ID, PostgreSQL server and client versions/image ID,
database name, Alembic revision, approved host/container path mapping,
quiescence/dump/archive/restart times in UTC, deliberate exclusions, member
counts/byte totals/modes, and SHA-256 for the database dump and every regular
file. Never include secret values, SQL, personal filenames, or absolute host
paths in logs or sidecar metadata.

Use PostgreSQL custom format because it is compressed and inspectable by
`pg_restore`; validate with `pg_restore --list`, require the expected schema and
`alembic_version`, and reject an empty/incomplete dump. PostgreSQL describes
custom format and archive inspection in [pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html).
Reject symlinks, hard links, devices, sockets, FIFOs, absolute/traversing or
duplicate archive members, changing files, unsupported modes, excessive
member/expanded-byte limits, unexpected top-level `/app/data` state, version
mismatches, or hash/count discrepancies. Do not delegate validation to
Mealie's native `shutil.unpack_archive` path, whose tagged validator checks
only for a `data` directory and `database.json`; see
[backup_file.py](https://github.com/mealie-recipes/mealie/blob/v3.22.0/mealie/services/backups_v2/backup_file.py).

## Secret-safe create-only restore contract

Declare `restore_capability = "partial"`. The plugin can safely materialize the
state into fresh local destinations, but it should not gain production
orchestrator authority just to boot an application. The disposable integration
harness owns the separate boot/readiness/content proof.

A restore must:

1. refuse unless the caller supplies the repository's exact non-production
   restore sentinel, a fresh local restore root whose `data` child does not yet
   exist, and a newly created
   isolated PostgreSQL 16 database owned by a disposable non-superuser role;
   never accept the source database name/host or overwrite an existing tree;
2. require exact Mealie version 3.22.0, the recorded compatible PostgreSQL
   version, trusted sidecar, complete manifest, safe members, and all hashes
   before making destination changes;
3. stage regular files privately, preserve `.secret` with restrictive
   permissions, and deliberately not materialize `.session_secret`; on first
   boot Mealie generates a fresh session secret, revoking old browser sessions
   while preserved `.secret` maintains access/API-token signing continuity;
4. inspect the dump table of contents, then run `pg_restore --single-transaction
   --exit-on-error --no-owner --no-privileges` into the fresh empty database;
   do not use `--clean`, `--create`, `--disable-triggers`, or a superuser;
5. verify destination file hashes and database revision/count invariants, then
   atomically rename the staged data tree into the still-fresh destination;
   on error roll back/remove only disposable destination state; and
6. return `partial` plus the exact digest-pinned disposable boot and validation
   steps. It must never contact, stop, write, or restore production.

`--single-transaction` makes the database restore all-or-nothing and implies
exit-on-error; `--no-owner` allows the destination owner to own restored
objects without superuser, and `--no-privileges` omits source grants. See
[PostgreSQL 16 pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html).
Only restore artifacts produced and authenticated by this system: PostgreSQL
warns that restore executes code selected by source superusers, so inspect the
generated SQL as part of validation and never accept an arbitrary uploaded
archive.

## Disposable exact-version two-run drill

Use temporary host directories, synthetic data and credentials, a fresh
internal-only Docker network, and immutable image digests. Publish no LAN
ports, join no production network, mount no NAS or production path, reuse no
production secret, and expose no Docker socket inside the plugin. A host-side
test harness may control only its own disposable Compose project.

Before the drill, perform the approved read-only inventory and pin the exact
running Mealie platform digest and exact PostgreSQL 16 image/minor version. If
the live Mealie image is confirmed to be the current official release image,
the v3.22.0 index/platform digests recorded above are suitable; otherwise pin
the observed immutable image after explaining the discrepancy. The unpinned
production PostgreSQL minor means an “exact deployment” drill must not proceed
until its image/version is known.

For each of two consecutive runs:

1. start a fresh digest-pinned source stack with PostgreSQL and Mealie 3.22.0;
   seed a synthetic admin/user, group and household, a recipe with an original
   image plus a separate recipe asset, user profile image, template, shopping
   list, meal-plan item, and long-lived API token; record database IDs/counts,
   Alembic revision, file byte hashes, and token behavior;
2. invoke the real plugin backup path through the proposed lifecycle seam;
   prove Mealie stopped while the dump/files were captured, then restarted and
   `/api/app/about` again reports 3.22.0;
3. require a distinct non-empty artifact/sidecar, independent artifact SHA-256,
   exact versions/digests, valid `pg_restore --list`, complete manifest/member
   hashes, and no secrets or absolute host paths in logs/metadata;
4. restore to a different fresh sentinel-marked data root and fresh disposable
   database, boot the same digest-pinned Mealie image, and prove health plus
   authenticated content access—not just container health;
5. verify exact recipe/user/group/list/plan/template counts and IDs, retrieve
   image/assets with byte equality, prove the preserved long-lived token works,
   prove a pre-backup browser session does not survive the new
   `.session_secret`, and require unchanged Alembic revision (an exact-version
   restore should need no migration); and
6. destroy every disposable container, network, volume, credential, artifact
   copy, and temp directory even after injected failure.

Between run 1 and run 2, mutate only the disposable source with a second marker
recipe/image/list item. Restore artifact A and artifact B into separate fresh
destinations and prove A contains only state A while B contains A+B. Inject at
least dump failure, filesystem-read/change detection, archive/hash failure,
timeout/cancellation, restart/readiness failure, unsafe member, unsupported
version, non-empty destination, untrusted sidecar, and SQL restore failure.
Failures must publish no artifact, leak no secret, touch no production target,
and never leave the disposable source quiesced.

## STOP conditions

Stop before implementation, a drill, or production work if any of these holds:

- The user has not explicitly approved bounded Mealie downtime and the exact
  narrowly scoped lifecycle design.
- The only proposed lifecycle path is a raw Docker socket, unrestricted remote
  shell, or another host-wide execution grant.
- `/docker-apps/mealie` cannot be mounted read-only, the one-database dump
  credential/network cannot be narrowly provided, or any proposal mounts the
  shared PostgreSQL data directory.
- The exact running Mealie image/platform digest, PostgreSQL image/minor
  version, database identity, `/app/data` mapping, or Alembic revision is not
  established by an approved read-only inventory.
- Mealie is not exactly 3.22.0, the schema is incompatible, the observed image
  differs from the expected release without explanation, or the PostgreSQL
  client is older than the server.
- Another writer can modify the database or data tree during the quiescence
  window and cannot be stopped/proven stopped.
- Unexpected top-level `/app/data` state appears and its authority/exclusion is
  not resolved; do not silently omit it.
- The implementation would call native backup (which can commit fixes), invoke
  native restore, grant PostgreSQL superuser, use a Mealie admin token, or make
  any test/probe write.
- Restart/readiness cannot be bounded and guaranteed on every backup failure or
  cancellation, or publication could occur before readiness returns.
- A restore destination is not fresh, empty, local, disposable, and sentinel-
  marked; any host/network/path/credential resembles production; the artifact
  is untrusted/unsafe; or restore would overwrite/drop existing state.
- The drill cannot pin the exact observed image digests, isolate all resources,
  validate application content on both runs, or clean up after failure.
- Any step would perform a production restore. Production restore is forbidden,
  not an approval prompt.
