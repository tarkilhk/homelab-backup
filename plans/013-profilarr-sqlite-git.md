# Plan 013: Add Profilarr 1.1.5 SQLite and Git backup with isolated restore

## Executor instructions

Read `AGENTS.md`, `ADDING_PLUGINS.md`, and
`plans/research/profilarr.md` completely before changing code. The research
note is the fixed service boundary and primary-source record. Work test-first
through the vertical slices below and keep every source operation read-only.
Stop on version, schema, journal, Git, image, path, or restore-isolation drift
instead of adding compatibility behavior.

Do not contact production during this milestone. Do not use Profilarr's native
backup, restore, or import endpoints. Do not stop or change a production
service. Production restore is forbidden under all circumstances.

> **Drift check (run first)**:
> `git diff --stat e30c061..HEAD -- backend/Dockerfile backend/app/plugins backend/tests docs CHANGELOG.md plans`
>
> Compare the live repository with the contract and current-state notes below.
> If an in-scope seam has changed materially, stop and report the drift before
> implementing. Do not preserve an old seam with a compatibility fallback.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **Category**: direction
- **Planned at**: commit `e30c061`, 2026-08-16
- **State**: DONE (local)
- **Production status**: implementation and recovery proof are complete
  locally. A later approved production change may add two narrow read-only
  mounts, a target, a schedule, and backup-only evidence. Production restore
  remains forbidden.

## Why this matters

Profilarr 1.1.5 has two independent authorities. `/config/profilarr.db` holds
credentials, authentication, linked repository and Arr configuration,
selections, schedules, and local policies. `/config/db` is a real Git working
repository holding profiles, custom formats, regex patterns, media-management
definitions, local refs, and potentially unpushed commits. Protecting only one
loses unique state.

Profilarr's native v1.1.5 backup recursively copies the live `/config` tree. It
does not establish either a SQLite transaction snapshot or a Git consistency
boundary, and it includes unnecessary logs, old backups, and repository
configuration. This plan replaces that unsafe mechanism with SQLite's online
backup API plus a stable, self-contained `git bundle --all` from a clean
repository.

## Outcome and fixed scope

Add one `profilarr` plugin for exact Profilarr 1.1.5, source commit
`21c8eaeb93241588323672866854275ff7dbed67`, and image
`santiagosayshey/profilarr:v1.1.5`. The local drill must pin Linux/amd64 image
digest:

`santiagosayshey/profilarr@sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad`

One backup run must:

1. prove exact read-only SQLite and Git source contracts;
2. create a coherent online SQLite snapshot;
3. create a full bundle only while HEAD, refs, branch, index/worktree state,
   operation state, and authoritative-file inventory remain stable;
4. validate both payloads and a private manifest; and
5. publish one private, sidecar-bound artifact transactionally.

Restore is create-only on the development VM. It materializes a new
`/config/profilarr.db`, reconstructs `/config/db` from the bundle without
copying `.git/config`, and verifies exact bytes, refs, branch, HEAD, inventory,
and Git integrity. The artifact covers all authoritative Profilarr application
state. Declare `restore_capability = "automatic"`, the runtime value for a
plugin-managed complete restore. The plugin must not gain Docker or host
lifecycle access merely to boot Profilarr; the exact-image drill supplies the
separate readiness and application-state proof.

Explicitly exclude logs, native backup ZIPs, repository configuration and
hooks, reflogs, Git credentials, dirty/index/conflict state, caches, unreachable
objects, Radarr/Sonarr state, media, container state, infrastructure, host root,
and Docker socket access. Do not implement Profilarr v2 compatibility; upstream
declares v2 databases and configuration incompatible with v1.

## Current repository seams

- `backend/app/core/plugins/base.py` exposes `BackupContext`, `RestoreContext`,
  and only `automatic`, `partial`, or `manual` restore capabilities.
- `backend/app/core/plugins/artifacts.py` owns private transactional artifact
  publication and mandatory size/SHA-256 sidecar binding. Reuse it; do not
  create a plugin-specific final-path or sidecar writer.
