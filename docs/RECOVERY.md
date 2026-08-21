# Recovery guide

Homelab Backup is only trustworthy after both backup and restore paths have been
tested for the services you depend on. Treat this guide as the minimum recovery
contract, not as a substitute for periodic restore drills.

See [PLUGIN_COMPATIBILITY.md](PLUGIN_COMPATIBILITY.md) for the exact homelab image
mapping, isolated drill evidence, and current deployment prerequisites.

## Artifact contract

Finalized artifacts use this layout:

```text
/backups/<target-slug>/<YYYY-MM-DD>/<unique-artifact-name>
/backups/<target-slug>/<YYYY-MM-DD>/<unique-artifact-name>.meta.json
```

Plugins write to a temporary file, validate that it is a non-empty regular file,
atomically publish it, and then atomically publish the sidecar. A scheduled run is
not recorded as successful unless the sidecar path, plugin, and target match and a
SHA-256 digest can be calculated.

Do not rename artifacts independently of their sidecars. Do not edit sidecars to
force an incompatible restore. A disaster-recovery scan ignores symlinks, empty
files, partial files, and artifacts with missing or inconsistent sidecars.

## Restore capability matrix

| Plugin | Capability | Meaning |
| --- | --- | --- |
| Bazarr | Partial | Creates validated Bazarr 1.5.6 SQLite and YAML state only in a fresh sentinel-marked local directory. Media, subtitle files, Sonarr, Radarr, and exact-image boot verification remain separate recovery prerequisites. |
| Cal.com | Partial | Restores one exact Cal.com 6.2.0 PostgreSQL 16 archive transactionally into an explicitly authorized fresh sentinel database and verifies migration, catalog, and control-plane marker equality. The original encryption/deployment configuration, external providers, and application lifecycle remain operator prerequisites. |
| Gitea | Automatic | Restores a validated native dump into an explicitly labeled isolated Gitea 1.27.1 container, verifies application state, and rolls back on failure. |
| Homelab Backup | Partial | Creates a validated database only in a fresh sentinel-marked offline directory. Booting and verifying the exact recorded backend image remains a separate operator step. |
| MySQL (legacy) | Partial | The existing adapter imports only into an empty database and validates tables. Plan 019 current-contract revalidation is blocked because exact MySQL Shell 8.4.0 could not prove consistent online capture without broader privileges; do not treat the v0.2.1 baseline as current recovery evidence. |
| PostgreSQL | Automatic | Restores one strictly validated PostgreSQL 16 named-database archive into an explicitly authorized fresh `template0` database with the exact sentinel, in one transaction. Cluster roles, tablespaces, server configuration, and application lifecycle remain external prerequisites. |
| Profilarr | Automatic | Creates and independently validates a complete Profilarr 1.1.5 SQLite control plane and reconstructed all-ref Git repository in a fresh sentinel-marked local directory. Radarr, Sonarr, Git hosting, credentials, and exact-image boot remain separate recovery-stack prerequisites. |
| Prowlarr | Automatic | Uploads an exact validated Prowlarr 2.4.0.5397 control-plane archive, restarts the isolated destination, and proves a different ready process. External indexers and download clients remain recovery prerequisites. |
| WordPress | Automatic | Replaces site files, imports the database, validates both, and rolls back on failure. The destination must be an isolated mounted WordPress root. |
| Invoice Ninja | Partial | Imports only into an explicitly authorized fresh local destination, verifies company/client/invoice markers, and records a partial outcome. Version 5.13.31 exposes no terminal import status and cannot reliably recover embedded document bytes into a fresh private destination. |
| Jellyfin (legacy) | Automatic | The existing adapter stages a minimally validated native archive and treats an observed restart/readiness transition as success. Plan 021 current-contract revalidation is blocked: the archive is not coherent across all reads, omits authoritative `/config` state, requires unrestricted Administrator authority, and exposes no terminal restore/rollback proof. Do not treat the v0.2.1 baseline as complete recovery evidence. |
| Lidarr | Automatic | Uploads an exact validated 3.1.0.4875-ls38 control-plane archive only to an authorized fresh local destination, then proves restored content through two distinct restart cycles. Music and download data remain external. |
| Pi-hole (legacy) | Automatic | The existing adapter imports a minimally checked Teleporter ZIP and proves only that an export remains possible. Plan 020 current-contract revalidation is blocked pending explicit export-consistency and source-authority policies; do not treat the v0.2.1 baseline as complete recovery evidence. |
| Radarr | Automatic | Uploads an exact validated 6.3.0.10514-ls313 control-plane archive only to an authorized fresh local destination, then proves restored content through two distinct restart cycles. Movies and download data remain external. |
| Readarr | Automatic | Uploads an exact validated Readarr 0.4.18.2805 control-plane archive, restarts the isolated destination, and proves a different ready process. Books and download working data remain external. |
| SFTPGo | Partial | Creates a validated SFTPGo 2.7.5 provider database only in a fresh sentinel-marked offline directory; application boot verification remains separate. |
| Sonarr | Automatic | Uploads an exact validated 4.0.19.2979-ls320 control-plane archive only to an authorized fresh local destination, then proves restored content through two distinct restart cycles. Episodes and download data remain external. |
| Vaultwarden | Automatic | Restores the exact validated 1.37.1 default-`/data` component artifact only to a fresh labeled and allowlisted local container, proves SQLite/files and exact Docker health, and rolls back before restart on failure. |

