# Plan 022: Revalidate Radarr, Sonarr, and Lidarr native recovery

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation and the Plan 014 hardened `ServarrPlugin`
- **State**: IN PROGRESS
- **Production status**: local implementation and recovery proof only;
  production activation requires explicit API-key, read-only-mount, and native
  cleanup approval. Production restore is forbidden.
- **Fixed point**: `5c6459f`

## Outcome

Replace the three legacy adapters with exact, thin, clean-breaking declarations
on the shared Servarr recovery module. Implement and prove:

| Application | API | Version | Package | Migration | Database | Fixed backend mount |
| --- | --- | --- | --- | --- | --- | --- |
| Radarr | `/api/v3` | `6.3.0.10514` | `6.3.0.10514-ls313` | 242 | `radarr.db` | `/sources/radarr/backups` |
| Sonarr | `/api/v3` | `4.0.19.2979` | `4.0.19.2979-ls320` | 217 | `sonarr.db` | `/sources/sonarr/backups` |
| Lidarr | `/api/v1` | `3.1.0.4875` | `3.1.0.4875-ls38` | 80 | `lidarr.db` | `/sources/lidarr/backups` |

The exact OCI/source identities, native protocol, archive structure, control-
plane boundary, and acceptance matrix are documented in
`plans/research/radarr-sonarr-lidarr.md`.

All three capabilities are `automatic` only for their named application
control planes. Movies, episodes, music, downloads, queues, provider state,
logs, caches, application binaries, container state, and external services are
excluded and remain recovery prerequisites where applicable.

## Immutable identities

Local drills use only these linux/amd64 manifests:

- Radarr
  `sha256:263be1036419fcb38fc1cf76be90db8db4b0dc49fd492617b17cc58e9e0bf1b5`;
- Sonarr
  `sha256:f6bf16c4c5a0c6c99833eab891671ded0f06f553f30c7b0702e98f455c5642cc`;
- Lidarr
  `sha256:0199ff56d973da7b66158ba8823cf3eac905d47b6ab7524d213931debfa75225`.

Also assert exact OCI architecture, full LSIO package label, image-source
revision, application version, API prefix, SQLite backend, and migration. The
deployed tags and OCI indexes are declaration/distribution evidence, not drill
identity. Production runtime bytes remain unproven until a separately approved
read-only inventory and immutable infrastructure pin.

## Public seams and deep-module boundary

The pre-agreed test seams are:

1. loader discovery, `/api/v1/plugins`, schema API, and TargetService
   persistence;
2. public `test(config)` for non-destructive exact status/list/mount proof;
3. public `backup(BackupContext)` for the complete native state machine and
   transactional artifact/sidecar;
4. public `restore(RestoreContext)` through real `RestoreService` for isolated
   destructive recovery and audit; and
5. one opt-in exact-image Docker drill exercising all adapters from clean state.

Keep `ServarrPlugin` as the deep module. Add only one optional exact
`packageVersion` invariant if needed. The adapters declare version, package,
migration, database, mount, required tables, and fresh-resource paths; they do
not duplicate HTTP, process, filesystem, archive, or restore logic.

Do not test implementation-private call order. Helper-level tests are reserved
for malicious archive/resource inputs or process-lifecycle failures that cannot
be safely produced through a public seam.

## Clean-breaking configuration

Each flat schema requires exactly `base_url`, secret `api_key`, and
`backup_directory`, rejects additional fields, and exposes the fixed mount as
both default and `const`. Remove every fake API-key default.

Validation rejects wrong/coerced values, whitespace, URL credentials, query,
fragment, path, unsafe scheme, malformed port, control characters, relative or
traversing paths, and broad roots. The backup directory must equal the adapter's
fixed absolute path, be a real dedicated read-only mount, and never be
`/config`, `/backups`, `/app`, a parent mount, or a symlink.

Remove without compatibility:

- Radarr and Sonarr `nzbdrone.db` aliases;
- mount-less configuration and HTTP archive download;
- UI cookies, API-key query parameters, alternate API prefixes, version ranges,
  and package/migration/database fallbacks; and
- the stale Lidarr `ls29` documentation/development contract.

Preserving any of those behaviors requires separate user approval.

## Exact non-destructive probe

`test()` performs authenticated GET requests only and sends the key solely in
`X-Api-Key`, never following redirects. It must:

1. require exact `appName`, app version, LSIO `packageVersion`, SQLite database,
   migration, and nonempty parseable `startTime`;
