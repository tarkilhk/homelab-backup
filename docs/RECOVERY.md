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
| Cal.com | Automatic | Restores a validated PostgreSQL custom archive transactionally. |
| Gitea | Automatic | Restores a validated native dump into an explicitly labeled isolated Gitea 1.27.1 container, verifies application state, and rolls back on failure. |
| Homelab Backup | Partial | Creates a validated database only in a fresh sentinel-marked offline directory. Booting and verifying the exact recorded backend image remains a separate operator step. |
| MySQL | Partial | Imports only into an empty database and validates tables; MySQL DDL is non-transactional, so a failed import requires the destination to be reset before retry. |
| PostgreSQL | Automatic | Restores a validated custom archive transactionally with cleanup and stop-on-error semantics. |
| WordPress | Automatic | Replaces site files, imports the database, validates both, and rolls back on failure. The destination must be an isolated mounted WordPress root. |
| Invoice Ninja | Partial | Queues the official company import. Invoice Ninja exposes no terminal import status, so application-level verification remains required. |
| Jellyfin | Automatic | Stages a validated archive in Jellyfin's shared backup directory and invokes the official restore endpoint. Success requires an observed restart and readiness transition. |
| Lidarr | Automatic | Uploads a validated archive, restarts Lidarr, and waits for a new ready process. |
| Pi-hole | Automatic | Imports a validated Teleporter archive and proves the service can export again. |
| Radarr | Automatic | Uploads a validated archive, restarts Radarr, and waits for a new ready process. |
| SFTPGo | Partial | Creates a validated SFTPGo 2.7.5 provider database only in a fresh sentinel-marked offline directory; application boot verification remains separate. |
| Sonarr | Automatic | Uploads a validated archive, restarts Sonarr, and waits for a new ready process. |
| Vaultwarden | Automatic | Stops the destination, restores a validated component manifest through an isolated helper, checks SQLite, proves Docker health or `/alive`, and rolls back before restart on failure. |

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

## Plugin-specific cautions

- Vaultwarden backup requires a version with the built-in `/vaultwarden backup`
  command (introduced in Vaultwarden 1.32.1). The generated SQLite snapshot is
  checked with `PRAGMA quick_check` before publication. Attachments and
  `config.json`, when present, are bundled with that snapshot.
- Vaultwarden restore requires `data_path` to exactly match a writable Docker
  mount. The backend needs Docker-socket access. A Docker healthcheck is preferred;
  otherwise configure an unauthenticated `health_url` or allow the backend to
  reach the container's auto-detected `/alive` endpoint. Restore commands are
  bounded and the rollback artifact remains available through readiness checks.
- MySQL restore is intentionally partial: use a new, empty, isolated database.
  A failed non-transactional import may leave objects behind and must not be
  retried until that destination is reset.
- PostgreSQL and Cal.com restores use validated custom archives and stop on the
  first error inside a transaction.
- WordPress restore rejects roots, symlinks, paths overlapping `/backups`, and
  artifacts stored below the destination. Files and database are rolled back when
  any restore or validation step fails.
- Pi-hole v6 backup uses SID authentication and validates Teleporter contents.
- Jellyfin's server-generated source archives are outside Homelab Backup retention;
  manage that shared directory with Jellyfin's own backup retention policy.

## Locally verified component versions

The following isolated drills were run with synthetic data and no published ports:

- MySQL 8.4.0 and PostgreSQL 16
- Pi-hole 2026.07.2
- Jellyfin 10.11.11
- Lidarr 3.1.0.4875, Radarr 6.3.0.10514, and Sonarr 4.0.19.2979
- Vaultwarden 1.37.1
- WordPress 7.0.2
- Invoice Ninja 5.13.31
- Gitea 1.27.1
- Homelab Backup backend 0.2.1
- SFTPGo 2.7.5 (`9888a3d`, pinned Alpine image digest)
- Bazarr 1.5.6 in LinuxServer image ls349

Each completed drill includes a non-destructive connection test, two distinct
validated backups with sidecars, and an isolated restore. Invoice Ninja remains
partial even though the local import marker arrived, because its API does not
provide a terminal status. Re-run drills after any component-version upgrade.

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
