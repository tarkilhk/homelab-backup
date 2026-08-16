# Plan 019: Revalidate Oracle MySQL 8.4 schema recovery

## Status

- **Priority**: P0
- **Effort**: XL
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: BLOCKED — pending privilege/consistency decision
- **Restore capability**: `partial`
- **Production status**: local work only; every production restore is forbidden
- **Fixed point**: `463703e`

## Outcome

Determine an approved strict Oracle MySQL 8.4 single-schema recovery boundary,
then replace the legacy raw-SQL `mysql` plugin only if that boundary can be
implemented without weakening consistency or least privilege. The original
candidate used first-party MySQL Shell 8.4.0 `util.dumpSchemas()` and
`util.loadDump()`, but the exact pinned local probe disproved its proposed
schema-only privilege contract. No implementation may publish a Shell dump
until the decision gate below is resolved.

If a safe boundary is approved, prove two clean rounds, each with distinct
phase-A/phase-B scheduled artifacts and two independently fresh create-only
restores on immutable linux/amd64 MySQL 8.4.0 servers.

This is not a MariaDB abstraction. Standard Notes may later consume this deep
core as one member of its separately quiesced database-plus-uploads composite;
Invoice Ninja keeps its supported native export/import as the primary contract;
WordPress remains retired.

Primary-source evidence, exact image/package identities, consistency semantics,
least privilege, and production gates are in `plans/research/mysql.md`.

## Immutable toolchain

Pin and assert all of the following before plugin behavior:

- source/drill server:
  `mysql@sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df`
  (`8.4.0-oraclelinux8`, linux/amd64);
- backend base:
  `python@sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454`
  (Python 3.11.16 slim Bookworm, linux/amd64); and
- Oracle package `mysql-shell_8.4.0-1debian12_amd64.deb`, byte length
  `35,862,792`, SHA-256
  `5e9576a3e65d1f21d6879882e5c4e73b63b3ac49b6356a171b68b0be7f342621`,
  source `ba0cd08174207752f92894f61844d2bed08d8279`.

Use Docker checksum verification or an equivalent local checksum gate; never
install from a moving Oracle APT index. Fail the build unless package metadata
is exactly `8.4.0-1debian12`/`amd64` and `mysqlsh --version` is exactly 8.4.0.

## Deep-module boundary

Create `app.core.plugins.mysql` for all client, identity/catalog, private auth,
Shell dump tree, manifest/tar, validation, authorization, deadline/cancellation,
and create-only load mechanics. Keep `app.plugins.mysql` a thin public adapter
owning schema/configuration, generic marker policy, logging, and honest `partial`
outcomes. Do not retain the raw `.sql`, `MYSQL_PWD`, `mysqlcheck`, loose schema,
or URL/default/alias behavior without explicit compatibility approval.

## Exact configuration and public seams

Use one flat clean-breaking schema:

- `mode`: exactly `source` or `restore_destination`;
- `host`: strict hostname/IP text without scheme, path, whitespace, or controls;
- `port`: exact integer `1..65535`;
- `database`: safe non-system MySQL identifier;
- `user` and `password`: nonempty strict strings with no defaults; and
- `ssl_mode`: exactly `REQUIRED` or `DISABLED`, with no implicit downgrade.

Reject unknown keys, coercions, legacy URL/options, system schemas, and inactive
fallback fields. Cover loader and `/api/v1/plugins`, schema, target persistence,
public `/test`, scheduled Target/Job/Run/TargetRun backup, truthful status, and
RestoreService staging/audit.

## Source identity and online consistency

`test()` and every backup attempt must run the exact Shell/client and observe:

- MySQL Community Server exactly 8.4.0 on linux/x86_64;
- exact configured schema, server UUID, GTID mode, default engine, character
  set/collation, lower-case-table mode, and packet limit;
- complete base tables, engines, views, routines, triggers, events, partitions,
  definers, constraints/indexes, generated columns, and AUTO_INCREMENT state;
- every base table is InnoDB and zero unsupported/system state is selected; and
- the effective identity has only schema-scoped `SELECT`, `SHOW VIEW`,
  `TRIGGER`, `EVENT`, and `LOCK TABLES`, plus dynamic `SHOW_ROUTINE` only when
  exact routine proof requires it.