The backend rejects automated restore requests for manual-only plugins. Copying an
artifact into another directory is not reported as a successful restore.

## Recovery drill

1. Choose a recent artifact from the Restore page and verify its target, plugin,
   timestamp, size, and sidecar source.
2. Restore into an isolated destination target. Never use the production target
   for the first drill.
3. Confirm the restore run completed and inspect its target-run message.
4. Validate the service at the application layer: authenticate, read representative
   records, and verify attachments or media where applicable.
5. Record the tested artifact, plugin version, elapsed time, and any manual steps.
6. Repeat after plugin, database-engine, or application upgrades.

For a database-backed artifact that still has run history, Homelab Backup rejects
the restore if the path, plugin, target slug, byte count, or SHA-256 digest differs
from the recorded backup. A sidecar-only disaster recovery restore can establish
origin and plugin compatibility, but it cannot compare against a lost database
record; verify those artifacts independently before using them.

## Retention

The UI previews global cleanup using the saved policy and displays the keep/delete
counts before confirmation. Unsaved policy edits disable the cleanup button. The
backend separately requires `confirmed=true`, so a direct request cannot bypass
the explicit gate accidentally.

If artifact deletion fails, the target-run and parent-run records remain in the
database and the maintenance result reports failed paths. Investigate those paths
before retrying cleanup.

## SQLite application database

The application database lives at `/app/db/homelab_backup.db` and must be stored on
a persistent volume. Startup migrations preserve existing data and include a real
SQLite table rebuild for historical installations where `runs.job_id` was still
`NOT NULL`. Back up the database volume before upgrading the application.

Database files, copied databases, and generated artifacts are ignored by Git and
must not be committed. The current-tree secret scan does not erase earlier Git
objects. If an old public revision contained credentials, rotate those credentials
and decide separately whether repository history should be rewritten.

### Homelab Backup self-recovery

The `homelab_backup` plugin snapshots the running SQLite database through
SQLite's online backup API. It does not copy a live database file or its WAL.
The resulting ZIP contains exactly `manifest.json` and
`homelab_backup.db`; the manifest binds the artifact to the exact application
version, SQLite payload size and SHA-256, normalized schema, required tables,
and non-secret row counts. The ZIP is deliberately mode `0600` because target
configuration rows can contain credentials in cleartext.

The artifact is database-only. It includes targets/configuration, groups/tags,
jobs/schedules and retention policies, settings, maintenance state, run history,
and the artifact catalog. It excludes `/backups` artifact bytes, environment and
Compose files, images, source, and frontend state. Recover or replicate each
instance's artifact tree—including sidecars—separately. A restored catalog may
truthfully contain paths whose artifact files are not mounted yet.

Restore is intentionally create-only and offline:

1. Use the exact application version recorded in the manifest.
2. Create a fresh isolated directory outside `/app/db` and `/backups`.
3. Create the regular sentinel file
   `.homelab-backup-restore-destination` containing exactly
   `homelab-backup-isolated-restore-v1` followed by one newline. Leave the
   directory otherwise empty.
4. Configure a separate restore target whose `database_path` is that
   directory's `homelab_backup.db`. The plugin rejects existing database,
   WAL/SHM, symlink, overlapping-artifact, and live-path destinations.
5. Restore the artifact. A `partial` result means the database was created,
   hashed, schema/integrity/foreign-key/count checked, and set to mode `0600`.
6. Mount a copy of that directory at `/app/db` in an isolated backend using the
   exact recorded image. Do not attach production networks, mounts, or the
   Docker socket. Confirm `/ready` and representative API-visible records before
   planning a real recovery cutover.

