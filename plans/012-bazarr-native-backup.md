# Plan 012: Bazarr 1.5.6 native backup and isolated restore

## Executor instructions

Read `AGENTS.md`, `ADDING_PLUGINS.md`, and `plans/research/bazarr.md`
completely. The research note is the fixed service boundary and primary-source
record. Work test-first through the vertical slices below. Stop on version,
storage, API, schema, or path drift instead of adding compatibility behavior.
Do not contact or change production during this local milestone.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: DONE (local)
- **Production status**: implementation and proof are local-only. A later,
  explicitly approved production change must verify SQLite mode, provide the
  API secret and network route, and mount only Bazarr's native backup directory
  read-only. Production restore is always forbidden.

## Outcome and fixed scope

Add a `bazarr` plugin for exact Bazarr 1.5.6 in LinuxServer image
`v1.5.6-ls349`. It uses Bazarr's native online SQLite snapshot by calling only
`GET /api/system/status`, `GET /api/system/backups`, and
`POST /api/system/backups`, attributes exactly one new stable native ZIP to each
run, validates the ZIP and both of its members, then publishes it
transactionally with its sidecar.

Restore is create-only and local: strictly revalidate a `RestoreService`-staged
artifact, materialize only `config/config.yaml` and `db/bazarr.db` beneath a
new isolated destination, and prove the restored control plane by booting the
exact image during the development drill. Declare
`restore_capability = "partial"` because media, external or embedded subtitle
files, Sonarr, Radarr, proxying, and NAS state remain independent recovery
prerequisites.

Do not back up `/Movies`, `/TVShows`, subtitle payloads, the complete `/config`
tree, historical Bazarr backups, logs, caches, container state, or deployment
configuration. Do not call Bazarr's `PATCH` restore or `DELETE` operation. Do
not implement PostgreSQL mode or any other Bazarr/image version.

## Exact plugin contract

### Configuration and non-destructive connectivity

Create `backend/app/plugins/bazarr/` with one exported class and a flat schema.
Use a required `mode` enum with `source` (default) and
`restore_destination`. Source mode requires:

- `base_url`: one absolute HTTP(S) origin with no credentials, query, fragment,
  control characters, or origin-changing redirect;
- `api_key`: one non-empty secret with no default;
- `backup_directory`: one literal absolute path to the dedicated read-only
  Bazarr native-backup mount.

Restore-destination mode requires only `restore_directory`: one absent path
beneath an existing private parent containing the exact isolated-restore
sentinel. Keep connect, request, poll, stability, and restore deadlines as
fixed documented implementation limits rather than expanding the user-facing
schema.

Reject coercions, aliases, legacy keys, implicit directory guesses, broad
`/config` paths, symlinks, traversal, and paths outside the one configured
mount. In source mode, `test()` sends authenticated `GET /api/system/status`
and `GET /api/system/backups` requests with the API key only in `X-API-KEY`,
proves the runtime is exact Bazarr 1.5.6 in SQLite mode, validates the exact
backup-list response shape, and proves the literal local directory is a genuine
read-only mount without writing to it. In restore-destination mode, `test()`
performs only path/sentinel/create-only preflight and no network call. Return
`True` only after the mode-specific check; otherwise use the canonical
exception mapping and secret-safe diagnostics. `get_status()` reports `ok`
only after the same real probe.

### Native backup state machine

`backup()` must use the centrally serialized target execution and this bounded
protocol:

1. Reconfirm exact Bazarr 1.5.6 and SQLite mode through the authenticated status
   response, then capture both the authenticated list response and a local
   directory baseline. Accept only regular, non-link files whose parent and
   resolved path remain inside the exact configured directory.
2. Record a monotonic boundary and send exactly one authenticated
   `POST /api/system/backups`. Treat its 204 response only as queue acceptance.
3. Poll the GET endpoint and read-only directory until exactly one new basename
   appears in both views and matches Bazarr 1.5.6's native
   `bazarr_backup_v<version>_<timestamp>.zip` naming contract. Require the
   version to be 1.5.6.
4. Require consecutive stable size/metadata observations, then open the same
   source inode safely and prove it does not change while streamed. Fail on no
   candidate, multiple candidates, collision, overlap, disappearance, link,
   path escape, instability, or deadline rather than selecting “latest.”
5. Stream only that source into `create_backup_artifact()` with a private ZIP
   artifact. Never rename, delete, retain, or modify the Bazarr source file.
   Validate before the helper publishes; return only the final absolute
   `artifact_path` after its sidecar exists.

Every HTTP transport test must spy on method and origin. The plugin may send
only GET to `/api/system/status`, and GET and POST to
`/api/system/backups`, on the configured origin. It must not follow an
unvalidated redirect and must have no PATCH, DELETE, upload,
production-restore, Docker-socket, or host-control path.

### Strict artifact validation

Treat every native ZIP as untrusted and bound all work. Require:

- a valid, unencrypted ZIP with passing CRCs and bounded member count,
  compressed bytes, uncompressed bytes, and expansion ratio;
