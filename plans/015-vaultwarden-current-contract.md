# Plan 015: Revalidate Vaultwarden 1.37.1 recovery

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: CRITICAL
- **Depends on**: Plan 001 foundation and explicit scheduled-downtime approval
- **State**: BLOCKED
- **Production status**: BLOCKED; every production restore remains forbidden
- **Fixed point**: `d4ffa2f`

## Outcome

Repair the existing `vaultwarden` plugin to satisfy the current authoring
contract for the exact linux/amd64 Vaultwarden 1.37.1 image:

- OCI manifest
  `sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744`;
- source revision `2629bcbe1380c894e3a7f52cafcac3988edb8fbb`;
- default SQLite state under the exact writable `/data` mount; and
- automatic restore only to a fresh, explicitly labeled and allowlisted local
  destination.

The exact state, consistency proof, least privilege, artifact format, and drill
are documented in `plans/research/vaultwarden.md`.

## Blocking decision

The current plugin snapshots SQLite and then copies live attachments and file
Sends. Vaultwarden has no transaction spanning those resources, so the result
is not guaranteed coherent. The selected dependable workflow briefly stops the
source for every full backup, captures all components while static, restarts it
in cancellation-shielded cleanup, and proves readiness before publication.

Do not implement or activate that behavior until the user explicitly approves
scheduled Vaultwarden stop/start during backups. Ordinary permission to trigger
a production backup does not authorize service downtime.

## Exact contract

### Configuration and status

Keep the schema flat and clean-breaking. Require a bounded container name,
fixed `/data`, and exact boolean `allow_service_stop=true`. Remove the arbitrary
`health_url` path. Do not add aliases or alternate storage layouts.

`test()` and `get_status()` must non-destructively prove the exact container,
image digest/revision/version, running state, healthy image healthcheck,
database-backed readiness, default SQLite/storage paths, exact writable `/data`
mount, and the expected database file. Fail on split/external storage or any
effective path override.

### Consistent backup

Serialize by resolved Docker container identity. Under that lock:

1. repeat exact preflight and record the source/mount/image identities;
2. stop the source to a fixed deadline and prove it stopped;
3. use an exact-image, networkless helper sharing only its `/data` mount to run
   the native SQLite backup;
4. attribute exactly one new native database snapshot;
5. stream only that snapshot, attachments, file Sends, optional `config.json`,
   and required `rsa_key.pem` into private temporary storage;
6. build and strictly validate one bounded versioned `.tar.gz` plus private
   manifest and non-secret sidecar evidence;
7. delete only the attributed native snapshot and remove the helper; and
8. restart and prove exact application readiness before atomically publishing.

Every timeout, exception, and cancellation must stop/reap uncertain work,
remove secret residue, restart and verify the source, then propagate the
original outcome. A failed restart is a critical failed backup, never success.

### Artifact and restore

The artifact validator must enforce exact members and types, path/link safety,
resource bounds, no trailing gzip/tar data, SQLite quick/FK/migration/table
checks, valid configuration and RSA material, and a complete database-to-file
map with size/SHA-256 for every attachment and file Send. Remove the obsolete
`rsa_key.pub.pem` contract. Hold validated descriptors through publication and
restore use.

Restore must require all of:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- an exact comma-capable local container allowlist;
- container label
  `asia.hollinger.homelab-backup.restore-destination=true`;
- a distinct source and destination identity;
- the exact image/default layout/mount; and
- a running fresh destination with zero user/vault/attachment/Send state.

Capture and validate a complete rollback preimage, stop the destination, apply
through a networkless exact-image helper, re-fetch and validate all restored
state, then start and prove a new ready 1.37.1 process. On any failure or
cancellation after mutation, restore and verify the preimage before returning;
otherwise leave the disposable destination stopped with a critical failure.

## Test-first slices

1. Exact discovery/schema/config and non-destructive status behavior.
2. Immutable image, version, health, default-layout, mount, and override
   rejection.
3. Container-identity serialization and bounded stop/helper/restart lifecycle.
4. Exact native-snapshot attribution and complete component capture.
5. Strict private artifact/manifest/sidecar validation and file-map negatives.
6. Timeout, repeated cancellation, helper failure, source restart, and secret
   cleanup behavior.
7. Hard local restore authorization and fresh-destination refusal.
8. Descriptor-bound restore, complete rollback, readiness, and concurrency.
9. Real API discovery/status behavior and secret-safe errors/logs.
10. Two exact-image backup-to-two-fresh-restore rounds plus representative
    failure injection.

## Exact local drill

Use only the immutable image on an internal disposable Docker network with no
published ports. Create one source and two fresh labeled destinations with
separate named `/data` volumes. Use Vaultwarden's supported Web Vault flow or
the official Bitwarden client to create phase-distinct synthetic secure notes,
attachment bytes, and file-Send bytes without placing credentials in arguments
or logs.

For A and B, prove distinct private artifacts, sidecars, sizes, hashes,
manifests, and source restart transitions. Restore each through
`RestoreService` to an independent fresh destination. Decrypt/read the expected
note and verify attachment and file-Send plaintext SHA-256, prove A/B
separation, restart each destination, and repeat application evidence. Run the
whole A/B recovery sequence twice from clean state and audit all containers,
volumes, networks, helpers, images introduced by the drill, files, listeners,
and synthetic credentials for absence.

## Verification and completion

- focused plugin/API/RestoreService tests pass;
- two clean exact-image drill rounds pass;
- full backend and frontend gates pass;
- mypy, changed-file Black/isort, SemVer, diff, secret scan, and resource audit
  pass;
- two independent reviewers find no unresolved P0/P1 Standards or Spec issue;
- compatibility, recovery, changelog, and ledger documentation are current;
  and
- the milestone is committed independently and never pushed or deployed as
  part of local work.

Mark `DONE (local)` only after every gate passes. Production remains blocked
until the exact image is pinned, the target opts into stop-based backups, the
schedule/downtime is approved, and a backup-only production validation succeeds.

## STOP conditions

Stop rather than adding compatibility or weakening evidence if downtime is not
approved; the image/version/default SQLite layout differs; authoritative state
exists outside the selected components; the source cannot be reliably
restarted; a helper cannot be stopped and reaped; exact file mapping cannot be
proved; restore is not fresh/local/labeled/allowlisted; a broad host path or
production restore is requested; or any fallback image, path, storage backend,
health origin, archive format, or credential transport would be required.