- `backend/app/services/restores.py` validates and privately stages restore
  artifacts before calling a plugin. Restore must still revalidate the complete
  Profilarr payload.
- `backend/app/plugins/bazarr/` and
  `backend/tests/integration/test_bazarr_docker_drill.py` are the current
  patterns for mode-aware source/destination schemas, bounded worker cleanup,
  local-only create-only restore, exact-image proof, and secret-safe evidence.
- `backend/app/plugins/audiobookshelf/` is the closest pattern for a
  consistency-aware read-only filesystem source and online SQLite snapshot.
- `backend/Dockerfile` currently does not install Git. The plugin needs the real
  Git CLI for bundle creation, verification, ref import, and `fsck`; add the
  Debian `git` package without introducing a shell or network-based Git path.
- `backend/tests/test_repository_hygiene.py` contains backend-image tool
  contract checks and must gain a Git-presence assertion.
- The canonical target schema UI is flat. Follow Bazarr's top-level `mode` plus
  conditional `allOf` requirements; defaults remain hints and secrets never
  receive defaults.

## Exact public plugin contract

### Package, discovery, and schema

Create:

```text
backend/app/plugins/profilarr/
├── __init__.py
├── plugin.py
└── schema.json
```

`__init__.py` exports exactly one `ProfilarrPlugin`. Keep the schema flat with
`additionalProperties: false` and these fields:

- `mode`: required enum `source` or `restore_destination`, default `source`;
- `database_path`: source-only required absolute path, default hint
  `/sources/profilarr/profilarr.db`;
- `repository_path`: source-only required absolute path, default hint
  `/sources/profilarr/db`; and
- `restore_directory`: restore-only required absent absolute path beneath a
  private sentinel-only parent under `/tmp` or `/restore`.

Source config contains exactly `mode`, `database_path`, and `repository_path`.
Restore config contains exactly `mode` and `restore_directory`. Reject absent
values, coercions, aliases, legacy keys, control characters, traversal,
symlinks, source/destination overlap, broad `/config`, `/backups`, or `/app`
paths, and implicit guesses.

Use fixed implementation limits rather than user-facing tuning fields. Start
with three complete capture attempts, a 300-second overall backup deadline,
300-second restore deadline, 60 seconds per Git command, 10,000 archive files,
1 GiB compressed bytes, 2 GiB uncompressed bytes, 100:1 maximum ZIP expansion,
16 MiB per YAML file, and 512 MiB total authoritative YAML/data bytes. Treat a
locally observed exact-v1 fixture exceeding a limit as a STOP condition and
document a revised bound; never silently remove a bound.

### Non-destructive source and destination probes

`test()` in source mode uses no network and no Profilarr, Arr, or Git-hosting
credential. It must:

1. require `profilarr.db` to be a regular non-link file on its exact configured
   genuine read-only bind and `repository_path` to be a canonical regular
   directory on its exact configured genuine read-only bind;
2. reject source roots that alias one another, escape their allowlists, expose
   broad `/config`, or require a write probe;
3. open SQLite read-only, require `PRAGMA journal_mode = delete`, no visible
   `-wal`, `-shm`, or hot `-journal` residue, a SQLite header, exact migrations
   1 through 4, the exact pinned schema fingerprint, successful
   `PRAGMA quick_check`, and an empty `PRAGMA foreign_key_check`;
4. require exactly the v1 persistent tables `arr_config`, `scheduled_tasks`,
   `settings`, `auth`, `format_renames`, `language_import_config`, `migrations`,
   `backups`, and `failed_attempts`, plus only SQLite-owned internal tables;
5. pin the complete v1.1.5 column/index/foreign-key fingerprint learned from a
   database initialized by the exact image, while explicitly checking the
   semantic columns listed in `plans/research/profilarr.md`;
6. require sane single-instance `auth` and `settings` state and a credential-
   free, safe linked repository URL if one is configured, without returning or
   logging its value; and
7. run the complete Git preflight defined below.

`test()` in restore mode performs only authorization, path, sentinel,
create-only, and isolation preflight. Require environment variable
`HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`, a loopback-only network namespace,
an absent destination, and a private parent containing only
`.profilarr-restore-destination` with exact content
`profilarr-v1.1.5-isolated-restore-v1\n`. It must never use HTTP, Git network,
Docker, or a production path.