Reject write/DDL/account/replication/file/admin privileges, global data access,
`RELOAD`, `BACKUP_ADMIN`, `PROCESS`, `FILE`, and `SUPER`.

The exact pinned local probe established that this proposed grant set is not
sufficient for warning-free `util.dumpSchemas()`:

1. the schema-only identity failed before dumping because Shell attempted role
   introspection;
2. adding read access to `mysql.default_roles` allowed the utility to continue;
3. Shell then reported that it could not acquire a global read lock, could not
   lock the `mysql` system tables, could not read binary-log coordinates, and
   that consistency could not be guaranteed; and
4. Shell exited zero after writing a dump despite those warnings, so exit status
   and completion metadata alone cannot establish this contract.

The strict worker correctly rejected that result because warnings and an
explicit consistency failure are fatal. Satisfying the observed path requires
some combination of role/system-schema reads or locks and global `RELOAD`,
`REPLICATION CLIENT`, or `BACKUP_ADMIN` authority. Those privileges exceed the
approved contract and must not be added implicitly.

### Required user decision

Choose one before this milestone resumes:

- approve a separately audited broader MySQL Shell identity and its production
  risk;
- approve bounded application quiescence and redesign the contract around a
  strict logical dump that does not claim unsupported online consistency; or
- classify the generic online MySQL foundation as blocked and leave
  application recovery to the already selected native/composite boundaries.

Until then, keep the worker/artifact work uncommitted, publish no Shell artifact,
and make no production grant, target, schedule, downtime, or connectivity
change. Any non-InnoDB table or ambiguous consistency result remains a STOP.

## Artifact and validation contract

Publish one mode-0600 transactional tar containing only:

1. `manifest.json`; and
2. the exact private `mysql-shell/` dump tree enumerated by that manifest.

The manifest binds format/validator version, exact server/Shell identity,
secret-free source/catalog hashes, dump options, structural counts, completion
and checksum metadata hashes, and every member's normalized path/type/size/
SHA-256. Sidecars contain only aggregate versions, counts, hashes, and validation
identity. They never contain host/user/password, URI, GTID, object/row names,
DDL, paths, or application content.

Stream and bound creation/inspection. Reject empty/incomplete dumps, missing or
extra members, links/devices/FIFOs/sockets, sparse files, absolute/traversal or
case-colliding paths, duplicate names, unsafe tar headers, unsupported
compression, trailing data, member/aggregate/expanded/ratio/depth/count limits,
manifest mismatch, missing `@.done.json`/`@.checksums.json`, Shell feature drift,
and source catalog drift. Bind one descriptor across validation/publication.

## Create-only isolated restore

Restore remains `partial` because MySQL cannot roll back a multi-file schema
load and the plugin does not restore applications, files, keys, users/grants,
or lifecycle. A failed disposable destination must be destroyed before retry.

Require before any destination mutation:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- exact MySQL-specific destination allowlist;
- RestoreService-staged size/hash/sidecar/source provenance;
- `mode=restore_destination`, distinct target IDs and observed server UUIDs;
- exact MySQL/MySQL Shell identities and network-isolated destination; and
- the original schema is absent, not merely empty.

Validate the descriptor and archive fully, extract into parent-owned private
staging, and run exact `util.loadDump(..., {dryRun:true})` against the authorized
fresh server before import. Then load with checksums enabled and with users,
GTID updates, force/continue, ignore-existing, binary-log suppression, definer
rewrites, compatibility transformations, and coercion disabled.

After load, require source/destination catalog equality, Shell checksums, object
definitions/counts, table/partition checksums, row counts, semantic phase
markers, binary hashes, view/routine/trigger/event behavior, and server restart.
Return `partial` with explicit external prerequisites.

## Vertical TDD slices

1. Pin the backend base and exact Shell package; add repository-hygiene and
   executable-version tests before Dockerfile changes.
2. Discovery, strict schema/config, partial capability, public target/API
   persistence, and removal of frontend's false automatic mock claim.
3. Secret-safe exact source probe/status, catalog/engine/grant identity, and
   all-InnoDB/least-privilege failures through the public test seam.
