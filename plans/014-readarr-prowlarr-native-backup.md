# Plan 014: Add exact Readarr and Prowlarr native recovery

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation and the existing `ServarrPlugin`
- **State**: DONE (local)
- **Production status**: local implementation and recovery proof only. Production
  activation remains blocked on immutable image pins, approval to delete each
  newly attributed native manual backup after safe publication, and two narrow
  read-only native-backup-directory mounts. Production restore is forbidden.

## Fixed contract

Read [the primary-source research](research/readarr-prowlarr.md), `AGENTS.md`,
and `ADDING_PLUGINS.md` before changing code. Do not contact production.

Implement exactly these linux/amd64 contracts:

| Application | Version | Immutable manifest | API | Migration | Database |
| --- | --- | --- | --- | --- | --- |
| Readarr | `0.4.18.2805` | `sha256:440dc56b904d7363468c1b19e60ccd9dd18b69bdccdb9712d5718779cc48d279` | `/api/v1` | 158 | `readarr.db` |
| Prowlarr | `2.4.0.5397` | `sha256:a82572d17330327d1efd3d2242eac03b95402607dc96f620447a8426be2f7bd1` | `/api/v1` | 44 | `prowlarr.db` |

The deployment selectors `rolling` and `2.4.0-develop` are mutable and are not
evidence of the production bytes. Local drills use only the immutable manifests
above. A later infrastructure change must pin the selected digests before a
production target is enabled.

Both plugins protect only their application control planes. Readarr books and
download working data, Prowlarr's external services, and all media payloads are
excluded. Do not add the historical `nzbdrone.db` alias or any other version,
member, API-prefix, credential, or schema compatibility path.

## Public seams and module design

The pre-agreed TDD seams are:

1. plugin loader plus `/api/v1/plugins` discovery/schema;
2. `plugin.test(config)` for exact, non-destructive connectivity;
3. `plugin.backup(BackupContext)` for the complete native state machine and
   final artifact/sidecar;
4. `RestoreService.restore(...)` plus `plugin.restore(RestoreContext)` for the
   isolated destructive workflow and audit ledger; and
5. one opt-in exact-image Docker drill exercising both adapters twice.

Keep `ServarrPlugin` as the deep module. Add only the common exact status,
command-completion, strict archive, bounded streaming, source-cleanup, and
restore-readiness behavior needed by both adapters. Readarr and Prowlarr remain
thin declarative adapters. Do not add a second protocol implementation.

## Required behavior

### Configuration and probe

Each flat schema requires `base_url`, secret `api_key`, and an absolute narrow
`backup_directory`. The defaults are `/sources/readarr/backups` and
`/sources/prowlarr/backups`. The schemas reject extra fields and have no real or
placeholder credential default. Validation rejects wrong types, whitespace-only
values, URL credentials/query/fragment, unsafe schemes, malformed origins,
relative/traversing paths, and broad application/backup roots.

`test()` performs only authenticated `GET` requests. It must:

- reject cross-origin redirects and send the API key only in `X-Api-Key`;
- require exact `appName`, version, `databaseType == "sqlite"`, and migration;
- validate the native backup list shape;
- require the configured backup directory to be a genuine read-only mount; and
- if a harmless existing entry is present, verify that its API basename maps to
  one regular, non-symlink file directly inside that mount without reading the
  artifact body.

No backup, delete, restore, restart, Docker, filesystem mutation, or production
operation is permitted in `test()`.

### Backup

Under the shared application-origin lock:

1. Re-run the exact status gate and capture a full baseline backup identity set.
2. POST exactly `{"name":"Backup"}` and require a numeric command id.
3. Poll to a fixed deadline. Readarr requires completed/successful; Prowlarr
   requires completed because its exact resource has no `result` member.
4. Attribute exactly one new manual entry created after the run boundary.
5. Reject unsafe, ambiguous, stale, or mismatched API paths and map the exact
   basename to one regular, non-symlink file directly under the fixed read-only
   mount.
6. Stream that stable local file to a private transactional artifact with hard
   time and byte ceilings, descriptor/identity evidence, and no whole-artifact
   buffering. Never fetch the ZIP over HTTP or store UI-session credentials.
7. Strictly validate before publication.
8. After the artifact and sidecar are durably published, delete only the exact
   newly attributed native manual entry. If deletion fails, record the run as a
   failure but preserve both copies for operator recovery.

The native ZIP must contain exactly three unique regular root members:
`config.xml`, exact database name, and `INFO`. Reject nesting, traversal,
absolute names, links/devices, encryption, unsupported compression, duplicate
or extra members, CRC/trailing-data failure, size/member/ratio bombs, malformed
XML, missing/non-unique API key, wrong INFO version/timestamp, corrupt SQLite,
foreign-key failure, wrong migration, missing conservative exact tables, and
config-only/PostgreSQL artifacts. Sidecars contain only structural evidence and
never credentials, URLs, paths, provider settings, or database values.

### Restore

Restore is destructive and local-only. It must be invoked through
`RestoreService`, use its immutable size/hash-verified staging copy, reject a
source target as destination, and refuse any URL that is not demonstrably part
of the current disposable exact-image drill. No production hostname, network,
container, volume, or data is allowed.

The drill runner must set `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` and list the
exact disposable destination origin in
`HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS`. Normal backend deployments
set neither value. The plugin requires both gates before any destination I/O.

For a fresh destination, first require the exact application status and empty
mutable control-plane resource lists; refuse a non-empty or unprovable target
before upload. Then:

