# Plan 004: Standard Notes composite backup and isolated restore

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation contract
- **State**: BLOCKED pending explicit approval for a short scheduled production
  downtime window
- **Local status**: Research and drill design complete; implementation has not
  started because the production consistency boundary is a public contract
  decision
- **Researched at**: `homelab-backup` v0.2.1 and `homelab-infra`
  `eeed77a`, 2026-08-15

## Outcome

Add one `standard_notes` composite plugin that captures the entire configured
MySQL schema and the filesystem uploads tree as one transactional artifact.
Restore only into a fresh isolated local destination, recreate Redis and
LocalStack empty, boot the exact source server image, and prove database and
upload content through functional API and independent hash checks.

This plugin must never publish an online best-effort database/filesystem copy
as a consistent Standard Notes backup. Production restore remains forbidden.

## Verified deployment facts

- Server image:
  `standardnotes/server:latest@sha256:6b371bc0c3ae755500b82f4c580fae7fd768e5f9214cdc2f195fc130cf6ffdcd`.
- The image contains API Gateway 1.92.2, Auth 1.178.6, Files 1.38.3,
  Syncing 1.136.5, and Revisions 1.51.19. Their upstream release tags map to
  source commit `162a63ae2bb926e1f061d3967dbe98878f0aedb7`.
- MySQL is declared as `mysql:8.4.0-oraclelinux8`; the complete
  `standard_notes_db` schema is authoritative.
- Uploaded bytes are stored under the server's mounted filesystem uploads
  tree. They are authoritative even when the tree is empty.
- Redis holds expiring cache/session/upload coordination. LocalStack provides
  only non-persistent SNS/SQS transport. Both are deliberately excluded and
  recreated empty.
- Server-side cryptographic secrets and deployment configuration are external
  recovery prerequisites. They must not be embedded in or logged from the
  artifact.
- The primary Homelab Backup backend can reach Standard Notes MySQL but does
  not have the required read-only uploads mount or a service-quiescence
  mechanism. The NAS backend is not a candidate for this target.

Primary-source analysis is recorded in
`plans/research/standard-notes.md`.

## Proven consistency boundary

The exact deployed server enforces no transaction between synced `SN|File`
items in MySQL and uploaded bytes on the filesystem:

- upload finalization appends chunks directly to the final pathname before
  publishing its cross-service event;
- `SN|File` item insertion is a separate sync operation and does not validate
  the filesystem object;
- deletion unlinks the filesystem object separately from the MySQL item's
  soft deletion;
- moves rename the object separately from the database association change;
- failed finalization can leave a partial file at its final pathname, and
  MySQL stores no plaintext upload size or hash that could prove completeness.

Repeated online dumps and filesystem reconciliation may detect some races and
fail closed, but they cannot guarantee both bounded completion and semantic
file completeness. The exact server and vendor documentation expose no global
maintenance, drain, snapshot, or read-only API.

Therefore a reliable backup requires this approved boundary:

1. reject new ingress and allow active requests to drain;
2. stop the single Standard Notes server container while leaving MySQL up;
3. prove both API and files endpoints are unavailable;
4. dump MySQL and archive/hash the now-static uploads tree;
5. validate and atomically publish the composite artifact and sidecar;
6. restart the server and prove functional readiness.

The plugin should not receive an unrestricted Docker socket. Prefer an
external host-side coordinator with a narrowly defined Standard Notes
stop-trigger-wait-start workflow. Its exact interface is part of the production
design gate after downtime approval.

## Public plugin contract

### Flat schema

Use only bounded, typed fields:

- `host`, `port`, `user`, `password`, and `database` for the single MySQL
  schema;
- `uploads_path` for an allowlisted read-only source mount;
- `base_url` and `files_url` for non-destructive readiness/offline checks;
- a bounded quiescence-evidence mechanism chosen after the production
  coordinator decision.

Do not accept arbitrary commands, shell fragments, container names, host
paths, image names, or Docker endpoints.

### Connectivity

`test()` must be non-destructive and prove:

1. the pinned MySQL client can connect and select the configured schema;
2. the uploads path is a readable, non-symlinked directory under the expected
   mount root;
3. API Gateway and files health endpoints are reachable;
4. the source is filesystem mode, not an unimplemented S3 configuration.

### Backup

The backup must refuse unless it has fresh, unambiguous proof that the writer
is quiesced. While that proof remains valid it must:

1. stream the complete schema with the pinned MySQL 8.4 `mysqldump` contract;
2. traverse the uploads tree without following links and reject all special
   files or unsafe paths;