Use the canonical exception mapping from `ADDING_PLUGINS.md`. `get_status()`
reports `ok` only after the same mode-specific probe and otherwise reports a
checked, secret-safe error.

## Exact Git source contract

Run Git with fixed argument arrays, `GIT_OPTIONAL_LOCKS=0`,
`--no-optional-locks`, `GIT_CONFIG_NOSYSTEM=1`, an empty private global config
and HOME, deterministic `LC_ALL=C`, bounded captured output, and no credential
helper or prompt. No command may name a remote URL or perform fetch, pull, push,
clone, submodule, or LFS network access against the source.

Repository preflight and each fence must require:

- a normal `.git` directory rooted exactly at `repository_path`, a valid
  checked-out worktree, Git object format `sha1`, and a symbolic branch whose
  tip equals HEAD;
- no shallow file, partial-clone/promisor configuration or objects, alternates,
  replace refs, submodules/gitlinks, Git LFS pointer dependency, missing object,
  or corrupt reachable object;
- no `.git/*.lock` or nested lock, merge, rebase, cherry-pick, revert, sequencer,
  or bisect state;
- clean tracked worktree and index, no untracked file anywhere, and no ignored
  file beneath `regex_patterns/`, `custom_formats/`, `profiles/`, or
  `media_management/`;
- every regular file beneath those four authoritative directories tracked and
  reachable from HEAD, no link/special file, and a bounded inventory of path,
  mode, byte size, and SHA-256; and
- a successful local connectivity/integrity check over every ref that will be
  bundled. Dangling unreachable objects and reflogs are intentionally excluded.

Fence evidence is a canonical tuple containing symbolic branch, HEAD, sorted
ref-name/tip pairs and their digest, porcelain-v2 tracked/index status, active-
operation markers, and authoritative-file inventory/hash. Never include commit
messages, repository content, remote URLs, or configuration values in public
metadata or errors.

## Bounded composite backup state machine

The centrally serialized target operation is the only target lock. Each of at
most three attempts performs the complete cross-store capture again:

1. Revalidate both read-only source identities and Git fence A.
2. Open the source database read-only and use
   `sqlite3.Connection.backup()` to a new mode-0600 temporary database. Do not
   raw-copy the live file. Bound busy handling, progress, cancellation, and the
   attempt deadline; close both connections before cleanup.
3. Run `git bundle create <private-temp>/repository.bundle --all` locally. The
   source remains read-only and optional Git locks remain disabled.
4. Capture fence B and require byte-for-byte equality with fence A. Also require
   the source database and repository mount identities to remain bound. If the
   repository changed, discard every temporary output and retry from step 1;
   after three attempts fail honestly as changing/dirty.
5. Validate the SQLite snapshot independently. Run `git bundle verify`, require
   no prerequisites, bind every advertised ref to the manifest, import into a
   private validation repository, inspect trees before checkout, reject unsafe
   modes/dependencies, run `git fsck --full`, check out the recorded branch and
   HEAD, and match the authoritative inventory.
6. Safely parse every tracked application YAML under the four authoritative
   directories. Pin the exact v1 top-level structures for regex, custom-format,
   profile, and media-management definitions from the exact fixture; never log
   YAML values or filenames derived from private content.
7. Build the deterministic artifact and revalidate it from its final staged
   inode before transactional publication. Return only the absolute final
   `artifact_path` after the helper has committed its mode-0600 sidecar.

Blocking SQLite, Git, hashing, validation, and ZIP work must not block the
backend event loop. Use bounded worker/process execution patterned after the
newest plugins. Timeout or cancellation must terminate and reap the complete
worker and any Git child process before releasing the target lock or deleting
private temporary data.

## Artifact and manifest contract

Publish one mode-0600 `.profilarr` ZIP with exactly three regular root members
in this order:

1. `profilarr.db`;
2. `repository.bundle`;
3. `manifest.json`.