- exactly two distinct regular root members, `bazarr.db` and `config.yaml`,
  with no extras, directories, links, devices, duplicate names, nested or
  absolute paths, traversal, ambiguous trailing archive, or empty member;
- safely parsed YAML with the exact expected top-level structure for 1.5.6,
  SQLite effective mode, and no PostgreSQL enablement, without returning or
  logging configuration values;
- a SQLite header, read-only `PRAGMA quick_check` and
  `PRAGMA foreign_key_check`, exact pinned Bazarr/Alembic tables and migration
  state learned from the exact image, no migration/temp residue, and bounded
  sane table counts; and
- an immutable source read plus final artifact digest/size and a valid,
  secret-free sidecar. The sidecar may contain only approved structural
  validation facts and must never contain media/subtitle paths, rows,
  credentials, notifier URLs, provider identities, or YAML values.

Reject config-only archives, including Bazarr's output when PostgreSQL is
enabled or SQLite snapshot creation failed. Keep validation streaming or
disk-backed; never expand an artifact-sized payload into process memory.

### Partial create-only restore

Accept only the independently staged and hash-verified artifact supplied by
`RestoreService`. Re-run the complete ZIP, YAML, and SQLite validation before
creating the destination. Restore is enabled only for the isolated local-drill
runtime and must reject production-looking paths, links, path escapes, an
existing destination, an unpinned verification image, or external/production
network reachability.

Create one new private destination root and safely stream only:

- `config.yaml` to `config/config.yaml`; and
- `bazarr.db` to `db/bazarr.db`.

Use create-exclusive files, private modes, bounded copies, flush/fsync files
and directories, and verify their hashes against the ZIP members. Never
overwrite, merge, or invoke Bazarr's native PATCH restore. On failure, report
no successful restore and leave no partially usable destination. Return an
honest `partial` result: plugin materialization is complete, while the exact
image boot and the separately managed Arr/media/subtitle prerequisites remain
explicit recovery checks.

## Test-first implementation slices

Each slice starts with one failing observable test, adds only enough behavior
to pass, then retains its negative cases before moving on.

1. **Contract and discovery.** Add package export, exact metadata,
   `restore_capability`, flat schema, loader discovery, schema API, and config
   validation tests.
2. **Read-only probe.** Test strict origin/path validation, exact 1.5.6 SQLite
   status and backup-list responses, read-only directory proof,
   auth/network/protocol errors, redaction, bounded timeout/cancellation, and
   truthful status.
3. **Trigger and attribution.** Test the baseline, one POST, bounded GET polling,
   exact shared basename/version, stable source identity, collision/overlap/
   mutation failures, and a spy proving no method other than GET/POST and no
   other origin is reachable.
4. **Transactional artifact.** Test every ZIP/YAML/SQLite invariant, bounded
   resource limits, private unique artifacts, sidecars, immutable source reads,
   helper/disk/cancellation failures, and zero publication for invalid output.
5. **Create-only restore.** Test `RestoreService` staging and provenance, absent
   local destination enforcement, safe two-file materialization, hashes/modes/
   fsync, partial result, and fail-closed existing/path/link/network/image/
   extraction cases.
6. **Real lifecycle and drill.** Exercise discovery, schema, target testing,
   backup, and `RestoreService` through the real app path, then run the exact
   disposable drill below twice in one sequence.
7. **Release evidence.** Update compatibility/recovery documentation and
   `CHANGELOG.md`; run focused and full backend gates plus applicable frontend
   gates; complete Standards and Spec review before marking this plan DONE.

## Exact two-backup/two-fresh-restore development drill

Run only on the development VM with synthetic secrets, names, rows, media, and
subtitle content. Pin Linux/amd64 image:

`ghcr.io/linuxserver/bazarr@sha256:4b00f5886f3307563cf06c1068037eccfc529f04070d42e2aa47f53128eed17e`

Use a fresh private network with no route to production or general internet and
fresh temporary `/config`, `/Movies`, and `/TVShows`. Use synthetic unreachable
Arr/notifier values; the drill must never contact those integrations. Confirm
Bazarr 1.5.6 and LinuxServer ls349 before testing; otherwise stop.

1. Let the exact image initialize and migrate its own SQLite schema. Bazarr does
   not expose deterministic, side-effect-free writers for every representative
   control-plane class. Stop only the disposable source, seed synthetic values
   into its exact migrated database and YAML, restart it, and verify every
   representative profile, language, notifier, catalog, history, and blacklist
   value through Bazarr's read APIs before proceeding. This fixture setup is
   not a production backup or restore path.
2. Run the real plugin probe and create artifact A. Prove exactly one POST and
   one uniquely attributed stable source ZIP, exact two-member validation,
   private artifact/sidecar, and absence of fixture secrets and private paths
   from observable metadata.
3. Mutate every representative control-plane class and the separate synthetic
   subtitle/media fixture, then create artifact B. Prove A is immutable and A/B
   paths, hashes, native database/config state, and expected counts differ.
4. Through `RestoreService`, restore A and B into two different absent private
   roots. Prove each contains only the exact two reconstructed files with bytes
   matching its artifact and that no external payload bytes entered either
   backup.
