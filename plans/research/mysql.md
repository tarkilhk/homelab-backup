# Oracle MySQL 8.4 current-contract recovery research

Research date: 2026-08-16
Scope: local `homelab-backup` and `homelab-infra` declarations plus Oracle,
Docker Official Images, and first-party MySQL source/documentation. No
production endpoint, host, container, or database was contacted or changed.

## Decision

Rebuild the existing `mysql` plugin as one strict **Oracle MySQL 8.4
single-schema foundation**, then let application plugins call that deep core.
Do not duplicate the database protocol in Standard Notes or another adapter,
and do not treat MariaDB as compatible evidence.

The clean target should use MySQL Shell 8.4 `util.dumpSchemas()` and
`util.loadDump()`, not publish the existing raw `mysqldump` stdout. MySQL Shell
has the first-party primitives this program needs: a consistent multi-session
snapshot, a completion marker and feature metadata, optional source/load
checksums, an incomplete/corrupt-dump error model, and a non-mutating load dry
run. Oracle documents these facilities in the
[MySQL Shell schema-dump guide](https://dev.mysql.com/doc/mysql-shell/8.4/en/mysql-shell-utilities-dump-instance-schema.html)
and
[dump-loading guide](https://dev.mysql.com/doc/mysql-shell/8.4/en/mysql-shell-utilities-load-dump.html).

There is a narrow online path without global `BACKUP_ADMIN` or `RELOAD`:

- every base table must be InnoDB;
- the backup identity has schema-scoped `LOCK TABLES`;
- `consistent: true` briefly locks the dumped tables while every worker starts
  a `REPEATABLE READ` consistent-snapshot transaction;
- after releasing those locks, MySQL Shell performs its documented extra
  consistency check because the identity deliberately lacks `BACKUP_ADMIN`;
  and
- any consistency diagnostic is fatal even though Oracle says a schema dump
  can continue while returning an error message.

This is online, but the short alignment lock can stall writers. Production
activation therefore still needs explicit approval for the new scoped
privilege and the bounded lock effect. If any table is MyISAM, MEMORY, or
another nontransactional engine, if the lock cannot be tolerated, or if the
extra consistency check cannot be interpreted fail-closed, **stop**. The
alternatives are an approved write-quiescence/downtime window or a more
powerful server-wide lock identity; neither may be inferred.

## Exact declarations and immutable candidates

The current `homelab-infra` checkout is commit
`01eae07691699a7f47a3794e9095240b672aa020`. Git proves declarations, not the
digest of an already-running container, so all runtime identities remain
candidates until an approved later read-only production check.

| Consumer | Current declaration | Boundary for this milestone |
| --- | --- | --- |
| Invoice Ninja | `mysql:8.4.0-oraclelinux8` in `docker.compose/work/invoiceninja/invoiceninja.yaml`; app now declares `invoiceninja/invoiceninja:5.13.32` | The whole `ninja` schema is authoritative database state, but the completed Invoice Ninja plugin's supported native company export remains the primary recovery contract. A full DB dump is optional defence in depth and must not be confused with that native artifact. |
| Standard Notes | `mysql:8.4.0-oraclelinux8` in `docker.compose/work/standardnotes/standardnotes.yaml` | The whole configured schema is authoritative, but it is only one member of a database-plus-uploads composite. The generic database foundation cannot make Standard Notes complete by itself. |
| WordPress | `mysql:8.4.0` in `docker.compose/work/wordpress/wordpress.yaml` | Explicitly excluded by the active program. It is version evidence only, not a reason to reactivate WordPress coverage. |
| Monica / Firefly III | MariaDB 12.3 / 12.3.2 | Outside this Oracle MySQL contract. They require MariaDB's own client, privilege, consistency, dump, and restore proof. |

Docker Hub's first-party tag API currently resolves the relevant images as
follows:

| Image | OCI index | linux/amd64 manifest | Official source |
| --- | --- | --- | --- |
| `mysql:8.4.0-oraclelinux8` | `sha256:f7a8e140a7d6d1e6e0c99eeb0489c50a186ee4ac44ff55323a176529b9a43d33` | `sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df` | [`docker-library/mysql` `c05422492215b3f0602409288c868ee4fd606ac3`](https://github.com/docker-library/mysql/commit/c05422492215b3f0602409288c868ee4fd606ac3), recorded by [Docker Official Images](https://github.com/docker-library/official-images/blob/35493c0c6d65382833eb85f1e598fd0d6ab3eaec/library/mysql) |
| `mysql:8.4.0` | `sha256:dab7049abafe3a0e12cbe5e49050cf149881c0cd9665c289e5808b9dad39c9e0` | `sha256:3e5649c69e6d75cf88fc6f8f39f877453faa4e5167b5e648007e45f54bb17f6b` | `docker-library/mysql` `319db566ac7fef45c22f3df15ee5e194a7c43259`, recorded by [Docker Official Images](https://github.com/docker-library/official-images/blob/b23c0e310a99044428046df4936655128d04e3c2/library/mysql) |

The backend currently says `FROM mysql:8.4.0 AS mysql-client` without a digest.
That line is therefore not immutable, and it copies only `mysql` and
`mysqldump`. It does not provide MySQL Shell. A rebuilt backend would currently
select the second manifest above, but Git cannot prove what an existing backend
image contains.

### Resolved MySQL Shell 8.4.0 toolchain pin

Oracle still publishes an exact Debian 12 amd64 package suitable for the
backend's `python:3.11-slim-bookworm` final stage:

| Field | Exact evidence |
| --- | --- |
| Package | `mysql-shell_8.4.0-1debian12_amd64.deb` |
| First-party URL | [`repo.mysql.com` package](https://repo.mysql.com/apt/debian/pool/mysql-8.4-lts/m/mysql-shell/mysql-shell_8.4.0-1debian12_amd64.deb) |
| SHA-256 | `5e9576a3e65d1f21d6879882e5c4e73b63b3ac49b6356a171b68b0be7f342621` |
| Oracle-published MD5 | `baf7d950bfb32bdf564e3d69009dece9` |
| HTTP byte length | `35,862,792` |
| Package control | version `8.4.0-1debian12`; architecture `amd64`; maintainer `Oracle MySQL Product Engineering Team`; installed size `304,656` KiB |
| Exact upstream source | [`mysql/mysql-shell` `ba0cd08174207752f92894f61844d2bed08d8279`](https://github.com/mysql/mysql-shell/commit/ba0cd08174207752f92894f61844d2bed08d8279) (`8.4.0` tag) |

The [Oracle archived-download listing](https://downloads.mysql.com/archives/shell/?version=8.4.0&os=21)
identifies that exact Debian 12 file, date, size, MD5, and a first-party
signature link. Two independent HTTPS streams from Oracle's APT endpoint on
the research date produced the SHA-256 above; Oracle's MD5 also matched the
response ETag. SHA-256, not MD5, is the build pin.

The package declares Bookworm-compatible floors for `libc6`, `libssl3`,
`libstdc++6`, `libcurl4`, Kerberos, SASL, SSH, readline/ncurses, tirpc/nsl,
udev, UUID, bzip2, and zlib. Therefore direct installation in the existing
Debian 12 final stage is feasible and preferable to copying an incomplete
binary/library subset from another image. The package is about 34.2 MiB
compressed and 297.5 MiB installed, so the image-size increase is explicit.

The implementation should fetch exactly that URL with Docker
`ADD --checksum=sha256:5e9576a3e65d1f21d6879882e5c4e73b63b3ac49b6356a171b68b0be7f342621`
or an equivalent download-plus-`sha256sum -c`, install the local `.deb` with
`apt-get` so Debian resolves its runtime libraries, remove the package/cache,
and fail the build unless both `dpkg-query` reports
`8.4.0-1debian12`/`amd64` and `mysqlsh --version` reports exact 8.4.0. Do not
add Oracle's live APT repository and run `apt-get install mysql-shell`: its
current index has moved to a later 8.4 patch and would defeat the exact
contract.

As of the research date, Oracle's public `mysql` Docker Hub namespace exposes
server, cluster, router, operator, and NDB-operator repositories but no
dedicated MySQL Shell repository. An operator image is not a justified source
for this client. Oracle also publishes the self-contained generic archive
`mysql-shell-8.4.0-linux-glibc2.17-x86-64bit.tar.gz` (SHA-256
`6c7d3353a09d0439704b12acfec0760242164a8a59f8709b2d81694d487243f5`,
`91,671,738` bytes), but it bundles a complete private Python 3.9 and is larger
and less naturally integrated with Bookworm package accounting. Retain it only
as independently pinned upstream evidence, not as the preferred build path.

This resolves the MySQL Shell binary/package identity gate. Full backend image
reproducibility still requires pinning the currently mutable
`python:3.11-slim-bookworm` base digest and controlling Debian dependency
resolution; the exact Shell package alone must not be used to claim an
otherwise floating image is immutable.

The local Docker Official Image resolution performed immediately after this
research closes the base-image half of that gate for the first implementation
candidate: `python:3.11-slim-bookworm` resolved to OCI index
`sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91`
and linux/amd64 manifest
`sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454`.
Its OCI annotations report Python `3.11.16-slim-bookworm`, official-images
source revision `fe89472bda6128fef7e964d1f1991534e32dcfb7`, and Debian base
manifest `sha256:362e64223cc0da95422b3b13c045186fc0a81250e765d31c025fbddf257f6143`.
Pin the linux/amd64 manifest in the local build/drill and keep the dependency
set explicit; re-resolve and redrill rather than carrying the digest across a
Python or Debian upgrade.

The Docker Hub metadata supporting the two image identities is available from
the official API for
[`8.4.0-oraclelinux8`](https://hub.docker.com/v2/repositories/library/mysql/tags/8.4.0-oraclelinux8)
and [`8.4.0`](https://hub.docker.com/v2/repositories/library/mysql/tags/8.4.0).

## Authoritative state boundary

The generic artifact covers exactly one configured, non-system schema:

- base-table definitions and every row;
- indexes, constraints, generated columns, partition definitions, table
  options, character sets and collations, and `AUTO_INCREMENT` state;
- views;
- stored procedures and functions;
- triggers; and
- Event Scheduler event definitions.

It deliberately excludes server users, roles, grants, authentication material,
the `mysql`, `sys`, `performance_schema`, and `information_schema` schemas,
tablespaces, binary/relay logs, GTID execution history, replication topology,
server configuration, logs, caches, and application files or keys. MySQL Shell
records the source GTID set as metadata but does not apply it unless explicitly
asked; restore must keep GTID update disabled. This artifact is logical
single-schema recovery, not physical, point-in-time, replication, or complete
application recovery.

For Invoice Ninja, external public/storage volumes and exact app configuration
remain outside this schema. For Standard Notes, the uploads tree and stable
encryption/deployment keys remain outside it and are mandatory composite
prerequisites. The existing
[`plans/research/standard-notes.md`](standard-notes.md) correctly keeps that
service blocked on an approved short quiescence window because MySQL and the
uploads filesystem have no shared transaction.

## Why the current `mysqldump` path is insufficient

The existing command chooses sensible individual flags:
`--single-transaction`, `--quick`, routines, events, triggers, `--hex-blob`,
`--no-tablespaces`, and `--set-gtid-purged=OFF`. Oracle confirms that:

- `--single-transaction` starts `REPEATABLE READ` and a transaction, but only
  InnoDB tables are consistent; MyISAM and MEMORY tables can change;
- concurrent `ALTER TABLE`, `CREATE TABLE`, `DROP TABLE`, `RENAME TABLE`, or
  `TRUNCATE TABLE` can make contents incorrect or make the dump fail;
- `--quick` retrieves rows a row at a time for large tables;
- `--hex-blob` encodes BINARY, VARBINARY, BLOB, BIT, spatial, and binary
  character-set values as hexadecimal;
- routines and events require explicit inclusion; and
- `--set-gtid-purged=OFF` omits both `SET @@GLOBAL.gtid_purged` and the session
  binary-log suppression statement.

Those are documented in Oracle's
[`mysqldump` reference](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html).
They do not supply an offline completeness manifest, source checksum, safe dry
run, or catalog-to-payload binding. A zero exit status and nonempty stdout are
not strict validation. The current unit test intentionally accepts the bytes
`dump data`, demonstrating that the published payload need not even be SQL.

MySQL Shell is a better clean target. Its dump directory contains DDL, chunked
data, feature/version metadata, and `@.done.json`; `checksum: true` adds
`@.checksums.json`. `util.loadDump(..., {dryRun: true})` reports dump-content
errors without importing, and the loader has explicit errors for incomplete
dumps, unsupported versions/features, invalid metadata, missing data, and
checksum failures. Source checksums are verified after a real load with
`checksum: true`. Dry-run cannot verify table-data checksums—Oracle explicitly
says that—so post-load checksum and independent marker checks remain required.

## Exact source identity and consistency contract

Use a clean-breaking flat configuration with `mode` equal to `source` or
`restore_destination`, strict host/port/database/user/password types, no
unknown fields or credential defaults, and an explicit TLS policy. Reject
system schemas and unsafe identifiers. Source `test()` and `get_status()` must
actually observe and redact:

- exact server `8.4.0`, Community distribution, linux/x86_64 identity;
- server UUID, configured schema, current authenticated identity, GTID mode,
  default engine, character set, collation, lower-case-table mode, and maximum
  packet size;
- complete base-table engine inventory, rejecting anything except InnoDB;
- tables/views, routines, triggers, events, partitions, and definers;
- exact effective grants and the absence of write/admin/file privileges; and
- exact MySQL Shell 8.4.0 client identity.

The intended source identity has only:

- `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT`, and `LOCK TABLES` on the one
  schema; and
- the dynamic `SHOW_ROUTINE` privilege if routines exist and the exact Shell
  8.4.0 drill proves it is required.

Oracle's
[privilege reference](https://dev.mysql.com/doc/refman/8.4/en/privileges-provided.html)
states that `SHOW_ROUTINE` permits routine backup without broad global
`SELECT`. Do not grant `BACKUP_ADMIN`, `RELOAD`, `PROCESS`, `FILE`, `SUPER`,
write DML, DDL, account-management, replication, or GTID privileges. The
MySQL Shell manual documents schema/table-scoped `LOCK TABLES` as the
substitute for `RELOAD` when `consistent: true`.

The exact local drill must prove this grant set rather than trusting prose. If
Shell 8.4.0 needs global `SELECT`, `RELOAD`, `BACKUP_ADMIN`, or another broad
privilege for the selected schema and object set, stop and ask. Do not silently
drop routines/events/triggers to avoid the decision.

Run `util.dumpSchemas([schema], ...)` with consistency, checksums, routines,
events, and triggers explicitly enabled, users excluded, progress hidden, and
bounded threads/chunk size. Use a private empty temporary directory. Treat
nonzero exit, cancellation, timeout, any consistency error, missing completion
metadata, source catalog drift, or unexpected warning as failure and publish
nothing. MySQL Shell converts unsafe text-form types such as BLOB to Base64 and
documents a per-value limit of about 0.74 times destination
`max_allowed_packet`; probe and drill that boundary rather than claiming
unbounded large-object support.

`LOCK INSTANCE FOR BACKUP` remains a rejected default. Oracle documents that
it permits DML while blocking operations that can invalidate a physical
snapshot, but it requires global `BACKUP_ADMIN` and affects the whole server
([statement reference](https://dev.mysql.com/doc/refman/8.4/en/lock-instance-for-backup.html)).
It is a future explicitly approved production option, not a fallback.

## Strict artifact contract

Publish one mode-0600 transactional tar artifact containing exactly:

1. a small Homelab Backup `manifest.json`; and
2. one private `mysql-shell/` dump tree enumerated by that manifest.

The outer manifest binds format version, exact MySQL/MySQL Shell versions,
source identity and catalog hashes, every member's normalized relative path,
type, size, and SHA-256, aggregate/member counts, the Shell completion and
checksum metadata hashes, dump options, and a validation result. It contains
no password, URI, host, user, application row, object name, raw DDL, private
path, or GTID value. Sidecar evidence should be still smaller: versions,
aggregate structural counts/hashes, and the validator version only.

Before publication and again before restore:

- bind and read one regular artifact descriptor; reject replacement races;
- enforce total/member-count, path-depth, filename, per-member, compressed,
  expanded, and ratio limits;
- accept only regular files/directories; reject links, devices, FIFOs,
  sockets, absolute/traversal paths, duplicate/case-colliding names, sparse or
  trailing data, and unexpected members;
- verify every manifest size/hash and the exact Shell completion/checksum
  metadata;
- extract only into a private newly created staging directory; and
- run exact MySQL Shell 8.4.0 `loadDump` dry-run against the authorized fresh
  destination before any import.

This catches truncation, mutation, missing chunks, malformed metadata, unknown
features, and incompatible DDL without executing artifact SQL. It does not
pretend dry-run proves row bytes; real create-only load plus Shell checksum and
semantic verification do that. Raw legacy `.sql` is a different artifact
format and must not be accepted through a compatibility fallback without the
user's explicit approval.

## Create-only isolated restore

Restore remains `partial`. MySQL DDL is atomic per supported statement but is
not transactional across the whole load and implicitly commits active
transactions
([Oracle atomic-DDL reference](https://dev.mysql.com/doc/refman/8.4/en/atomic-ddl.html)).
The plugin cannot roll back a multi-file schema load or prove the surrounding
application, external files, and configuration. Any failed destination is
invalid and must be destroyed before retrying.

Before loading, require all of the following:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- an exact destination allowlist;
- `mode=restore_destination` and source-run provenance from `RestoreService`;
- distinct source/destination target IDs and distinct observed server UUIDs;
- exact MySQL 8.4.0 and MySQL Shell 8.4.0;
- the original schema name is absent, not merely empty; and
- no source endpoint, production server, host network, or shared destination
  server is reachable from the restore topology.

Requiring an absent schema makes the operation genuinely create-only and
avoids unsafe schema renaming: Oracle warns that loading under a different
schema name does not rewrite qualified references in views or stored programs.
Use a narrowly privileged restore identity only on the disposable server.
Never enable ignore-existing, force/continue, GTID update, user loading,
binary-log suppression, compatibility rewrites, definer stripping, or data
coercion as a fallback. Definers must be satisfiable by deliberately created
isolated test accounts; otherwise stop.

Run dry-run first, then real `loadDump` with `checksum: true`. After load,
require the expected source/destination catalog hash, object counts and
definitions, table/partition checksums, row counts, phase markers, binary-byte
hashes, view/routine/trigger/event behavior, and destination server restart.
`mysqlcheck` alone is not completion evidence. In addition, the current
backend's `mysqlcheck` is a symlink to Debian `mariadb-check`, so it is not an
exact Oracle MySQL 8.4 validator and must not anchor this milestone.

## Exact two-clean-round drill

The opt-in drill should use the deployed linux/amd64 server manifest
`sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df`
and the eventually pinned exact MySQL Shell 8.4.0 toolchain. Workloads and the
runner use a private internal network, no published ports, no host network,
Docker socket, privileged mode, production mount, or production credential.

Each clean round performs the complete sequence, not half a sequence per run:

1. Create a fresh source, schema-scoped read-only backup identity, separate
   application/definer identity, and synthetic phase A through supported SQL.
   Cover parent/child rows and FK, generated value, unique/check/index state,
   `AUTO_INCREMENT`, UTF-8/null/escaping, view, procedure/function, trigger,
   disabled/far-future event, partition, BIT/spatial value, and a deterministic
   BLOB larger than the ordinary 16 MiB client default but inside the probed
   Shell/server bound.
2. Execute a real scheduled Target/Job/Run/TargetRun backup. Validate the
   private artifact, sidecar, independent size/SHA-256, Shell metadata,
   completion marker, member hashes, catalog, checksum set, secret absence,
   and denied source writes.
3. Mutate through the application identity to cumulative phase B and make a
   second scheduled artifact. Prove A remains immutable and A/B paths, sizes,
   hashes, manifests, and semantic markers differ.
4. Restore A and B through `RestoreService` into two independent fresh exact
   MySQL servers with the same original schema name. Verify Shell checksums,
   every phase-specific object/data/binary marker and real query behavior.
5. Restart each destination, repeat identity/readiness and semantic checks,
   and prove A excludes B while B contains A plus B.

Run that full A/B sequence twice from clean state. The generic drill proves the
database boundary only. A thin application adapter or composite must separately
boot its exact application image and prove supported reads before claiming
complete service recovery; database readiness must not be promoted into a
Standard Notes or Invoice Ninja application claim.

Representative exact failures include non-InnoDB source, missing privilege,
write-capable source identity, Shell/client/server drift, injected consistency
failure, dump timeout/cancellation, warning, missing completion/checksum/member,
corrupt compressed chunk, altered outer manifest or sidecar, resource-limit
breach, wrong plugin/version, unauthorized or same-server restore, existing
schema, incompatible definer, dry-run error, load error, and post-load checksum
or marker mismatch. Every path must leave no published partial artifact and no
container, network, volume, listener, staging tree, synthetic credential, or
runner image with the drill prefix/label.

## Generic foundation and thin consumers

Follow the PostgreSQL precedent: place the bounded client, identity/catalog,
Shell dump-tree, archive, authorization, validation, and restore machinery in
`app.core.plugins.mysql`. Keep `app.plugins.mysql` a small adapter over the
public plugin interface.

- Standard Notes should call this core from its already specified quiesced
  database-plus-uploads composite; it must not create a second MySQL dumper.
- Invoice Ninja should retain its supported native export/import plugin. If a
  full-schema defence-in-depth target is later wanted, it should add only an
  exact app catalog/readiness profile over the same core.
- Oracle MySQL consumers may add exact catalog/version/marker profiles without
  weakening the foundation.
- Monica and Firefly III must use a separate MariaDB core and evidence.

This makes the module deep: one strict protocol and artifact contract, thin
service-specific semantics, no false cross-vendor abstraction.

## Concrete current repository gaps

The current implementation is a legacy baseline, not current-contract proof:

- `schema.json` permits unknown fields and supplies dangerous `root`,
  `secret`, and `mysql` defaults; it has no source/destination mode or strict
  bounds.
- configuration validation accepts coercible ports and does not constrain
  host, identifier, TLS, or unknown keys.
- `test()` proves only `SELECT 1`; `get_status()` always returns `unknown`.
- credentials are placed in `MYSQL_PWD`; the new Shell boundary needs an
  exact secret-safe input path with no argv, environment, logs, or residue.
- backup publishes arbitrary nonempty stdout after only a process return-code
  check; it does not reject warnings, non-InnoDB tables, DDL/consistency drift,
  malformed/truncated content, missing objects, wrong versions, or bad grants.
- sidecars lack exact server/client, catalog, dump-completion, and checksum
  evidence.
- restore accepts any existing path and executes it before vendor validation.
  It has no hard isolated-restore environment/allowlist, mode, provenance,
  source/destination identity check, sidecar/plugin binding, or same-server
  refusal.
- an empty schema is the only destructive preflight; arbitrary SQL can target
  other schemas/server state according to the restore identity's grants.
- a failed load leaves partial DDL/data, and the MariaDB `mysqlcheck` alias plus
  table count does not prove rows or non-table objects.
- unit tests are subprocess mocks and even accept `dump data`; no exact MySQL
  plugin Docker drill, scheduler path, `RestoreService` recovery, A/B phase,
  restart, privilege, corruption, large/binary, or cleanup proof exists.
- `frontend/src/mocks/handlers.ts` still calls MySQL restore `automatic`, while
  the backend and capability registry correctly call it `partial`.
- `docs/PLUGIN_COMPATIBILITY.md` says two validated logical dumps and one
  `mysqlcheck` restore passed, but the repository retains no exact current
  MySQL A/B-to-two-fresh-destinations test/evidence capable of supporting that
  stronger wording. The coverage ledger correctly leaves current-contract
  MySQL `planned-plugin`.

## Production gates and STOP conditions

Local implementation and exact Docker drills may proceed without production
contact. Production remains backup-only and needs separate explicit approval
for a new target/configuration, scoped backup identity, `LOCK TABLES` grant,
network attachment, schedule, and the brief writer-stall characteristic. A
later approved read-only probe must confirm the exact runtime image/server
version, schema, engines, objects, grants, packet limit, TLS behavior, and
application role. No production restore is permitted.

Stop rather than weakening the result if:

- any base table is not InnoDB;
- the exact Shell consistency algorithm cannot run with schema-scoped
  privileges or reports uncertainty;
- production cannot tolerate the short alignment lock;
- exact routines/events/triggers require broad global data/admin access;
- the exact MySQL Shell package is unavailable, changes from the recorded
  SHA-256, fails its version/architecture assertion, or cannot run with the
  pinned Bookworm dependency set;
- a value exceeds the proved packet/Base64 bound;
- source/destination identity, artifact completeness, catalog, checksum,
  definer, or readiness cannot be proved;
- the destination is existing, shared, same-server, reachable as production,
  or not destroy-and-recreate disposable;
- Standard Notes is requested without its uploads/key boundary and explicit
  quiescence approval;
- MariaDB behavior is requested through this Oracle adapter;
- production write, restore, downtime, broad privilege, or lifecycle control
  would be required without approval; or
- a legacy raw-SQL format, field, alias, version fallback, or compatibility
  rewrite is requested without explicit approval.