Use fixed ZIP member timestamps and private regular-file modes. Reject
encryption, duplicates, extras, directories, nested or absolute paths,
traversal, links, devices, unsupported compression, ambiguous/trailing ZIP
data, empty members, size/count/ratio violations, CRC failures, and hash or
manifest mismatches. Keep validation streaming or disk-backed.

`manifest.json` is canonical JSON with a format version and contains only:

- exact Profilarr version, source commit, image tag/digest, artifact creation
  time, and SQLite/Git tool versions;
- database size/SHA-256, exact schema fingerprint, migrations, integrity result,
  and non-sensitive table counts;
- bundle size/SHA-256, object format, symbolic branch, HEAD, sorted ref
  names/tips and ref digest, clean-state proof, and authoritative inventory of
  paths/modes/sizes/hashes; and
- format limits and validation version needed for independent recovery.

The private artifact necessarily contains credentials, hashes, internal URLs,
repository history, profiles, and application state. The manifest must not copy
SQL values, API keys, password/session material, remote URLs, profile names,
commit messages, or YAML values. The external sidecar contains only generic
identity plus safe structural facts such as application version, artifact
format, validation result, table count names/counts, and ref count. It must not
contain manifest inventory paths, ref names, URLs, repository/config values, or
fixture secrets.

## Create-only isolated restore contract

Accept only the independently staged and hash-verified artifact supplied by
`RestoreService`. Re-run complete ZIP, manifest, SQLite, bundle, ref, tree, and
YAML validation before creating service paths. Keep restore disabled unless the
isolated authorization, loopback-only namespace, exact destination sentinel,
and `/tmp` or `/restore` location all pass.

Create one new private destination representing `/config`:

- stream `profilarr.db` to `<destination>/profilarr.db` with mode 0600;
- reconstruct `<destination>/db` as a new Git repository from
  `repository.bundle`; never extract `.git` bytes and never copy source
  `.git/config`, hooks, reflogs, locks, or credentials;
- import every manifest ref to its exact ref name, restore the symbolic branch
  and HEAD, check out a clean worktree, and prove ref digest, `git fsck`, and
  authoritative inventory equality; and
- read the restored database's linked repository URL only inside the private
  worker. Reject credentials, control characters, query/fragment secrets,
  local/file/ext-helper schemes, or an unsafe origin. Configure at most one
  credential-free `origin`; no connection attempt is part of restore.

Use create-exclusive files/directories, no-follow descriptor-relative
operations, private modes, bounded copies, fsync for files and directories,
and atomic no-replace publication on the destination filesystem. Consume the
sentinel only after success. On failure remove only plugin-owned staging and
leave no usable destination; preserve any foreign state introduced by a race.
Return `status: success`, the restored path, and a secret-safe message stating
that all authoritative Profilarr application state was restored. Exact-image
boot and external Git/Radarr/Sonarr dependency readiness remain separate drill
and recovery-stack checks, not missing Profilarr state.

## TDD vertical slices

Begin each slice with one failing observable test. Implement only enough for
that test, retain its negative cases, and keep all earlier slices green.

1. **Runtime tool, contract, and discovery.** Add Git to `backend/Dockerfile`
   and its image-tool guard. Add package export, exact metadata, `automatic`
   capability, flat mode-aware schema, strict config validation, loader/API
   discovery, target persistence, and honest status tests.
2. **Read-only SQLite boundary.** Test genuine bind/source identity, exact
   rollback-journal mode, companion/hot-journal refusal, online snapshot under
   concurrent DB-only writes, pinned migrations/schema/integrity/foreign keys,
   source replacement, busy/timeout/cancellation, and secret-safe diagnostics.
3. **Git preflight and stable fence.** Test every clean-repository invariant,
   fixed argv/environment, disabled optional locks, local-only command allowlist,
   all-ref digest, authoritative inventory/YAML validation, and refusal of
   detached/unborn/dirty/untracked/ignored/in-progress/incomplete/external-
   object repositories.
4. **Composite capture and artifact.** Test a successful stable A/B fence,
   complete bounded retry, bundle `--all` with local-only branches/tags/notes/
   stashes where supported, exact manifest binding, strict archive rejection,
   private unique artifact and sidecar, source immutability, helper/disk/Git
   failure, timeout/cancellation, and zero publication on every failure.