1. fully revalidate the staged ZIP before mutation;
2. record exact status/start time with the destination key;
3. upload the ZIP and require `restartRequired == true`;
4. request restart and require `restarting == true`;
5. use the restored `config.xml` API key only in memory; and
6. require a different non-empty start time plus the exact status/version before
   success.

The result is `automatic` for each application's control plane. Never claim that
external media, services, credentials, or integrations are restored.

## TDD vertical slices

Work red → green through one public behavior at a time:

1. **Discovery and schema** — both adapters load, expose `automatic`, and
   persist strict flat configs through target APIs.
2. **Exact probe** — happy exact status/list and secret-safe negative status,
   version, migration, database, redirect, timeout, and auth cases.
3. **Native command state machines** — Readarr result-required and Prowlarr
   result-absent success, terminal failure, timeout, and ambiguous attribution.
4. **Artifact boundary** — streamed private artifacts, strict three-member
   validation, bounded negative matrix, durable sidecar identity, and zero
   publication on failure/cancellation.
5. **Native cleanup** — delete only the attributed id after publication; prove
   failed deletion preserves the published artifact and native source copy.
6. **Restore and audit** — immutable RestoreService staging, full revalidation,
   upload/restart/new-process proof, restored-key transition, provenance and
   success/failure audit records, and source/destination refusal.
7. **Exact drill and release** — two online backups and two independent fresh
   restores per app, repeated from a clean state, followed by full checks and
   two-axis review.

Tests use public seams and `httpx.MockTransport`; helper-level tests are allowed
only for deterministic malformed ZIP/resource-bound cases that cannot be
observed safely through a spawned or network adapter.

## Exact local drill

Create one opt-in `test_readarr_prowlarr_docker_drill.py`. For each application:

1. Pull/run only the immutable amd64 manifest on an internal disposable network
   with private config and automatic restart behavior. Verify labels/status,
   exact version, SQLite backend, and migration. Mount only that source's native
   backup directory read-only into the locked-down backend runner.
2. Create tag A through the supported API, run `test()`, create artifact A, then
   create tag B and artifact B. Prove distinct command/native ids, hashes,
   timestamps, structural manifests, valid private sidecars, and exact cleanup
   of each attributed native backup.
3. Restore A and B through `RestoreService` into two fresh exact-image
   destinations with separate empty config. Prove A contains only tag A; B
   contains A and B; then restart and repeat readiness/state proof.
4. Prove no production/public endpoint, source config, media/download path,
   Docker socket, or broad host path is exposed to the backend runner.
5. Exercise representative wrong-version/database/migration, failed command,
   ambiguous backup, missing/writable/swapped backup mount, corrupt artifact, unauthorized
   restore, and stale-destination failures.
6. Remove every container, network, volume, directory, listener, and synthetic
   credential, then assert absence. Run the A/B backup and recovery sequence
   twice from clean state; run the representative negative matrix once per app
   against the same immutable images and public seams.

The exact local images redirect `/backup/...` to Forms login even when a valid
`X-Api-Key` is supplied. The selected contract therefore forbids HTTP artifact
download and UI cookies. STOP if the narrow read-only backup-folder mount cannot
be provided; do not weaken authentication or silently add a fallback.

## Verification and completion

Run from `backend/` with the existing virtual environment:

- focused plugin/API/RestoreService tests;
- the exact opt-in drill twice;
- full pytest, mypy, Black, and isort;
- repository hygiene and SemVer checks;
- frontend tests, lint, and build because schemas are user-visible; and
- `git diff --check` plus secret-pattern and disposable-resource audits.

Update `docs/PLUGIN_COMPATIBILITY.md`, `docs/RECOVERY.md`, `CHANGELOG.md`,
`plans/007-coverage-ledger.md`, and `plans/README.md`. Two independent reviewers
must find no unresolved P0/P1 Standards or Spec issue from fixed point
`74325db`. Commit the complete local milestone as
`feat: add Readarr and Prowlarr recovery milestone`; do not push, deploy, tag,
or release.

Mark `DONE (local)` only when both applications pass every gate. Production
rows remain blocked until the user approves immutable infrastructure pins,
native-manual cleanup, target/schedule changes, and backup-only validation.

Completion evidence (2026-08-16): 226 focused new/legacy Servarr and
RestoreService tests passed; the opt-in exact-image drill passed both
applications in two clean A/B rounds plus one negative matrix per app
(`2 passed in 196.42s`), covering four backups and four independent fresh
restores per application with exact restart/state proof and complete resource
cleanup. The full backend suite passed (`1092 passed, 9 skipped`), backend mypy
passed for 103 source files, and frontend tests (`48 passed`), lint, and build
passed. Changed-file Black/isort, SemVer, diff, Gitleaks, and disposable-resource
checks passed. The independent Standards and Spec reviews found no unresolved
P0/P1 issue; all lower-priority findings were resolved before commit.

## STOP conditions

Stop rather than adding compatibility or weakening evidence if:

- either immutable image reports drift in version, architecture, API, database,
  migration, command, archive, or restore behavior;
- the configured backup directory is absent, writable, not a mount, broadens to
  `/config`, or cannot be mapped safely to the attributed API basename;
- native manual backup cleanup is not approved for later production use;
- an artifact is config-only, incomplete, unsafe, unbounded, or secret-leaking;
- worker/network work cannot be timed out, cancelled, and cleaned before return;
- restore isolation or fresh-destination identity cannot be proven;
- production contact/write/restore, source downtime, Docker socket, `/config`,
  media, download, root-host, or SSH access would be required; or
- any legacy database alias, floating tag, version fallback, or alternate
  protocol would be needed.
