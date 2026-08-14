# Recovery guide

Homelab Backup is only trustworthy after both backup and restore paths have been
tested for the services you depend on. Treat this guide as the minimum recovery
contract, not as a substitute for periodic restore drills.

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
| Cal.com | Automatic | Executes a PostgreSQL restore. |
| MySQL | Automatic | Executes the SQL restore against the configured database. |
| PostgreSQL | Automatic | Executes `psql` with `ON_ERROR_STOP` enabled. |
| WordPress | Partial | Restores the database; site files remain a manual step. |
| Invoice Ninja | Manual | Use the application import workflow. |
| Jellyfin | Manual | Restore the exported data using Jellyfin's documented process. |
| Lidarr | Manual | Upload/import the backup through Lidarr. |
| Pi-hole | Manual | Import the Teleporter archive in Pi-hole. |
| Radarr | Manual | Upload/import the backup through Radarr. |
| Sonarr | Manual | Upload/import the backup through Sonarr. |
| Vaultwarden | Manual | Stop the container, restore data, remove stale SQLite WAL files, then start and verify it. |

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

## Plugin-specific cautions

- Vaultwarden backup requires a version with the built-in `/vaultwarden backup`
  command (introduced in Vaultwarden 1.32.1). The generated SQLite snapshot is
  checked with `PRAGMA quick_check` before publication. Attachments and
  `config.json`, when present, are bundled with that snapshot.
- Vaultwarden automated restore is disabled because overwriting a live SQLite
  database or leaving a stale `db.sqlite3-wal` can corrupt the restored state.
- PostgreSQL restores stop on the first SQL error; a zero exit status is required
  before success is recorded.
- Pi-hole v6 backup uses session-ID and CSRF headers and terminates the API session
  after downloading the Teleporter archive. Pi-hole restoration remains manual.