5. **Create-only restore.** Test `RestoreService` staging/provenance, local
   authorization, exact sentinel, complete prevalidation, safe database write,
   all-ref repository reconstruction, clean branch/HEAD/inventory, sanitized
   origin, modes/fsync, success result, and fail-closed existing/path/link/
   overwrite/network/image/remote/extraction/publication races.
6. **Real lifecycle and exact drill.** Exercise discovery, schema, target
   testing, backup, and `RestoreService` through the real application path,
   then execute the exact two-backup/two-restore drill below.
7. **Release and independent review.** Update compatibility/recovery docs and
   `CHANGELOG.md`, run every focused/full/backend/frontend/version gate, run
   Standards and Spec review from fixed point `e30c061`, resolve all P0/P1
   findings, and record final secret-free evidence before marking DONE.

## Exact two-backup/two-fresh-restore development drill

Run only on the development VM. All credentials, repositories, hosts, names,
rows, commits, refs, and Arr objects are synthetic. Use fresh temporary source
and restore directories, an intentionally unreachable credential-bearing
source remote, deterministic mock Radarr/Sonarr HTTP servers, and internal
Docker networks with no route to production or the general internet. Bundle
self-containment is proven locally; the plugin never contacts or depends on the
configured source remote. The backup and restore runners must have no Docker
socket, host root, production mounts, application/Git credential, or write
access to either source.

Exact-image discovery refined the fixture setup: v1.1.5 exposes read,
authentication, and Arr connectivity paths, but no coherent supported API for
authoring the complete Git-plus-SQLite state used by this drill. Seed each
synthetic phase only while the disposable source container is stopped, using
the exact migration and repository shapes, then boot the pinned image and prove
that it accepts and exposes that state before any live backup. This is a test
fixture boundary, not a backup or restore mechanism.

1. Build the backend runner and assert its Git and SQLite versions. Start the
   pinned Profilarr image with synthetic PUID/PGID and fresh `/config`; assert
   exact image digest, source revision/version, database migrations, and
   rollback-journal mode before continuing.
2. With the disposable source stopped, seed one exact regex, custom format,
   profile, and media-management definition; create the primary branch, tag,
   and local-only refs/commits; and return to a clean symbolic primary branch.
   Boot the pinned image, authenticate through its supported API, and verify all
   expected application and Git state before backup.
3. Seed one mock Radarr and one mock Sonarr target with different selected
   profiles/formats and deterministic schedules, plus language-import,
   auto-pull, and rename choices. Boot and prove the values through the exact
   application while both Arr endpoints are isolated protocol doubles. Record
   only hashes/counts and expected structural state in evidence.
4. Mount only `profilarr.db` and `db` read-only into a networkless backend
   runner. Run the real target probe and backup A while Profilarr stays live.
   Repeatedly perform one harmless supported DB-only update during the online
   copy so overlap is deterministic; no Git state may change during capture.
5. Independently verify A's private modes/sidecar, exact members, SQLite schema
   and integrity, complete prerequisite-free bundle, primary branch/HEAD/tag,
   the unpushed local ref and commit, Git `fsck`, inventory/YAML hashes, and
   manifest binding. Prove no log, native backup, `.git/config`, hook, source
   credential, internal URL, fixture secret, profile/ref name, commit message,
   or private inventory path appears in logs, errors, metrics, filenames, or
   sidecar.
6. Stop only the disposable source, mutate the profile and custom format, add a
   second profile, change Arr selection/schedule and language score, commit on
   the primary branch, and add a second local-only branch/ref. Restart the exact
   image and verify the changed state through its supported read APIs. Require a
   clean repository, then create and validate artifact B. Rehash A to prove it
   is immutable; require distinct database, bundle, manifest, and artifact
   hashes plus expected A/B counts and refs. This stopped fixture-authoring seam
   is deliberate: exact Profilarr 1.1.5 does not expose a coherent supported
   write API for all Git-managed state.