2. validate the exact six-field native backup-list shape;
3. prove the configured path is a dedicated genuine read-only mount; and
4. when a harmless existing manual entry is present, map only its safe basename
   to one regular non-link file directly under the fixed mount without reading
   artifact contents.

Return true only after every check. Invalid configuration, auth/status,
redirect, malformed response, wrong identity, missing/writable/broad mount, or
unsafe existing entry raises a concise secret-safe error. `get_status()` may
report healthy only from the same checked exact evidence.

## Exact backup state machine

Under the canonical application-origin lock:

1. repeat exact status and record every manual baseline identity using all six
   fields;
2. trigger exactly `POST <api>/command` with `{"name":"Backup"}` and require a
   non-boolean numeric command ID;
3. poll to one fixed deadline and accept only `completed` plus `successful`;
4. attribute exactly one previously unknown manual entry at or after the
   vendor's whole-second run boundary;
5. reject malformed, unsafe, stale, ambiguous, or colliding API/native paths;
6. map the exact basename to the fixed read-only mount and stream the stable,
   descriptor-bound file through a spawned bounded copy/validation worker;
7. publish one mode-0600 artifact and sidecar transactionally; and
8. only after durable publication, delete the exact attributed native backup
   by ID. A failed deletion fails the run and preserves both copies.

Never read `/config`, media, or download roots; use Docker/SSH; or fetch
`/backup/...` over HTTP. The static route follows UI authentication and does not
accept an API key under Forms auth. A UI-cookie fallback is forbidden.

Native filenames have one-second resolution. Serialize and ensure consecutive
phase triggers cross the filename boundary.

## Exact artifact contract

Accept exactly three unique regular root members:

- Radarr: `config.xml`, `radarr.db`, `INFO`;
- Sonarr: `config.xml`, `sonarr.db`, `INFO`;
- Lidarr: `config.xml`, `lidarr.db`, `INFO`.

Validate before publication and again from the RestoreService-staged,
descriptor-bound artifact before destination contact. Require:

- no duplicate/case-colliding, nested, absolute, traversing, control,
  backslash, encrypted, link, device, FIFO, sparse, unsupported, CRC-failing,
  trailing, oversized, over-count, or expansion-bomb member;
- exact `INFO` version/timestamp shape and newline;
- one root `<Config>` with exactly one nonempty API key, held only in memory;
- SQLite header, bounded size/rows, immutable/query-only inspection,
  `quick_check=ok`, zero foreign-key failures, no hot journal sidecars, exact
  migration, and every conservative required table; and
- rejection of PostgreSQL/config-only artifacts.

Required table sets are exactly those in the research note. Sidecars contain
only application/version/package, backend/migration, command/native IDs and
times, structural inventory/counts, validator identity, and artifact
size/SHA-256. The plugin cannot observe its source application's OCI identity,
so the independent exact-image drill binds that evidence instead of copying an
adapter constant into the sidecar. Sidecars never contain keys, origins, paths,
settings, titles, database values, providers, or other application content.

## Create-only isolated restore

Restore must flow through RestoreService and requires:

- its private immutable size/hash-verified staged inode and matching sidecar;
- distinct source/destination target IDs;
- explicit `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- the exact canonical destination origin in the comma-capable local allowlist;
- exact status and every fresh-resource endpoint returning an empty list; and
- a new disposable exact-image destination with separate empty config, no
  published port, production route, shared mount, or production credential.

That precondition deliberately leaves no prior authoritative destination state
to preserve or roll back. Any failure at or after native upload marks the
disposable destination tainted: the run must fail, no success may be reported,
and the drill/orchestrator must destroy it rather than retrying or reusing it.
This is not authorization to restore onto an existing or production target.

Fresh-resource paths:

- Radarr: tag, rootfolder, indexer, downloadclient, notification, movie;
- Sonarr: tag, rootfolder, indexer, downloadclient, notification, series;
- Lidarr: tag, rootfolder, indexer, downloadclient, notification, artist.

Revalidate fully, upload the held bytes, require
`{"restartRequired":true}`, request restart with the old destination key, and
require `{"restarting":true}`. Extract the restored source key only from the
already validated config and keep it in memory. Poll until that key receives
the exact status with a new nonempty start time, then prove the phase marker and
structural state. Restart the destination a second time, require another new
start time, and repeat content proof.

Any failure or ambiguity destroys the disposable destination. Never retry a
partially changed target or call upload acknowledgement/readiness alone a
successful restore.

## TDD vertical slices

Work one RED-to-GREEN public tracer bullet at a time:

1. discovery, automatic capability, strict schemas, config, and target APIs;
2. exact status/package/migration/list/mount probe and truthful status;
3. command completion, six-field attribution, whole-second collision, and
   source-path ownership;
4. bounded spawned copy/validation, strict artifact matrix, transactional
   publication, private sidecar, and cancellation cleanup;
5. exact post-publication native cleanup and failure preservation;
6. RestoreService provenance/audit, isolated/fresh authorization, restored-key
   restart, phase markers, and second-restart persistence; and
7. exact three-app drill, documentation, full gates, reviews, and focused
   commit.

Maintain existing Readarr/Prowlarr behavior and prove shared-core changes with
their focused suite. Do not broaden the module to unsupported apps or archive
formats.

## Exact two-clean-round Docker drill

Use one opt-in integration file and only the exact amd64 manifests. Every
container, network, config tree, credential, native source, artifact root, and
listener is synthetic, internal, private, and disposable. The backend runner
gets no internet, Docker socket, `/config`, media, download, or broad host path;
it sees only one app's fixed read-only manual-backup mount and its artifact
root.

For each app and clean round:

1. create a fresh exact source, verify OCI labels/status, and prove the narrow
   mount and API-key transport;
2. create tag A through the supported API and produce scheduled artifact A;
3. cross the filename boundary, add tag B, produce artifact B, and prove unique
   commands/native entries, native cleanup, distinct size/hash/content,
   sidecars, strict database/member validation, and A immutability;
4. destroy the source;
5. restore A through RestoreService to fresh destination one, prove only A,
   exact restart/status, then second-restart persistence;
6. restore B to an unrelated fresh destination two and prove A+B through both
   restart cycles; and
7. destroy and independently audit every created resource.

Repeat from entirely empty state with new source, credentials, volumes,
artifacts, and destinations. Per app this is four backups and four independent
fresh restores; the trio totals twelve backups and twelve fresh restores.

In the exact-image drill, run the safely inducible identity,
package/version/backend/migration, failed/ambiguous command, mount/swap,
corrupt artifact, unauthorized restore, and nonfresh-destination negatives once
per app. Run the exhaustive attribution/collision, source mutation,
archive/resource/config-only, cleanup preservation, provenance, same-target,
restored-key, restart/state-loss, timeout, cancellation, and worker-reaping
matrix once against the shared public Servarr deep-module seams. Parameterize
the trio adapters for every adapter-specific identity, API prefix, package,
migration, fixed mount, database/member/table, sidecar, fresh-resource, and
semantic restored-content rule plus one full public backup, restore, and
RestoreService journey each. Do not duplicate identical process-lifecycle
tests for thin constant-only adapters or weaken production code merely to
inject destructive vendor-process failures into the exact-image drill.

## Verification and completion

- focused trio, legacy Servarr, public API, scheduler, RestoreService, artifact,
  and hygiene tests;
- the complete exact-image drill from two clean states;
- full backend pytest and mypy, changed-file Black/isort;
- frontend tests, lint, and build for user-visible schemas;
- SemVer, diff, secret, and disposable-resource audits;
- independent Standards and Spec reviews with no unresolved P0-P3 issue;
- compatibility, recovery, changelog, ledger, research, plan, and README
  evidence current; and
- one focused milestone commit, with no push, deploy, target, schedule,
  credential, mount, or production change.

Mark `DONE (local)` only when every gate passes. Production separately requires
immutable image pins, the broad application API keys, three exact read-only
manual-backup mounts, exact native-cleanup approval, targets/schedules, and
backup-only validation. Production restore remains forbidden.

## STOP conditions

Stop rather than weaken the result when any manifest, architecture, app/package
version, API, backend, migration, member, command, table, status, or restore
behavior drifts; a fixed mount is missing/writable/broad or cannot bind the
attributed basename; HTTP/UI auth, `/config`, media/download access, Docker,
SSH, root host access, downtime, or an alternate alias/version/protocol would
be required; cleanup is not exactly attributable and post-publication; the
artifact is incomplete, unsafe, unbounded, inconsistent, or secret-leaking;
the destination is not fresh, exact, local, isolated, and disposable; or any
production privilege, mount, credential, target, schedule, write, restore, or
compatibility behavior would occur without explicit approval.
