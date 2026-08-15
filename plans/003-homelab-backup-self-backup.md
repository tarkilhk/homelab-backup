# Plan 003: Homelab Backup self-backup and offline restore

## Status

- **Priority**: P0
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation contract
- **State**: DONE (local)
- **Production status**: BLOCKED until the locally verified image is deployed;
  production restore remains forbidden
- **Researched at**: `homelab-backup` v0.2.1 and `homelab-infra`
  `eeed77a`, 2026-08-15

## Outcome and boundary

Add one `homelab_backup` plugin for the two v0.2.1 backend instances. It must
create a consistent online snapshot of `/app/db/homelab_backup.db`, validate
the snapshot and a versioned manifest, and publish one secret-bearing ZIP plus
its normal sidecar.

The database owns targets and their raw plugin configuration, tags/groups,
schedules, retention settings, maintenance configuration, and the run/artifact
catalog. The plugin deliberately excludes `/backups`, frontend files, container
images, environment files, and infrastructure declarations. Artifact-tree
replication and failure-domain placement remain external responsibilities; a
restored catalog must truthfully show any artifacts that are not mounted at
their recorded paths.

## Verified deployments

Both backends use `tarkilhk/homelab-backup:backend-v0.2.1`, run as root through
Compose, and bind a host directory at `/app/db`:

- Docker VM: `/docker-apps/homelab-backup/db:/app/db`
- TarkilNAS: `/volume1/docker/homelab-backup/db:/app/db`

Their `/backups` roots are distinct. The Docker VM path is NFS-backed under
`/volume1/shared`, while the NAS instance uses
`/volume1/docker/homelab-backup/backups`. Neither deployment declares peer
communication. The frontend has no persistent state.

Authoritative declarations:

- `homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml`
- `homelab-infra/docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml`
- `backend/app/core/db.py`

## Public interface

Keep the target schema flat:

- `database_path`: required absolute path ending in `homelab_backup.db`, with
  `/app/db/homelab_backup.db` as the deployment hint.

For an existing database, `test()` must open it read-only, require the current
application tables and columns, and prove `PRAGMA quick_check` plus an empty
`PRAGMA foreign_key_check`. For a nonexistent database, it succeeds only when
the parent is a safe, writable, otherwise-empty restore directory with the
fixed sentinel. It must not log configuration rows or credentials.

`backup()` must use SQLite's online backup interface rather than copying the
live database/WAL files. Cancellation and timeout must stop the snapshot worker.
It must validate the snapshot independently, then write a ZIP containing only:

- `homelab_backup.db`;
- `manifest.json` with a format version, application version, database byte
  count/SHA-256, required-table list, and non-secret semantic row counts.

The manifest also records SQLite version and a normalized schema SHA-256. The
manifest and SQLite payload must agree before transactional publication. The
artifact must retain mode `0600`; fail publication if the filesystem cannot
enforce that private mode.

## Offline restore contract

Declare `restore_capability = "partial"`. The plugin can prove an exact,
integrity-checked offline database replacement, but it cannot safely stop and
start another backend without broad Docker/host privileges. The exact-image
drill supplies the separate application-readiness proof.

Restore must:

1. validate the ZIP, manifest, digest, schema, integrity, and foreign keys
   before destination mutation;
2. require the destination database's parent to contain the fixed sentinel
   `.homelab-backup-restore-destination` with the exact v1 marker and no other
   files;
3. refuse all paths under `/app/db` or `/backups`, symlinks, non-canonical
   filenames, and any existing destination database/WAL/SHM file;
4. stage on the destination filesystem and create the database atomically;
5. revalidate digest, schema, integrity, foreign keys, and semantic counts;
6. remove staging and any newly published database after a post-publication
   failure;
7. return `partial` with the restored path and an explicit requirement to boot
   and verify an isolated backend.

No production directory may contain the restore sentinel.

## Test-first slices and seams

The pre-agreed seams are the plugin public methods, loader/schema routes, and an
exact-image local boot. Work vertically:

1. Discovery, flat schema, capability, and invalid paths.
2. Read-only connectivity with table/integrity/foreign-key failures.
3. Online SQLite snapshot, manifest, unique artifact, and sidecar.
4. Timeout/cancellation, concurrent-path use, secret-safe logs, and atomic
   cleanup.
5. Empty/malformed/incomplete/mismatched/malicious artifact rejection.
6. Sentinel/live-path/WAL/symlink restore refusal before mutation.
7. Successful offline atomic creation with exact content proof.
8. Cleanup or safe failure at each publication boundary.
9. Real `/api/v1/plugins` discovery/schema/test behavior.
10. Two consecutive exact-image local boots from distinct restored snapshots.

## Local drill

Use only disposable local bind directories and an internal Docker network. Do
not mount production paths, `/var/run/docker.sock`, Jellyfin state, NFS, or the
Standard Notes network. Seed synthetic marker targets through the source API,
take two changing snapshots, restore each to a fresh sentinel-marked directory,
then boot the locally built v0.2.1 backend image with that directory mounted at
`/app/db`. Prove readiness, marker equality, expected row counts, artifact
size/SHA-256/sidecar, and that restored jobs are never executed.

## Production gate

After deployment, create one local self-backup target and job on each backend.
Connectivity checks and backup triggers are allowed only after the user confirms
the deployment. Do not create a restore destination or sentinel in production,
and do not perform a production restore.

## Done criteria

- [x] All ten test-first slices pass.
- [x] Two consecutive exact-image backup-to-boot drills pass.
- [x] Full backend and frontend behavior checks pass.
- [x] Code review has no unresolved P0/P1 findings.
- [x] Recovery/compatibility/changelog documentation records DB-only scope,
      secret sensitivity, exact version, and evidence.
- [x] The milestone is committed independently in the focused commit containing
      this completed plan.

## Local evidence

- Final backend suite: `360 passed, 2 skipped`.
- Final frontend suite: `48 passed`; ESLint and production build passed.
- Application plus changed-test mypy: 90 files, no issues.
- Changed Python files pass Black and isort.
- Exact v0.2.1-image drill: two unique online snapshots, two independent
  size/SHA-256/sidecar/manifest validations, two create-only restores, and two
  isolated `--network none` backend boots passed.
- Standards and spec re-reviews report no remaining P0/P1 findings.
- The repository-wide formatter/type-check commands still encounter pre-existing
  test-tree debt: duplicate top-level `conftest` modules prevent blanket mypy,
  and unrelated historical tests are not Black/isort-clean. No unrelated files
  were mechanically rewritten in this milestone.

## STOP conditions

Stop rather than broaden privileges if a consistent snapshot requires stopping
production, the restore would overwrite an existing database, the artifact was
created by a different application version, the drill would mount production
storage or contact production endpoints, or full application readiness could
be claimed only by granting the plugin Docker or host lifecycle control.