7. Through `RestoreService`, restore A and B into two different absent private
   roots using separate `--network none` runners. Prove each creates only
   `profilarr.db` and the reconstructed `db` repository, matches its manifest,
   contains all expected refs, has a clean worktree, and contains no copied
   source repository config, hooks, or credentials.
8. Boot one fresh exact pinned Profilarr instance against each restore on the
   internal mock network. Authenticate and prove the correct A or B settings,
   Arr targets/selections/schedules, branch/HEAD/tag/local-only commits, and
   regex/format/profile/media-management state; prove the opposite phase's
   mutations are absent. Restart each restored instance and repeat readiness
   and state proof.
9. Exercise supported application reads and Arr connectivity against the
   isolated mocks. Assert recovered state and mock request counts, and prove no
   Git, production, public, or unknown host was contacted. Never sync a
   production Arr.
10. In the exact drill, exercise representative dirty-Git, visible-WAL,
    tampered-artifact, and unauthorized-restore failures. The deterministic unit
    suite covers the concrete negative contract listed below; other malformed
    inputs remain fail-closed implementation constraints rather than separately
    claimed fixtures. Tear down every
    disposable container, image, network, volume, temp tree, and secret file,
    then assert each resource is absent.

Artifacts A and B and their two separate fresh restored instances are the two
consecutive recovery drills. Run the complete opt-in drill twice from clean
state before final review. Record only secret-free image/tool/version identity,
timings, artifact paths/sizes/hashes, sidecar and structural manifest results,
table/ref/file counts, A/B differences, readiness/restart results, mock request
counts, and teardown evidence.

## Required negative evidence

Automated tests plus the exact drill must prove these concrete failure classes:

- missing, wrong-type, symlinked, writable, or replaced source mounts; WAL/hot
  journal residue; corrupt SQLite; wrong columns or migrations; foreign-key
  failure; and bounded probe/backup timeout or repeated cancellation;
- unborn, detached, shallow, partial/promisor, corrupt, alternate-object,
  submodule, LFS, or replace-ref Git repositories; active lock, merge, rebase,
  cherry-pick, or bisect state; dirty/staged/untracked/ignored authority; and
  executable authoritative YAML;
- source identity or fence changes, one successful bounded retry, retry
  exhaustion, helper failure, and confirmed terminate-to-kill cleanup of Git
  descendants;
- duplicate, extra, traversal, link, encrypted, hash-mismatched, invalid
  manifest, invalid bundle, or invalid database artifact content, with no final
  artifact or sidecar on failure;
- source `.git/config` credentials excluded from artifact public metadata,
  diagnostics, logs, metrics, filenames, and sidecars;
- restore without verified `RestoreService` provenance, authorization,
  loopback-only isolation, a private regular sentinel, or an absent safe path;
  plus destination, parent, sentinel, artifact-path, and publication replacement
  races that preserve foreign state; and
- exact-drill proof that the backup runner has no network, writes neither
  read-only source mount, and uses no native backup/restore, Docker, or
  host-control seam.

## Commands and verification gates

Run backend commands from `backend/` through the existing virtual environment:

| Purpose | Command | Expected result |
| --- | --- | --- |
| Focused tests | `.venv/bin/pytest -q tests/plugins/test_profilarr_plugin.py tests/test_api/test_plugins_api.py tests/test_api/test_targets_api.py tests/test_core/test_restore_capabilities.py tests/test_repository_hygiene.py` | all selected tests pass |
| Exact drill | `RUN_PROFILARR_DOCKER_DRILL=1 .venv/bin/pytest -q -s tests/integration/test_profilarr_docker_drill.py` | two artifacts, two restores, two exact boots/restarts, clean teardown |
| Full backend | `.venv/bin/pytest -q` | all tests pass; only documented opt-in skips remain |
| Type check | `.venv/bin/mypy app tests` | exit 0, no issues |
| Format | `.venv/bin/black --check app tests` | exit 0 |
| Imports | `.venv/bin/isort --check-only app tests` | exit 0 |
| Version | `.venv/bin/python scripts/check_version.py` | synchronized valid SemVer |

Run frontend gates from `frontend/` because the new schema is rendered by the
Targets UI:

| Purpose | Command | Expected result |
| --- | --- | --- |
| Tests | `npm test -- --run` | all tests pass |
| Lint | `npm run lint` | exit 0 |
| Build | `npm run build` | exit 0 |

Finally run from the repository root:

```bash
git diff --check e30c061
git status --short
```

The diff check must be silent. Status may list only the files explicitly in
scope below.

## Scope

Implementation may modify only:

- `backend/Dockerfile`;
- `backend/app/plugins/profilarr/__init__.py`;
- `backend/app/plugins/profilarr/plugin.py`;
- `backend/app/plugins/profilarr/schema.json`;
- `backend/tests/plugins/test_profilarr_plugin.py`;
- `backend/tests/integration/test_profilarr_docker_drill.py`;
- `backend/tests/test_api/test_plugins_api.py`;
- `backend/tests/test_api/test_targets_api.py`;
- `backend/tests/test_core/test_restore_capabilities.py`;
- `backend/tests/test_repository_hygiene.py`;
- `.github/workflows/ci.yml`;
- `docs/PLUGIN_COMPATIBILITY.md`;
- `docs/RECOVERY.md`;
- `CHANGELOG.md`;
- `plans/013-profilarr-sqlite-git.md`; and
- `plans/README.md`.

Do not modify core artifact, scheduler, lock, target, or restore-service
interfaces unless an existing contract demonstrably cannot carry the required
safe behavior. If that occurs, stop and request a separately reviewed core
change instead of expanding this milestone. Do not modify `homelab-infra`,
frontend source, production configuration, release versions/tags, unrelated
plugins/tests, or any production system.

## Git workflow

- Use branch `feat/profilarr-backup` if a branch is requested.
- Follow Conventional Commits; the milestone commit should be
  `feat: add Profilarr recovery milestone`.
- Keep the milestone in one focused commit only after all gates and reviews
  pass. Do not push, open a PR, tag, release, or deploy unless explicitly asked.

## Release evidence

- The public discovery endpoint exposes `profilarr` version `0.2.1` with
  `restore_capability = "automatic"`; the schema endpoint exposes only the four
  flat, mode-aware fields and no restore destination default.
- Source and restore-destination target configurations persist byte-for-byte,
  while missing mode-specific fields and extra legacy fields fail schema
  validation.
- From `backend/`, `.venv/bin/pytest -q tests/test_api/test_plugins_api.py
  tests/test_api/test_targets_api.py tests/test_core/test_restore_capabilities.py
  tests/test_repository_hygiene.py` passed: `11 passed in 0.35s`.
- CI now executes `git --version` in the built backend image in addition to the
  repository-level Dockerfile package guard.
- The focused Profilarr suite covers 77 discovery, configuration, probe,
  backup, process-lifecycle, artifact, RestoreService, restore, and race cases.
- Two final clean exact-image drills passed after the complete schema fingerprint
  and sentinel hardening: `1 passed in 36.18s` and `1 passed in 34.22s`. Each
  created two distinct artifacts, restored them separately, booted and restarted
  the pinned exact image, proved A/B application and Git state, and cleaned all
  disposable resources.
- The final full backend suite passed: `885 passed, 8 skipped in 234.55s`.
- Frontend tests (`48 passed`), lint, build, version validation (`0.2.1`),
  focused mypy/Black/isort, and `git diff --check` passed.
- Independent Standards and Spec reviews from fixed point `e30c061` found no
  unresolved P0/P1 issue; the final Spec review was fully clean.
- These checks were local only; production was not contacted or changed.

## Done criteria

- [x] All seven TDD slices and the required negative evidence pass.
- [x] The backend image contains the required Git CLI and runs the plugin as the
      existing unprivileged application user.
- [x] Exact Profilarr 1.1.5 image/source, SQLite migrations/schema/journal mode,
      Git object/ref/inventory contract, artifact format, and limits are pinned.
- [x] Backup is live, filesystem-only, read-only, clean-fenced, bounded,
      cancellable, secret-safe, private, transactionally published, and
      sidecar-bound without native API, remote Git, Arr, Docker, or host access.
