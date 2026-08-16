# Plan 021: Revalidate Jellyfin 10.11.11 recovery

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation; recovery-boundary, downtime, and
  credential-authority decisions
- **State**: BLOCKED
- **Production status**: research/local planning only; every production restore
  remains forbidden
- **Fixed point**: `83006fa`

## Outcome

Replace the legacy Jellyfin adapter only after the user selects one honest
recovery boundary and authorizes its consistency and credential model. The
exact deployed declaration is Jellyfin 10.11.11. Local work must pin:

- OCI index
  `sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db`;
- linux/amd64 manifest
  `sha256:0b901391a662862eddb5dc55d244d7883cbb6236ef5b9a6ea82abc78a89819f0`;
- server source `1fbd8739292cce610231be93daf43368733edf63`;
- packaging source `a5c7e85a759ca5b038f943033f05be695fe7c16e`; and
- native backup-engine version `0.2.0`.

The authoritative source analysis, archive inventory, API behavior, restore
limitations, and drill alternatives are documented in
`plans/research/jellyfin.md`.

## Blocking decisions

### Recovery boundary and consistency

Choose one:

1. **Recommended full boundary:** approve a short scheduled Jellyfin outage and
   a narrow mechanism that exposes a stopped, immutable, read-only projection
   of the complete `/config` tree. Exclude backups, locks, logs, caches,
   transcodes, trickplay, generated subtitles, and media payloads only after
   exact path and authority review.
2. **Deliberately partial native boundary:** explicitly accept that the
   supported online archive is not one transaction across its database and
   copied files, and classify omitted state after an inventory. This is a
   weaker policy exception, not a strict point-in-time backup.
3. **Proved live fence:** design and approve an external mechanism that blocks
   every user, administrative, scan, scheduled-task, plugin, metadata, and
   configuration mutation for the full native-create interval. Jellyfin does
   not provide this fence itself.

Two equal consecutive native archives do not convert sequential reads into a
transactional snapshot. Do not infer approval for downtime, lifecycle control,
or a broad `/config` mount from the existence of the current shared backup
directory.

### Omitted authoritative state

The native archive omits `/config/plugins`, plugin configuration,
`/config/data/device.txt`, arbitrary data files, and custom metadata unless its
optional metadata flag is selected. Git cannot prove whether those stores are
active or rebuildable in production.

Before a native subset is selected, perform a separately approved read-only
inventory and classify every omitted store as included elsewhere,
rebuildable, external, or deliberately unprotected. Otherwise select the full
stopped `/config` boundary.

### Credential authority

Jellyfin 10.11.11 maps every API key to Administrator and documents API keys as
unrestricted. Native backup and restore endpoints require elevation. Choose:

1. explicitly approve a dedicated global administrative API key; or
2. approve the recommended proxy-held key. Homelab Backup receives only a
   distinct proxy credential; the proxy injects the Jellyfin key, permits exact
   create-only methods, paths, and request bodies, and makes the direct
   Jellyfin origin unreachable.

Production must never expose `POST /Backup/Restore` through the create-only
proxy. A proxy solves credential scope only; it does not solve consistency or
omitted state.

## Contract after decisions

### Full stopped-filesystem option

Use a clean-breaking source schema that identifies the exact source and
explicitly opts into scheduled stop/start. The production mechanism must:

1. prove exact running container/image, complete `/config` mount, active
   plugins, effective paths, and readiness without mutation;
2. serialize by resolved service identity;
3. stop Jellyfin to a fixed deadline and prove it stopped;
4. snapshot or stream only the reviewed full authoritative projection through
   a narrow helper with no shell, Docker socket, media mount, or unrelated host
   access;
5. create and strictly validate one private versioned artifact plus sidecar;
6. restart in cancellation-shielded cleanup and prove exact readiness before
   publication; and
7. fail critically if restart or cleanup cannot be confirmed.

Restore is create-only and local. Materialize the complete tree only into a
new empty volume while Jellyfin is stopped, then start the exact image and
prove device identity, plugins/configuration, users, libraries, collections,
playlists, custom metadata, and database markers. Restart again and repeat the
proof. This option may be `automatic` only for the reviewed complete
application-control-plane boundary.

### Partial native option

If the user explicitly accepts the weaker boundary, require a flat source or
restore-destination mode schema with canonical origin, secret credential,
fixed narrow read-only native-backup mount, exact selected options, and no
legacy defaults or aliases. The capability is `partial`.

`test()` and status use unauthenticated `GET /System/Info/Public` for exact
10.11.11 readiness plus the approved credential-boundary and mount checks.
Backup must:

1. serialize by canonical application origin;
2. capture the complete baseline native-backup identity set;
3. call exact `POST /Backup/Create` with fixed approved options;
4. wait for the synchronous response, require exact server/engine/options and
   a uniquely new safe basename, and map it to one regular read-only source;
5. stream and strictly validate the closed file under one deadline;
6. publish a mode-0600 artifact and sidecar transactionally; and
7. delete only the proven new server archive after durable publication, if
   that cleanup is separately approved.