3. record each upload's relative path, size, and SHA-256;
4. validate the dump structure, expected active file paths, file counts,
   hashes, and source-version metadata;
5. create a single private artifact containing exactly `manifest.json`,
   `database.sql`, and `uploads.tar`;
6. publish it through `create_backup_artifact()` so the artifact and sidecar
   appear atomically;
7. fail without a final artifact if quiescence expires, the source changes, a
   file is partial/missing, or any validation fails.

The manifest must never contain passwords, keys, tokens, cookies, raw env, or
note plaintext.

### Restore

Declare `restore_capability = "partial"` unless the plugin itself can prove the
final exact-image application boot without gaining broad Docker privileges.
Restore must require a fresh isolated destination, empty MySQL schema, empty
uploads directory, and an explicit non-production sentinel. It must:

1. validate the full artifact before mutation, including member allowlist,
   traversal/link/special-file rejection, manifest schema, sizes, and hashes;
2. stage and verify uploads on the destination filesystem;
3. import into the empty schema, run `mysqlcheck`, and verify table/sentinel
   counts;
4. atomically publish the staged uploads only after both components validate;
5. return an honest partial result until the integration harness boots the
   exact server digest with external synthetic keys and proves a real login,
   sync read, upload equality, and both health endpoints.

A failed MySQL import makes the disposable destination invalid; destroy and
recreate it rather than retrying into partial state.

## Test-first slices

1. Discovery, schema, invalid configuration, and restore capability.
2. Pinned-client database test plus uploads-path and HTTP health checks.
3. Fresh/expired/missing/ambiguous quiescence-evidence refusal.
4. Streamed MySQL dump and safe uploads traversal into one private artifact.
5. Strict manifest, SQL, tar, path, type, count, size, and hash validation.
6. Atomic cleanup for dump, copy, validation, timeout, and cancellation
   failures.
7. Fresh isolated restore guards and empty-destination enforcement.
8. Successful database and uploads staging/publication.
9. Restore failure behavior that never claims a reusable partial destination.
10. Secret-redaction, concurrency, API discovery/schema/test coverage, and
    bounded-memory behavior.

## Local drill

Build a development backend image and two completely isolated Standard Notes
stacks using the exact server digest and declared MySQL version. Use only
synthetic secrets and data, no published LAN ports, no production network,
mount, credential, or endpoint.

The two consecutive drills must prove:

- the source server is quiesced before each plugin backup;
- two unique non-empty artifacts and sidecars with independent size and
  SHA-256 calculations;
- advancing database and upload sentinels between backup 1 and backup 2;
- restore of artifact 1 into a fresh destination contains only state A;
- restore of artifact 2 into another fresh destination contains states A and
  B;
- exact upload count, size, and hashes match each source point;
- the exact server image boots with fresh Redis/LocalStack and external test
  keys;
- authenticated API login/sync reads plus API/files health prove usability;
- all disposable containers, networks, volumes, and temporary files are
  removed afterward.

## Production gate

Before implementation, obtain one explicit decision: may Standard Notes be
unavailable for a short scheduled window during every backup? If approved,
design the least-privileged external coordinator and separately obtain approval
for its concrete infrastructure changes. Expected minimum access for the
Homelab Backup backend remains MySQL dump credentials, one narrow read-only
uploads mount, `/backups` write access, health endpoints, and non-forgeable
quiescence evidence.

After deployment, production validation is limited to connectivity and an
explicitly approved backup trigger. Production restore is always forbidden.

## Done criteria

- [x] Exact deployed version and authoritative/excluded state are documented
  from primary sources.
- [x] Online consistency was analyzed against exact-version source.
- [x] A two-backup/two-restore isolated drill is specified.
- [ ] Scheduled production downtime is explicitly approved.
- [ ] The external quiescence contract is designed and approved.
- [ ] All ten test-first slices pass.
- [ ] Both local drills satisfy every evidence item.
- [ ] Full backend/frontend checks and two-axis review have no unresolved
  P0/P1 findings.
- [ ] Compatibility/recovery documentation and SemVer evidence are updated.
- [ ] The milestone is committed independently.

## STOP conditions

Stop rather than weaken consistency if production downtime is declined; the
quiescence proof can be forged or become stale; implementing it would expose
unrestricted shell/filesystem/Docker authority; the source switches to S3;
the exact image/schema differs from research; a restore destination is not
provably isolated and fresh; backward compatibility would be required; or any
drill could contact production.