Never place the restore sentinel in a production database directory and never
restore over `/app/db/homelab_backup.db`. Cross-version and in-place restores
are outside this plugin contract.

### SFTPGo control-plane recovery

The `sftpgo` plugin reads `/var/lib/sftpgo/sftpgo.db` through a dedicated
read-only mount and uses SQLite's online backup API while SFTPGo remains live.
The private `.db` artifact contains the exact 2.7.5/schema-33 provider state:
administrators, users and public keys, groups, virtual folders, shares, API
keys, roles, IP lists, events, quotas, and provider configuration. The copied
database is validated for its complete schema, integrity, foreign keys, and an
administrator before publication. Active transfers, shared sessions, task
locks, and Defender history are removed from the copy only.

This is deliberately control-plane-only. It does not include `/srv/sftpgo`,
any `/nas/*` client/media payload, generated SSH host keys, environment files,
or Compose declarations. SFTP is disabled in the currently verified deployment;
if it is enabled later, host-key continuity and writable user payload require a
new backup contract.

Restore is create-only and offline:

1. Use SFTPGo 2.7.5, schema version 33.
2. Create a fresh directory outside `/sources/sftpgo`, `/var/lib/sftpgo`, and
   `/backups`.
3. Add `.sftpgo-restore-destination` containing exactly
   `sftpgo-v2.7.5-isolated-restore-v1` followed by one newline. Leave the
   directory otherwise empty.
4. Configure the destination target's `database_path` as that directory's
   `sftpgo.db`. Existing DB/WAL/SHM files, symlinks, and overlapping paths are
   rejected.
5. Restore the artifact. `partial` means the mode-`0600` database was created
   atomically and revalidated; it does not claim a service boot.
6. Mount a copy at `/var/lib/sftpgo/sftpgo.db` in an isolated exact-image
   container. Authenticate, confirm the SQLite provider, and inspect
   representative users, keys, groups, folders, shares, API keys, roles, and
   rules before any recovery cutover.

Never restore SFTPGo in production through Homelab Backup.

### Bazarr control-plane recovery

The `bazarr` plugin asks exact Bazarr 1.5.6/LinuxServer ls349 to create its
native online backup, then copies the uniquely attributed stable ZIP from a
dedicated read-only mount. It requires exactly `bazarr.db` and `config.yaml`,
SQLite mode, the pinned Alembic migration and table set, clean integrity and
foreign-key checks, and bounded resource use. The artifact contains credentials
and requires protected storage; the sidecar contains only approved non-secret
structural evidence.

This is deliberately control-plane-only. Movies, television episodes,
subtitle files, Sonarr, Radarr, proxy state, and NAS data are excluded and must
be restored independently. A `partial` result never proves those prerequisites.

Restore is create-only and local:

1. Use Bazarr 1.5.6 in LinuxServer image ls349.
2. Create a private disposable parent under `/tmp` or `/restore` containing
   only `.bazarr-restore-destination` with
   `bazarr-v1.5.6-isolated-restore-v1` followed by one newline.
3. Set `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` only inside the isolated
   restore runner and configure an absent child `restore_directory`.
4. Restore through `RestoreService`. It stages and hashes the artifact, then
   creates only `config/config.yaml` and `db/bazarr.db` with private modes.
5. Boot the exact pinned image without production reachability, verify
   representative state through Bazarr, restart it, and independently classify
   the separately restored media/subtitle payload as matching, missing, or
   mismatched.

Never expose the restore authorization variable on a production backend and
never invoke Bazarr's native `PATCH` restore through this plugin.

### Profilarr composite recovery

The `profilarr` plugin protects exact Profilarr 1.1.5 state from two narrow
read-only sources. It uses SQLite's online backup API for
`/config/profilarr.db` and creates a self-contained `git bundle --all` from a
stable clean `/config/db` repository. The private `.profilarr` ZIP contains
exactly `profilarr.db`, `repository.bundle`, and `manifest.json`. The database
can contain authentication material, Arr API keys, internal URLs, and session
secrets; the bundle can contain private history and local-only refs. Protect the
artifact as credential-bearing even though its external sidecar contains only
non-secret structural evidence.

A backup fails deliberately when the repository is dirty, detached, unborn,
changing, shallow, partial, corrupt, in an active Git operation, or dependent
on submodules, LFS, alternates, replace refs, or missing objects. Commit or
resolve the repository state and let the next scheduled run retry. Do not copy
the worktree or enable a dirty-state compatibility path.

Restore is create-only and local:

