# Plan 005: SFTPGo v2.7.5 control-plane backup and isolated restore

## Status

- **Priority**: P0
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation contract
- **State**: DONE (local)
- **Production status**: BLOCKED until the local milestone and later
  infrastructure rollout are complete; production restore remains forbidden
- **Researched at**: `drakkan/sftpgo:v2.7.5-alpine`, upstream commit
  `9888a3d169aed9011ae6e4f7a97ae735c1643068`, and `homelab-infra`
  `eeed77a`, 2026-08-15

## Outcome and boundary

Add one `sftpgo` plugin that snapshots the exact v2.7.5 SQLite data-provider
database through a read-only filesystem mount. It protects users, public keys,
admins, groups, virtual folders, shares, API keys, roles, IP lists, event
configuration, quotas, and provider settings without downtime or a SFTPGo
administrator credential.

Publish one private standalone SQLite artifact plus the normal sidecar. Remove
only active transfers, shared sessions, task locks, and Defender history from
the copied snapshot. The plugin excludes `/srv/sftpgo`, every `/nas/*` payload,
generated SSH host keys, infrastructure declarations, logs, and other transient
runtime state.

The exact rationale, rejected native API design, and source evidence are in
`plans/research/sftpgo.md`.

## Verified deployment

The Docker VM runs `drakkan/sftpgo:v2.7.5-alpine` as UID/GID `1000:1000` with:

- `/docker-apps/sftpgo/config:/var/lib/sftpgo` for `sftpgo.db` and generated
  working files;
- `/docker-apps/sftpgo/data:/srv/sftpgo` for user homes/exports;
- five read-only NAS media mounts;
- SFTP, FTP, and WebDAV disabled; and
- WebClient/API plus WebAdmin/API HTTP bindings.

The future Homelab Backup deployment needs only:

```text
/docker-apps/sftpgo/config:/sources/sftpgo/config:ro
```

No network attachment, service credential, Docker socket, or downtime is part
of this plugin.

## Public interface

Keep the target schema flat:

- `database_path`: required absolute path named `sftpgo.db`, with
  `/sources/sftpgo/config/sftpgo.db` as the deployment hint.

For an existing database, `test()` opens it read-only and requires exact schema
version 33, all v2.7.5 provider tables/columns, at least one administrator,
`PRAGMA quick_check = ok`, and no foreign-key violations. For a nonexistent
database, it succeeds only for a safe, writable, otherwise-empty isolated
restore directory bearing the fixed v1 sentinel. Errors must not reveal stored
rows or credentials.

Declare `restore_capability = "partial"`: the plugin proves a correct offline
database creation but does not control or claim destination application
readiness.

## Backup contract

`backup()` must:

1. validate the source before work and refuse symlinks or non-regular files;
2. create a cancellation- and deadline-aware snapshot using SQLite's online
   backup API while the source stays live;
3. clear declared transient tables inside the snapshot only;
4. normalize the artifact to a standalone database with no WAL/SHM dependency;
5. validate schema version, tables/columns, admin presence, integrity, foreign
   keys, transient-table emptiness, and private mode independently;
6. transactionally publish a unique artifact under
   `/backups/<target_slug>/<YYYY-MM-DD>/`; and
7. return `{ "artifact_path": "..." }` after the sidecar exists.

No backup path or record may survive a timeout, cancellation, validation
failure, or sidecar failure.

## Offline create-only restore

Restore must validate the staged source before destination mutation and require:

- a canonical absolute `sftpgo.db` path;
- no symlink in any existing path component;
- a parent containing only `.sftpgo-restore-destination` with the exact v1
  marker;
- no destination DB, WAL, or SHM file; and
- no overlap with the artifact, `/backups`, `/sources/sftpgo`, or
  `/var/lib/sftpgo`.

Copy through a private same-filesystem staging file, fsync, revalidate, publish
create-only, fsync the directory, and validate again. A failure after publication
must remove the new destination. Success returns `partial` and tells the operator
to boot and verify the exact v2.7.5 image in isolation.

## Test-first slices

Work vertically through these public seams:

1. Discovery, schema route, capability, exact flat config, and invalid paths.
2. Existing-source connectivity: schema version, required tables/columns,
   integrity, foreign keys, admin presence, and secret-safe failures.
3. Fresh sentinel-marked destination connectivity and forbidden-root refusal.
4. Live SQLite online snapshot, transient-state scrubbing, private artifact,
   unique path, and valid sidecar.
5. Timeout/cancellation and no partial artifact/temporary-file leakage.
6. Corrupt, incomplete, wrong-version, symlinked, and non-private artifact
   rejection.
7. Create-only restore, exact byte/hash/schema/content evidence, and DB/WAL/SHM
   collision refusal.
8. Failure injection before and after publication with deterministic cleanup.
9. Real `/api/v1/plugins`, schema, and test-route behavior.
10. Two consecutive exact-image local backup-to-fresh-restore drills.

## Exact-image local drill

Use the exact public v2.7.5 Alpine digest on a disposable Docker internal
network with synthetic credentials and host directories under pytest's temp
root. Never mount production, NAS, `/var/run/docker.sock`, or any real service
path.

Seed phase one through SFTPGo's own API with a synthetic administrator, user,
public key, group, folder, share, API key, role, and event metadata. Keep the
source running, exercise a read-only database view, and take artifact 1. Add
phase-two objects and take artifact 2. Require unique paths and hashes and prove
the expected phase difference.

Restore each artifact to a different fresh sentinel-marked directory, boot a
different exact-image container, and prove process readiness, exact version,
administrator authentication, SQLite provider availability, expected semantic
objects/keys, and empty transient tables. Destination 1 must lack phase-two
state; destination 2 must contain it.

## Production gate

Local work ends without contacting production. After the release is deployed
and the user explicitly approves configuration writes:

1. pin the SFTPGo image digest in `homelab-infra`;
2. add the one read-only source mount to the Docker-host backend;
3. create and schedule the target;
4. run only non-destructive connectivity and backup validation; and
5. read-only inspect `/srv/sftpgo/data` to classify any unique payload.

No production restore is permitted.

## Done criteria

- [x] All ten test-first slices pass.
- [x] Two consecutive exact-image backup-to-fresh-restore drills pass.
- [x] Full backend and frontend checks pass.
- [x] Standards/spec review has no unresolved P0/P1 findings.
- [x] Compatibility/recovery/changelog documentation records exact version,
      DB-only scope, sensitive artifact handling, and local evidence.
- [x] The milestone is committed independently with this plan marked DONE.

## STOP conditions

Stop before expanding scope if `/srv/sftpgo/data` contains unique payload,
SFTP/host-key continuity becomes required, the provider stops being exact
SQLite v2.7.5 schema 33, the restore destination is not fresh/isolated, a
production change is required before local verification is complete, or any
design would need downtime, network administration, Docker control, or an
unrestricted SFTPGo credential.