4. Parent-owned Shell worker workspace, private option input, fixed script/
   argv/env, one cumulative deadline, warning/consistency/error handling, and
   termination/reap on timeout or repeated cancellation. This slice is locally
   unit-green but deliberately uncommitted at the privilege decision gate.
5. Strict bounded manifest/tar validator and real scheduled private backup with
   sidecar, stable pre/post catalog fence, unique publication, and no residue.
6. Restore authorization, descriptor/provenance binding, absent-schema preflight,
   dry-run-before-load, same-server refusal, cancellation, and partial audit via
   real RestoreService.
7. Post-load catalog/checksum/semantic validation, restart persistence, and
   explicit failed-destination semantics.
8. Immutable two-clean-round exact Docker drill plus all repository, SemVer,
   review, documentation, and focused-commit gates.

Work red to green one public vertical slice at a time. Private helper tests are
reserved for bounded archive/parser/process cases that cannot safely be produced
through public seams.

## Exact two-clean-round drill

Use unique internal Docker networks, synthetic credentials/data, no published
ports/host network/socket/privileged workload/production route, and no host mount
except a narrow artifact root exposed read-only where possible. Each of two
clean parameterized rounds must:

1. boot exact MySQL 8.4.0, create the schema-scoped backup identity and a separate
   application/definer identity, and prove all positive/negative grants;
2. seed phase A through supported SQL with parent/child FK rows, generated and
   indexed/check/unique state, AUTO_INCREMENT, UTF-8/null/escaping, view,
   procedure/function, trigger, disabled future event, partition, BIT/spatial,
   and deterministic BLOB data inside the probed Shell/server limit;
3. create artifact A through a real scheduled run and independently prove its
   private mode, sidecar, size/SHA-256, manifest/tree/checksum/completion metadata,
   catalog, semantic markers, and secret absence;
4. mutate cumulatively to phase B, create distinct artifact B, and rehash A to
   prove immutability and phase separation;
5. restore A and B through RestoreService into two separate fresh exact servers,
   verify every object/data/binary/behavior marker and Shell checksum, restart
   each server, and repeat identity/readiness/semantic proof; and
6. exercise representative exact non-InnoDB, privilege, server/Shell drift,
   consistency diagnostic, cancellation/timeout, missing/corrupt member/checksum,
   tampered manifest/sidecar, resource bound, unauthorized/same-server/existing
   schema, dry-run/load/checksum/marker failure, and full label/prefix cleanup.

The drill proves only generic schema recovery. It cannot promote Standard Notes
or Invoice Ninja application recovery without their own exact application/composite
evidence.

## Verification and completion

- focused MySQL, artifact, API, scheduler, RestoreService, and hygiene tests;
- two complete exact clean drill rounds;
- full backend and frontend tests/lint/build;
- application mypy, changed-file Black/isort, version, diff, and secret scan;
- exact image/package health and independent cleanup audit;
- final Standards and Spec reviews with no unresolved P0-P3 issue;
- compatibility, recovery, changelog, ledger, research, plan, and README current;
- one focused milestone commit; no push or deploy.

Mark `DONE (local)` only after every item passes. Production remains gated on
read-only runtime inventory plus explicit approval for network/target/schedule,
the schema-scoped identity and `LOCK TABLES` grant, brief writer stall, and one
backup-only run. Production restore is forbidden.

The current exact result does not satisfy these completion conditions. Do not
mark the plan `DONE (local)` or claim a consistent artifact until the required
user decision is recorded and the selected boundary passes a fresh exact drill.

## STOP conditions

Stop rather than weaken the contract if any table is non-InnoDB; Shell cannot
obtain/prove consistent snapshots with schema-scoped privilege; warnings or
consistency results are ambiguous; the short lock is unacceptable; routines or
other objects need broad/global privilege; exact tool/package/base identities
cannot be pinned; secret input cannot avoid argv/env/log/residue; data exceeds
the proved packet/Base64 limit; artifact/catalog/checksum/definer/readiness
cannot be bound; the destination is existing/shared/same-server/non-disposable;
MariaDB or a Standard Notes composite is requested through this generic adapter;
or production change, restore, downtime, broader privilege, or compatibility is
required without explicit approval.