1. Use Profilarr 1.1.5 from the pinned linux/amd64 image manifest recorded in
   the artifact and compatibility matrix.
2. Create a private disposable parent beneath `/tmp` or `/restore` containing
   only `.profilarr-restore-destination` with
   `profilarr-v1.1.5-isolated-restore-v1` followed by one newline.
3. Set `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` only inside a loopback-only
   restore runner and configure an absent child `restore_directory`.
4. Restore through `RestoreService`. It stages and hashes the artifact, creates
   `profilarr.db`, reconstructs `db` from the bundle without copying source
   `.git/config`, hooks, or credentials, and revalidates database bytes, every
   ref, branch, HEAD, repository integrity, and authoritative file inventory.
5. Boot a copy with the exact pinned Profilarr image on an isolated local
   network. Provide only disposable mock Radarr, Sonarr, and Git dependencies;
   verify representative application state and restart readiness before
   planning any recovery cutover.

The automatic capability covers all authoritative Profilarr application state.
It does not restore Radarr or Sonarr, provision Git credentials, push local-only
commits, or recreate infrastructure. Never expose the restore authorization
variable on a production backend and never restore Profilarr in production
through Homelab Backup.

### Readarr and Prowlarr control-plane recovery

The `readarr` and `prowlarr` plugins ask the exact application API to create a
native manual backup, then copy the uniquely attributed ZIP from a dedicated
read-only backup-directory mount. They never download artifacts through the UI
route. Each archive must contain exactly lowercase `config.xml`, `INFO`, and the
exact SQLite database, pass bounded ZIP/XML/SQLite validation, and match the
pinned application version and migration before publication.

Restore is permitted only in a disposable local drill. Set
`HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` and list only the exact disposable
destination origins in `HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS`, then
invoke `RestoreService` with a fresh destination target. Before upload, the
plugin requires the exact version/migration and empty tag, integration,
notification, download-client, and root/application resource lists. It stages
and hashes the artifact, holds that verified descriptor through revalidation
and upload, requests restart, switches to the restored API key only in memory,
and requires a different non-empty process start time plus the exact version
and migration. A failed disposable restore destination must be discarded rather
than reused.

The artifacts restore application control-plane state only. Readarr books and
download data, Prowlarr's external services, and every media payload remain
separate prerequisites. Never enable the restore environment gates on a normal
backend and never restore either application in production through Homelab
Backup.

## Plugin-specific cautions

- Vaultwarden backup is pinned to exact 1.37.1 and requires
  `container_name` plus `allow_service_stop=true`. It briefly stops the source,
  runs the native SQLite snapshot from an exact-image networkless helper,
  captures the default `/data` database, attachments, Sends, RSA key, and
  optional configuration while static, then restarts and proves exact health
  before publication.
- Vaultwarden restore requires the explicit isolated-restore flag, exact local
  container allowlist, restore-destination label, distinct source identity, and
  a fresh exact-image destination with one writable `/data` mount. Arbitrary
  paths, alternate storage layouts, and `health_url` fallbacks are rejected.
  File Sends are currently unused and were zero in the exact drill; their
  client-level recovery must be revalidated before relying on that feature.
- Production release `v0.4.0` runs the approved stop-based Vaultwarden backup
  daily at 04:00 Asia/Singapore. Target/job `1` and backup Run/TargetRun
  `264`/`263` are the rollout evidence; the artifact was discovered from its
  sidecar after Vaultwarden returned healthy. This does not authorize a
  production restore.
- MySQL restore is intentionally partial: use a new, empty, isolated database.
  A failed non-transactional import may leave objects behind and must not be
  retried until that destination is reset. The legacy adapter has not passed
  the current two-round contract. Exact MySQL Shell 8.4.0 revalidation is
  blocked pending explicit approval of broader source privileges or a proven
  quiescence boundary; see `plans/019-mysql-8-4-shell.md`.
- PostgreSQL restore accepts only a private RestoreService-staged artifact whose
  size, SHA-256, source identity, catalog, TOC, and sidecar provenance all match.
  The destination must be a separately authorized PostgreSQL 16 database created
  from `template0`, contain the exact restore sentinel, have no user objects or
  other connections, and use an owner without cluster-wide authority. Restore
  stops on the first error inside one transaction and never creates or drops a
  database.
- Cal.com restore accepts only a RestoreService-staged artifact with exact
  v6.2.0 migration, schema, catalog, marker, size/SHA-256, and sidecar provenance.
  The destination must be a separately authorized PostgreSQL 16 database created
  from `template0`, carry the Cal.com-specific sentinel, and contain no user
  objects or competing connections. The plugin restores the database in one
  transaction and reports `partial`; boot the exact app separately with the
  original encryption/deployment configuration and verify external integrations.