Reject redirects, version/engine/option drift, same-second collisions,
pre-existing or swapped files, and any secret/path/content leakage.

## Strict native artifact contract

Before publication and again from the RestoreService-staged descriptor, bound
and validate:

- ZIP central directory, CRC, trailer, member/count/size/ratio/depth/deadline
  ceilings;
- regular unique members only, with no encryption, links, devices, traversal,
  absolute/drive/backslash/control names, or normalized collisions;
- exactly one bounded `manifest.json` with server `10.11.11`, engine `0.2.0`,
  sane UTC creation time, and exactly the approved flags;
- complete unique `DatabaseTables`, migration-history member, and one bounded
  JSON-array member for every declared table;
- the exact expected Config, Root, collections, playlists, and scheduled-task
  projections, plus only approved optional trees; and
- a canonical secret-free structural projection for later round-trip proof.

The API response and archive manifests must agree. Sidecars may contain only
exact component identities, options, aggregate counts, validator version,
artifact size/hash, and canonical projection hash. They must not contain API
keys, origins, server paths, users, device IDs, media paths, titles, or member
contents.

## Native restore safety

The native endpoint returns 204 after scheduling a restart; restore occurs on
startup, overwrites represented files, purges/repopulates tables without one
encompassing transaction, leaves unrepresented files in place, and exposes no
terminal result or rollback. Therefore native restore is allowed only when:

- RestoreService supplied the private size/hash-verified staged inode and
  matching sidecar/provenance;
- source and destination target/origin/credentials differ;
- an explicit local-only flag and exact destination-origin allowlist pass;
- the exact destination has a new empty volume, no production connectivity,
  published port, shared mount, or pre-existing state; and
- the selected native subset and bootstrap assumptions are explicit.

Require an observed unavailable-to-ready transition, exact version, restored
source identity, representative users/policies/configuration/libraries/
collections/playlists markers, and an independently created post-restore
native archive whose canonical projection matches. Restart the destination and
repeat all readiness/content assertions. Any failed or ambiguous destination
is destroyed, never retried or called successful.

## TDD slices after approval

1. Discovery, exact capability, strict schema/config, public API, and target
   persistence.
2. Exact non-destructive status, source identity, mount, authority, and
   secret-safe failures.
3. Consistency/lifecycle boundary selected above, bounded cancellation, and
   same-source serialization.
4. Native attribution and streamed transactional publication, or stopped full
   projection capture.
5. Strict archive/manifest/table/file/resource validation and sidecar binding.
6. RestoreService provenance, local/fresh authorization, destructive boundary,
   readiness, semantic proof, and failure auditing.
7. Two clean exact-image A/B rounds, reviews, repository gates, docs, and one
   focused commit.

Use one public RED-to-GREEN tracer bullet at a time. Do not preserve the fake
API-key default, permissive redirects, arbitrary version acceptance, weak ZIP
validator, no-restart `partial` fallback, or unconditional legacy behavior.

## Exact local drill

Run the selected contract twice from clean state against only the immutable
amd64 manifest, using internal disposable networks, no published ports, unique
synthetic credentials, distinct volumes, no production route/socket/mount, and
complete teardown audits.

Each round creates phase A and cumulative phase B with distinct users,
permissions, configuration, library roots, collections/playlists, database/API
key state, custom metadata, and—when the full boundary is selected—plugin and
device-identity markers. Produce distinct private A/B artifacts and sidecars,
independently verify size/hash/content/immutability, destroy the source, and
restore A and B through RestoreService into two independent fresh exact-image
destinations. Prove phase separation, canonical content, exact readiness, and
second-restart persistence.

Exercise representative identity, authority/proxy, consistency, collision,
mount, archive/table/resource, provenance, same/nonfresh destination, no-
restart, readiness-without-markers, cancellation, cleanup, and partial-mutation
failures. Four artifacts and four independent fresh restores are required.

## Verification and completion

- focused plugin/API/scheduler/RestoreService/artifact/hygiene tests;
- two complete exact-image A/B recovery rounds;
- full backend and frontend tests, lint, build, mypy, Black, and isort;
- SemVer, diff, secret, and disposable-resource audits;
- final independent Standards and Spec reviews with no unresolved P0-P3 issue;
- compatibility, recovery, changelog, ledger, research, plan, and README
  evidence current; and
- one focused milestone commit, with no push, deploy, target change, credential
  creation, or production action.

Mark `DONE (local)` only after all user decisions are recorded and every gate
passes. Production activation separately requires immutable runtime identity,
the approved lifecycle or proxy/mount design, exact target/schedule, and a
backup-only validation. Production restore remains forbidden.

## STOP conditions

Stop rather than weaken the result when the boundary, omitted-state inventory,
credential authority, consistency policy, or restore claim is undecided; exact
identity, engine, layout, options, or paths drift; active plugin/custom metadata
state would be omitted; quiescence or restart cannot be proved; the artifact
cannot be strictly bound and validated; restore is not exact, fresh, isolated,
disposable, and content-verifiable; a direct global key, broad mount, Docker
socket, lifecycle change, production write/restore, or compatibility path would
be introduced without explicit approval.