- [x] Restore is `RestoreService`-staged, completely revalidated, local-only,
      create-only, path-safe, private, independently hashed, Git-equivalent,
      and honestly `automatic` for complete Profilarr application state.
- [x] Two distinct artifacts and two fresh restores pass exact-image app-visible
      A/B state, local-only ref, mock integration, restart, negative, and clean
      teardown proof; the full drill then passes a second clean run.
- [x] Focused/full pytest, mypy, Black, isort, SemVer, frontend tests/lint/build,
      and `git diff --check` all pass.
- [x] `docs/PLUGIN_COMPATIBILITY.md`, `docs/RECOVERY.md`, and `CHANGELOG.md`
      document exact version, composite scope, secret sensitivity, clean-Git
      failure policy, automatic capability, local evidence, and later production
      prerequisites.
- [x] Standards and Spec review from fixed point `e30c061` has no unresolved
      P0/P1 finding; record exact secret-free commands/counts/timings in this
      plan and mark both Plan 013 status locations `DONE (local)`.
- [x] Production remains untouched. The handoff lists only later approved
      two-mount/read-permission/target/schedule and backup-only evidence work.
- [x] The completed milestone is committed independently only after review.

## STOP conditions

Stop and report; do not improvise, weaken checks, or add compatibility behavior
if:

- any production contact, write, restore, Arr sync, service stop/restart,
  filesystem mutation, or native backup/restore/import call would occur;
- the service/image is not exact v1.1.5, the source revision/digest differs, or
  a v2 database/repository/configuration appears;
- SQLite is WAL or non-default journal mode, a hot/recovery journal is present,
  the single-file read-only mount cannot provide a coherent online snapshot, or
  exact migrations/schema/integrity/foreign keys differ;
- the Git repository is dirty, detached, unborn, incomplete, changing,
  corrupt, shallow/partial, in an operation, or depends on submodules, LFS,
  alternates, replace refs, missing objects, or authoritative ignored/untracked
  data;
- the two narrow source paths are not exact read-only binds, escape/alias their
  allowlists, require UID 0/write access, or would require all `/config`, an app
  API key, Git PAT, Docker socket, host root, or a production network;
- validation cannot remain bounded, disk-backed, deterministic, secret-safe,
  inode/hash-bound, or safely cancellable with all worker descendants reaped;
- a credential, internal URL, SQL/YAML/repository value, profile/ref name,
  commit message, or private path would enter logs, errors, metrics, filenames,
  sidecars, or retained public evidence;
- the artifact would include `.git/config`, hooks, reflogs, logs, native
  backups, temporary files, live raw repository copies, journal residue, or
  anything other than the exact three members;
- restore cannot stay new, no-replace, sentinel-marked, loopback-only,
  exact-image-pinned, free of external connectivity, and safe against partial
  publication or foreign-state races;
- the linked repository URL in the source database is credential-bearing or
  uses an unsafe scheme, because a recoverable sanitized `origin` cannot then
  be proven without changing source state; or
- either A/B restore lacks an expected local-only ref/commit, cannot reproduce
  exact database/repository state, cannot boot/restart visibly on the pinned
  image, contacts a non-mock endpoint, or leaves disposable resources behind.

Future Profilarr versions, WAL mode, different schema/repository layout,
external Git object stores, dirty-state capture, or plugin-controlled service
lifecycle require new research and explicit approval. They are not fallbacks
for this plan.

## Maintenance notes

- Treat every Profilarr v2 migration as a new plugin contract; do not widen the
  v1 validator.
- If Profilarr moves to WAL, replace the single-file mount design with an
  audited companion-file or storage-snapshot boundary before taking another
  backup.
- If Git grows submodules, LFS, partial-clone, or alternate object storage,
  protect those external authorities separately before accepting the repo.
- A dirty/in-progress repository is an actionable failed backup. The operator
  commits, removes, or resolves it; the next scheduled run succeeds. Do not add
  a dirty-tree archive compatibility mode.
- Radarr, Sonarr, Git hosting, Git credentials, and artifact replication remain
  separate recovery responsibilities even though Profilarr's own recovered
  state is complete.
