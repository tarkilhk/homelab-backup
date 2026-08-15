# Plan 009: Audiobookshelf 2.36.0 control-plane backup and isolated restore

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: IN PROGRESS
- **Production status**: research and local implementation only; production
  restore remains forbidden
- **Exact local-drill image**:
  `ghcr.io/advplyr/audiobookshelf@sha256:180acad33d69c99ed208676465d8edcb268fa46967735579a7810859885b1a8e`

## Outcome

Add one `audiobookshelf` filesystem plugin that snapshots the exact 2.36.0
control plane through read-only `/config` and `/metadata` views. It must create
an online SQLite snapshot, include only native-format item/author metadata,
prove every included database reference is usable, publish a private validated
artifact and sidecar, and restore only into fresh isolated sentinel-marked
config/metadata destinations.

Audiobook and ebook payload remains excluded and externally protected. The
authoritative boundary, native-API rejection, online consistency limit, and
exact source evidence are documented in `plans/research/audiobookshelf.md`.

## Public contract and seams

The existing `BackupPlugin` methods remain the public interface. Keep the flat
target schema to `config_path` and `metadata_path`, defaulting to
`/sources/audiobookshelf/config` and `/sources/audiobookshelf/metadata`.
Discovery and schema remain observable through `/api/v1/plugins`.

`test()` is strictly read-only. It validates canonical non-symlink roots,
opens `absdatabase.sqlite` with SQLite `mode=ro`, and requires exact 2.36.0
migration metadata, integrity, foreign-key, schema, and root-user evidence.

`backup()` uses SQLite's online backup API rather than copying the live DB. It
captures bounded before/after metadata manifests and a second DB-reference
snapshot, retries on change, validates referenced images/JSON, then publishes
one private strict `.audiobookshelf` ZIP transactionally. It guarantees a
usable, referentially complete control-plane artifact, not a byte-exact instant
for presentation metadata whose paths—but not hashes—are stored in SQLite.

`restore()` is create-only and local-only. It verifies the complete artifact
before mutation and atomically materializes `absdatabase.sqlite`, `items/`, and
`authors/` into two fresh sentinel-marked destinations. It never controls or
overwrites a running Audiobookshelf instance, so `restore_capability` is
`partial`.

## Test-first vertical slices

1. Discovery, flat schema, strict source/destination path validation, partial
   restore capability, honest status, and secret-safe errors.
2. Read-only exact-version SQLite validation: online snapshot, required
   schema/columns, root user, migration version, quick-check and foreign keys.
3. Database-reference classification and strict metadata tree handling:
   included item/author paths, explicitly external media paths, valid images
   and JSON, and refusal of symlinks, unknown roots, or missing references.
4. Bounded stable-read algorithm with before/after file evidence and a second
   DB-reference snapshot; deterministic retry and continuously changing-source
   refusal.
5. Private native-shape ZIP, strict manifest/member/count/size/hash validation,
   transactional publication, unique artifacts, and valid sidecars.
6. Backup/validation timeout and repeated-cancellation hard stops with no
   surviving child work, decrypted database copy, archive, or partial artifact.
7. Strict create-only two-root restore with exact sentinels, archive safety,
   ownership-safe publication/rollback, forbidden/overlapping source and
   destination refusal, and timeout/cancellation/concurrency races.
8. Real plugin discovery/schema/test routes plus two distinct exact-image
   backups, two fresh restores, two exact-version boots, login/state/reference
   checks, media exclusion, restart persistence, and A-versus-B evidence.

## Done criteria

- [ ] All eight vertical slices pass.
- [ ] The backend runner reads both sources through genuine OS read-only binds
      and has no network, Docker socket, Audiobookshelf credential, or media
      mount.
- [ ] Two exact-version backup-to-fresh-restore drills pass.
- [ ] Full backend and applicable frontend checks pass.
- [ ] Standards/spec review has no unresolved P0/P1 findings.
- [ ] Compatibility, recovery, changelog, and future infrastructure mounts are
      documented.
- [ ] The milestone is committed independently with this plan marked DONE.

## STOP conditions

Stop before compatibility behavior for any version other than exact 2.36.0,
raw live SQLite copying, database-only success, inclusion of media payload,
native admin/API fallback, an overwrite/merge restore, writable source access,
Docker socket or broad host access, or any production restore. Stop if the
source DB/schema/version is unexpected, references escape the approved roots,
metadata cannot stabilize boundedly, or exact cross-component point-in-time
semantics become mandatory without an approved quiescence/snapshot design.