- WordPress restore rejects roots, symlinks, paths overlapping `/backups`, and
  artifacts stored below the destination. Files and database are rolled back when
  any restore or validation step fails.
- Pi-hole v6 backup uses SID authentication, but the legacy adapter validates
  only ZIP readability plus one member and has not passed the current two-round
  semantic/DNS recovery contract. Exact 2026.07.2 export is non-atomic across
  its restorable stores, and its application password is not endpoint-scoped;
  see `plans/020-pihole-teleporter-current-contract.md` for the two decision
  gates.
- Jellyfin's legacy server-generated archives are outside Homelab Backup
  retention, but archive retention is not the current blocker. Exact 10.11.11
  research found a non-atomic database/file boundary, omitted plugins and device
  identity, unrestricted API-key authority, and a destructive restore without
  terminal proof. See `plans/021-jellyfin-current-contract.md` before changing
  its native-backup directory or credential.

## Locally verified component versions

The following isolated drills were run with synthetic data and no published ports:

- MySQL 8.4.0
- PostgreSQL 16.14 using pinned linux/amd64 manifest
  `sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00`
- Cal.com 6.2.0 using pinned linux/amd64 manifest
  `sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001`
  against the PostgreSQL 16.14 manifest above
- Pi-hole 2026.07.2
- Jellyfin 10.11.11
- Lidarr 3.1.0.4875-ls38, Radarr 6.3.0.10514-ls313, and Sonarr
  4.0.19.2979-ls320 using the pinned linux/amd64 manifests recorded in
  `PLUGIN_COMPATIBILITY.md`; two clean rounds produced twelve strict backups
  and twelve fresh RestoreService destinations with two-restart persistence.
- Readarr 0.4.18.2805 and Prowlarr 2.4.0.5397 (pinned linux/amd64 manifests)
- Vaultwarden 1.37.1 using linux/amd64 manifest
  `sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744`
- WordPress 7.0.2
- Invoice Ninja 5.13.31
- Gitea 1.27.1
- Homelab Backup backend 0.2.1
- SFTPGo 2.7.5 (`9888a3d`, pinned Alpine image digest)
- Bazarr 1.5.6 in LinuxServer image ls349

Each completed legacy drill includes a non-destructive connection test, two
distinct validated backups with sidecars, and an isolated restore. The current
Invoice Ninja milestone goes further: two clean rounds produced four distinct
exports and four independent fresh RestoreService destinations, with exact
company/client/invoice marker checks after every import. It remains partial
because its API does not provide terminal job status and exact 5.13.31 does not
reliably recover embedded document bytes into a fresh private destination.

The current PostgreSQL milestone also uses the stricter two-round contract. Each
clean round produced immutable phase-A and phase-B archives through a real
Target/Job/Run/TargetRun, restored each archive through RestoreService into a
separate fresh database, and repeated exact relational, FK, index, sequence,
extension, large-object, and readiness checks after PostgreSQL restart. This is
local capability evidence only: production remains gated on the actual runtime
digest, database inventory, dedicated denied-write role/default grants, network,
targets/jobs, and a separately approved backup-only run. Production restore is
forbidden.

The Cal.com milestone reuses that PostgreSQL foundation and adds exact v6.2.0
application evidence. Each of two clean rounds produced immutable A/B archives,
restored both into independently fresh databases, booted the pinned Cal.com
image against each, proved phase-specific public event/booking and typed
control-plane markers, then repeated the proof after database and app restart.
Production remains gated on the actual Cal.com/PostgreSQL runtime digests, a
dedicated denied-write role, the DMZ database-only network path, targets/jobs,
and a separately approved backup-only run. Production restore is forbidden.
Re-run drills after any component-version upgrade.

Use `backend/scripts/plugin_drill.py` to repeat that contract. Provide source and
destination plugin configurations as JSON files mounted from outside the
repository; never commit them. The destination must be an isolated disposable
service. The script independently recomputes artifact sizes and SHA-256 digests,
validates both sidecars, and requires the declared restore outcome. For example:

```bash
cd backend
.venv/bin/python scripts/plugin_drill.py postgresql \
  --component-version 16 \
  --target-slug postgresql-drill \
  --source-config /run/secrets/postgresql-source.json \
  --destination-config /run/secrets/postgresql-destination.json
```
