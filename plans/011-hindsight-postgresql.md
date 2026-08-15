# Plan 011: Hindsight 0.8.6 PostgreSQL backup and isolated restore

## Executor instructions

Read `AGENTS.md`, `ADDING_PLUGINS.md`, and
`plans/research/hindsight.md` completely. The research note is the fixed
service boundary and primary-source record; stop on deployment/version/storage
drift instead of adding compatibility behavior. Work test-first in the vertical
slices below. Do not touch production during this local milestone.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: IN PROGRESS
- **Production status**: implementation and proof are local-only. Network,
  credential, target, schedule, and backup-run activation require a later
  user-approved production change. Production restore is always forbidden.

## Outcome and fixed scope

Add a `hindsight` plugin for exact Hindsight 0.8.6 on PostgreSQL 18/pgvector.
It takes one online, custom-format logical dump of the complete database,
validates it before transactional publication, and restores it only into a new,
empty, sentinel-marked PostgreSQL 18 database on the development VM.

The database is the single consistency boundary: this deployment uses native
`file_storage`, so uploaded bytes are PostgreSQL `BYTEA`. Exclude the external
Codex OAuth directory and deployment configuration. The dump is plaintext and
secret-bearing; publish it mode 0600, never expose content or credentials, and
rely on the separately managed backup destination for storage protection.

Declare `restore_capability = "partial"`: database materialization and
verification are complete, while independently managed OAuth/configuration and
the exact-image application boot remain explicit recovery prerequisites. The
local drill performs that boot proof.

## Exact plugin contract

### Configuration and compatibility

Create `backend/app/plugins/hindsight/` with one exported class and a flat
schema:

- required `mode`: `source` or `restore_destination`, default `source`;
- required non-empty `host`, `database`, `user`, and `password` strings; and
- integer `port`, default 5432, range 1-65535.

The password has no default. Reject URL-form connections, embedded credentials,
control characters, unsafe database identifiers, coercions, aliases, legacy
fallbacks, and versions other than the researched one. `backup()` accepts only
`source`; `restore()` accepts only `restore_destination`.

Using PostgreSQL 18 `psql -X`, a source must prove server major 18, configured
database identity, exact locally-pinned vector and Hindsight 0.8.6 Alembic
versions, no RLS on required tables, and the exact version-pinned schema object
allowlist from the vendor migrations/table inventory cited in the research.
That allowlist must include every persistent Hindsight table, `file_storage`,
`webhooks`, `alembic_version`, and `bank_stats_cache`, and reject omissions or
unexpected Hindsight-owned tables.

A restore destination must prove server/vector compatibility, an exact database
name prefix `hlb_hindsight_restore_`, database comment
`homelab-backup:hindsight-restore:v1`, and no non-system objects except the
preinstalled vector extension and its owned objects.

`test()` is read-only and returns `True` only after the mode-specific probe.
Use the canonical exception mapping and redact all tool/database diagnostics.
`get_status()` reports `ok` only after an actual probe; otherwise report a
checked error/unknown state without leaking connection details.

### Backup

Use a temporary 0600 `PGPASSFILE`, never a password argument or `PGPASSWORD`.
Use fixed subprocess arguments, bounded output/memory, connection/statement/
wall deadlines, and terminate plus reap child work on timeout or cancellation.

Stream PostgreSQL 18
`pg_dump --format=custom --no-owner --no-privileges` directly into
`create_backup_artifact()` with prefix `hindsight-postgresql` and suffix
`.dump`. Create the temporary dump mode 0600 before its first byte is written.
Treat nonzero exit or any stderr warning as failure. Before publication, run
bounded `pg_restore --list` inspection and require the exact server/service
schema fingerprint. Reject empty, malformed, truncated, incomplete,
wrong-service, or unexpectedly expanded archives. Return only the final
absolute `artifact_path`; the helper writes the sidecar.

### Create-only restore

Accept only the sidecar/hash-verified staged artifact from `RestoreService`.
Revalidate its bounded TOC before connecting, require distinct source and
destination targets, and require a disposable destination owner distinct from
the source read-only identity. Refuse a database name outside the dedicated
restore prefix, wrong/missing sentinel, any prior application object, non-empty
destination, or incompatible server/vector identity.

Generate an exact TOC allowlist that omits only vector extension creation and
its extension comment, because the disposable administrator preinstalls the
exact extension. Restore with `pg_restore --use-list`, `--single-transaction`,
`--exit-on-error`, `--no-owner`, and `--no-privileges`. Never use `--clean`,
`--create`, trigger disabling, error continuation, or shell execution.

On failure, transaction rollback leaves the sentinel database for inspection;
the plugin never drops or cleans it. On success, `ANALYZE`, then verify the
exact schema, constraints, valid indexes, Alembic/vector identity, and required
tables. The drill—not production plugin code—verifies synthetic fixture IDs,
counts, and native-file hashes. Return `status: success` with a message naming
the remaining exact-image boot/configuration proof; the declared restore
capability remains `partial`.

## Least privilege

The drill creates a source login limited to this database with `CONNECT`,
schema `USAGE`, and read access to required tables, views, and sequences. Start
with explicit database-local grants; use `pg_read_all_data` only if the drill
proves a narrower complete dump impossible. Assert denial of writes, object/
database/role creation, replication, RLS bypass, backend signalling, server
file/program access, and connection to an unrelated database. It is never
owner or superuser.

The runtime receives only PostgreSQL network access and PostgreSQL 18 clients:
no Docker socket/exec, Hindsight credential, OAuth directory, host/source mount,
root, source-owner, or writable source access. The destination owner exists
only in the disposable local drill; no production restore identity is created.

## TDD vertical slices