5. Boot one fresh exact pinned Bazarr instance against each restore with
   outbound traffic denied. Through read APIs/UI prove the expected A or B
   profiles, languages, history, blacklist, notifier structure, counts, and
   A/B differences. Restart each restored instance and repeat readiness/state
   proof.
6. Run an independent hash-based prerequisite classifier against the matching
   separate synthetic media/subtitle fixture, then against missing and swapped
   fixtures. Require explicit `matching`, `missing`, and `mismatched` verdicts;
   the plugin itself must continue to report only `partial` and must never claim
   that excluded payload was restored.
7. Exercise representative fail-closed cases for auth, no/multiple/unstable
   candidate, corrupt or malicious ZIP, PostgreSQL/config-only archive,
   existing/unsafe destination, network escape, image drift, unreachable
   synthetic integration configuration under denied egress, and injected extraction failure.
   Tear down only disposable local resources
   and retain only secret-free evidence.

Artifacts A and B and their two distinct fresh restored instances constitute
the two consecutive backup-to-restore drills. Record secret-free image/version
identity, artifact paths/sizes/hashes, sidecar validation, structural
table/member counts, readiness, expected A/B differences, partial-payload
outcomes, restart results, timings, and clean teardown.

## Done criteria

- [x] All seven slices and all Bazarr contract tests pass; the exact Docker
      drill is an intentional opt-in test.
- [x] Exact Bazarr 1.5.6/LinuxServer ls349 API, filename, ZIP, YAML, SQLite,
      Alembic, and image compatibility is pinned locally.
- [x] Backup is online, GET/POST-only, bounded, cancellable, secret-safe,
      uniquely attributed, transactionally published, private, and sidecar
      backed without modifying Bazarr's native backup directory.
- [x] Restore is RestoreService-staged, strictly revalidated, local-only,
      create-only, path-safe, private, independently hashed, and honestly
      `partial`.
- [x] Both consecutive local recovery drills pass with distinct artifacts,
      fresh restores, exact-image app-visible A/B state, restart proof, partial
      payload proof, negative cases, and clean teardown.
- [x] Focused and full backend pytest/mypy/Black/isort plus applicable frontend
      test/lint/build gates pass.
- [x] Compatibility/recovery documentation and `CHANGELOG.md` are updated; no
      secret or production identity/data appears in code, fixtures, or evidence.
- [x] Standards/Spec review has no unresolved P0/P1 finding; this milestone is
      independently committed and marked `DONE (local)`.
- [x] Production remains untouched; the handoff lists only later approved
      SQLite/mount/network/credential/target/schedule and backup-trigger work.

## Local completion evidence

Recorded on 2026-08-16 against the final reviewed worktree:

- focused Bazarr/core/API suite: 118 passed;
- exact pinned-image drill pass one: 342.49 seconds;
- exact pinned-image drill pass two: 340.65 seconds;
- full backend suite: 805 passed and 7 intentionally skipped;
- backend application Black/isort/mypy: 97 source files clean;
- applicable frontend baseline: 48 tests, lint, and production build passed;
- SemVer validator: 0.2.1; `git diff --check`: clean;
- Standards and Spec reviews: no remaining actionable P0-P3 findings; and
- teardown audit: no disposable Bazarr containers, networks, or runner images.

The milestone is locally complete only. Production still requires the explicit
read-only/native-backup mount, SQLite-mode, network, credential, target, job,
and backup-trigger approvals described above. Production restore remains
forbidden.

## STOP conditions

Stop rather than weaken, guess, or broaden the contract if:

- any restore/drill endpoint or destination could be production, any restore
  would mutate production, or the disposable network has production/general
  internet reachability;
- exact Bazarr 1.5.6/LinuxServer ls349, native filename/API shape, image digest,
  YAML structure, SQLite schema/Alembic/integrity, or two-member ZIP contract
  differs;
- effective PostgreSQL mode is enabled or the native archive lacks either
  `bazarr.db` or `config.yaml`;
- a unique stable source ZIP cannot be attributed to the single POST, including
  overlap, multiple files, collision, disappearance, timeout, or mutation;
- the native backup path is not the exact dedicated read-only allowlisted mount,
  traverses a link, escapes its directory, or needs write access;
- implementation would call PATCH/DELETE, upload/restore into Bazarr, stop a
  production service, or require `/config`, media mounts, root, host access, or
  a Docker socket;
- validation cannot be bounded, secret-safe, exact, and sidecar-bound, or any
  media/subtitle payload is proposed for inclusion;
- the restore root is not new, private, create-only, local, exact-image pinned,
  and externally isolated, or safe failure cannot avoid a partially usable
  destination; or
- either artifact/restore cannot prove unique hashes, exact reconstructed
  bytes, application-visible A/B state, restart survival, separate-payload
  dependency, and complete disposable teardown.

Also stop before any production mutation without explicit approval and before
any production restore under all circumstances. Future Bazarr/image versions,
PostgreSQL mode, or a changed storage/API topology require new research and a
new explicit contract, never a compatibility fallback.
