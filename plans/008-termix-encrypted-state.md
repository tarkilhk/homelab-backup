# Plan 008: Termix 2.3.2 encrypted-state backup and isolated restore

## Status

- **Priority**: P0
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: IN PROGRESS
- **Production status**: local implementation only; production restore remains forbidden
- **Researched at**: Termix 2.3.2 / upstream commit
  `c3282b5dca081d52513e94329bbc71084338217d`, 2026-08-15
- **Exact local-drill image**:
  `ghcr.io/lukegus/termix@sha256:06a27a3dc22ae426cf0681fcdbdb58732f2aab56d8ce9e95f4deea18306e5c2f`

## Outcome

Add one `termix` filesystem plugin that snapshots only the exact authoritative
2.3.2 state from a read-only data-directory view, proves the encrypted database
can be decrypted and read as a valid Termix SQLite database, publishes a
private validated archive and sidecar, and restores only into a fresh isolated
sentinel-marked directory.

The authoritative allowlist, persistence limitation, rejected native export,
and exact source evidence are documented in `plans/research/termix.md`.

## Public contract and seams

The existing `BackupPlugin` methods are the external module interface. Keep the
flat target configuration to one `data_path`, defaulting to
`/sources/termix/data`. Discovery and schema remain observable through
`/api/v1/plugins`.

`test()` is read-only and proves that `.env` and `db.sqlite.encrypted` are
private regular files, the exact v2/AES-256-GCM envelope authenticates with the
stored `DATABASE_KEY`, and the decrypted database passes integrity, foreign-key,
and minimum Termix 2.3.2 schema checks. Optional `.opk/config.yml` is accepted;
unknown persistent entries are refused.

`backup()` returns the latest successfully persisted Termix snapshot, not an
unwritten in-memory mutation. It stable-reads the allowlist after the exact
application's save debounce, retries boundedly, validates a private copy, and
publishes one private archive transactionally.

`restore()` is create-only and local-only. It accepts only an exact plugin
artifact, verifies its manifest and payload again, and atomically materializes
the allowlist into a fresh sentinel-marked destination. It never controls or
overwrites a running Termix instance, so `restore_capability` is `partial`.

## Test-first vertical slices

1. Discovery, flat schema, strict source/destination path validation, and
   secret-safe configuration errors.
2. Exact v2/AES-256-GCM envelope parsing and authentication using an independent
   known-answer fixture.
3. SQLite integrity, foreign-key, and required-table validation through
   `test()`, including wrong key, corrupt envelope, legacy layout, symlink,
   permissions, and unknown-entry refusal.
4. Stable read-only snapshot with bounded settle/retry behavior, restrictive
   modes, a manifest with independent hashes, transactional publication, and a
   valid sidecar.
5. Backup timeout/cancellation cleanup with no sensitive temporary file or
   child work surviving the terminal result.
6. Strict archive member/type/count/size/path/duplicate and manifest validation,
   including malformed, incomplete, cross-version, and malicious artifacts.
7. Fresh local sentinel restore, destination collision/symlink/forbidden-root
   refusal, atomic create-only publication, and ownership-safe rollback for
   timeout, cancellation, validation failure, and publication races.
8. Real discovery/schema/test routes and honest unknown runtime status.
9. Two consecutive backups from an exact 2.3.2 source data directory, followed
   by two fresh exact-image boots with authenticated representative-record
   retrieval and independent size/hash/content/readiness evidence.

## Done criteria

- [ ] All nine vertical slices pass.
- [ ] The backend runner reads the Termix source through a genuine OS read-only
      bind and has no network, Docker socket, or Termix application credentials.
- [ ] Two exact-version backup-to-fresh-restore drills pass.
- [ ] Full backend and applicable frontend checks pass.
- [ ] Standards/spec review has no unresolved P0/P1 findings.
- [ ] Compatibility, recovery, changelog, and future infrastructure mount are
      documented.
- [ ] The milestone is committed independently with this plan marked DONE.

## STOP conditions

Stop before compatibility behavior for any Termix version other than exact
2.3.2, v1/two-file/unencrypted database layouts, SSL-enabled state, unknown
persistent entries, an overwrite restore, zero-second RPO, application/Docker
credentials, production downtime, production mutation, or any production
restore. Those are separate product or privilege decisions.