For each slice, add one observable failing test, implement only enough to pass,
then refactor with all earlier tests green.

1. **Client, discovery, and schema.** Prove the backend image contains major-18
   `psql`, `pg_dump`, and `pg_restore`; guard against regression. Add discovery,
   `partial` capability, mode-aware flat schema/API, strict validation, and
   diagnostic redaction tests.
2. **Compatibility probe.** Test fixed SQL, bounded machine output, exact
   source/destination fingerprints, RLS refusal, exception mapping, honest
   status, private password-file lifecycle, timeout, and cancellation.
3. **Transactional backup.** Test fixed argv/env, streaming and bounded memory,
   warning/failure handling, strict TOC parsing, unique private artifacts,
   valid sidecars, and zero publication after invalid output or cancellation.
4. **Restore preflight.** Test staged-artifact identity, immutable revalidation,
   sentinel/name/source-destination/pristine checks, the vector-only TOC
   omission, and fail-closed corrupt, incompatible, or ambiguous inputs.
5. **Transactional restore.** Test exact argv, separate private destination
   credential, rollback/no-clean behavior, exact post-restore verification,
   partial result, and child cleanup under timeout/cancellation.
6. **Real lifecycle.** Exercise discovery, schema, target testing, backup, and
   `RestoreService` through the real app path, then run the disposable drill.
7. **Release evidence.** Update compatibility/recovery docs and `CHANGELOG.md`,
   run all backend and applicable frontend gates, and complete Standards plus
   Spec review before marking this plan DONE.

## Disposable two-backup/two-fresh-restore drill

Run on the development VM only, using fresh private networks, synthetic
credentials/content, and new volumes. Pin the Linux/amd64 images recorded in
the research:

- Hindsight:
  `ghcr.io/vectorize-io/hindsight@sha256:47eba343fe1cc0feb30839fa9bae4d1bb592676a2e7a7c3b8c80689ac93fbf8c`
- PostgreSQL/pgvector:
  `pgvector/pgvector@sha256:ff8da7b0714e5efa413d77f43e24d93064dd66469d418d12608c1bbc91fcf045`

Use provider `none`, native file storage, and retained uploaded files. For one
drill sequence:

1. Migrate and boot exact Hindsight; pin app/server/client/vector/Alembic
   identities. Create distinct owner, read-only backup, and disposable restore
   roles, then prove the backup role's required denials.
2. Through first-party APIs create synthetic banks, memories/documents, a
   directive, a webhook with synthetic secret, and a native uploaded file.
   Keep supported writes active while the real plugin creates artifact A.
3. Mutate all representative state through supported APIs and create artifact
   B. Require distinct paths and SHA-256 values; independently verify both
   sidecars, modes, TOCs, object coverage, and nonzero data.
4. Create two fresh `template0` databases with exact vector and sentinels.
   Restore A into one and B into the other through `RestoreService`. Prove exact
   schema, fixture counts/IDs/hashes, vector and file bytes, plus the expected
   A-versus-B differences.
5. Boot a fresh exact Hindsight instance against each restore. Prove readiness,
   API-visible recovered state, file download hashes, and A/B differences.
   Restart both database and app destinations and repeat the proof.
6. Prove fail-closed behavior for old client, bad/unreachable/underprivileged/
   RLS source, dump warning/failure/cancellation, corrupt or wrong artifact,
   unsafe/non-empty destination, wrong sentinel/vector, and injected restore
   failure. No case may publish success or mutate pre-existing state.

Artifacts A and B, their two fresh restores, exact-image boots, and restart
checks are the two consecutive recovery drills required by this program. Tear
down only disposable resources. Retain secret-free identities, timings,
paths/sizes/hashes, sidecar/TOC results, fixture counts/hashes, readiness,
A-versus-B, and restart evidence.

## Done criteria

- [ ] All seven slices and all Hindsight contract tests pass without skips.
- [ ] Exact 0.8.6 server/vector/Alembic/schema compatibility is pinned locally.
- [ ] Backup is online, least-privileged, bounded, cancellable, secret-safe,
      transactionally published, TOC-validated, private, and sidecar-backed.
- [ ] Restore is local/fresh/sentinel-only, vector-allowlisted, transactional,
      independently verified, and honestly `partial`.
- [ ] Both consecutive recovery drills pass with independent evidence for the
      two artifacts, fresh restores, exact-image boots, and restarts.
- [ ] Focused and full backend pytest/mypy/Black/isort plus applicable frontend
      test/lint/build gates pass.
- [ ] Compatibility/recovery documentation and `CHANGELOG.md` are updated; no
      secret or production identity/data appears in code, fixtures, or evidence.
- [ ] Standards/Spec review has no unresolved P0/P1 finding; this milestone is
      independently committed and marked `DONE (local)`.
- [ ] Production remains untouched; handoff lists only later approved read-only
      role, network attachment, target/schedule, and backup-only proof work.

## STOP conditions

Stop rather than weaken or broaden the contract if the exact version,
PostgreSQL 18, pgvector, Alembic, schema, or native-storage boundary differs;
durable state exists outside PostgreSQL; exact dumping requires RLS bypass,
owner/superuser/write/Docker/host access; client/server compatibility, bounded
TOC coverage, secret safety, or plaintext destination protection cannot be
proven; vector is not safely preinstallable with the exact two-entry omission;
the destination is not provably local, new, empty, distinct, sentinel-marked,
and transactional; OAuth/configuration is not independently recoverable; or
either clean drill fails artifact uniqueness, restore atomicity, recovered
content/files, exact-image boot, restart, or teardown.

Also stop before any production mutation without explicit approval and before
any production restore under all circumstances. A future version or storage
topology requires new research and an explicit new contract, not a fallback.
