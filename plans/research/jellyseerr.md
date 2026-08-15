# Jellyseerr 2.7.3 backup and restore research

Research date: 2026-08-15  
Scope: the Jellyseerr deployment declared in `homelab-infra`, the exact 2.7.3
vendor source, and SQLite's primary documentation. No production endpoint,
host, API, database, or filesystem was contacted. No production write, stop,
start, or restore was performed.

## Decision summary

Jellyseerr is a **two-store, quiescence-required SQLite workload**. Its complete
recoverable state is:

1. a standalone, validated backup of `/app/config/db/db.sqlite3`; and
2. the exact `/app/config/settings.json` from the same quiescent boundary.

The SQLite database is authoritative for users, authentication and sessions,
requests and their statuses, media availability records, issues and comments,
blacklists, watchlists, override rules, user preferences, and migration state.
`settings.json` is authoritative for the application API key, session-signing
identifier, Web Push keys, Jellyfin/Plex/Radarr/Sonarr integrations, notification
credentials, schedules, public settings, and network configuration. Both files
are credential-bearing and the artifact must be treated as secret material.

Classification: **CONDITIONAL / STOP before production-capable implementation**.
The exact vendor documentation says to stop Jellyseerr and back up its config
directory unless the backup system provides an atomic filesystem snapshot. An
online SQLite backup is internally consistent, but it cannot make the separate
`settings.json` copy part of the same transaction. Exact setup code demonstrates
both settings-before-database and database-before-settings write sequences.

The core artifact, validation, and create-only restore can be built and proven
entirely on the dev VM. A production-capable plugin cannot be completed safely
until the user explicitly accepts a brief Jellyseerr outage per backup and
chooses a narrowly scoped Jellyseerr-only lifecycle mechanism. Do not mount the
Docker socket into Homelab Backup. An externally guaranteed atomic filesystem
snapshot would be a valid alternative boundary, but none is declared today.

## Exact deployed service and source identity

The declaration was inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75`:

| Property | Exact declaration | Backup consequence |
| --- | --- | --- |
| Image | `fallenbagel/jellyseerr:2.7.3` | Contract is Jellyseerr 2.7.3, not current Seerr |
| Container | `jellyseerr` | One writer process must be quiesced |
| Config bind | `/docker-apps/jellyseer/config:/app/config` | Only authoritative local source; preserve the singular host spelling `jellyseer` |
| Environment | common environment plus `jellyseerr.env` | Only `NODE_OPTIONS=--max-old-space-size=512` is Jellyseerr-specific; no database override |
| Port | host `5055` to container `5055` | Can be used for a bounded readiness probe after restart |
| Network | media Compose default network | Jellyfin and media managers are integrations, not backup content |
| Media mounts | none | No media bytes are part of this target |
| Memory | 768 MiB | Operational only |

Evidence: `docker.compose/media/jelly_misc/jelly_misc.yaml:32-48` and
`docker.compose/media/jelly_misc/jellyseerr.env:1-3`. The shared common
environment only declares timezone-related keys. Neither `DB_TYPE` nor
`CONFIG_DIRECTORY` nor `API_KEY` is declared, so the exact release uses its
default SQLite database under `config/db/db.sqlite3` and persists its generated
API key in `settings.json`.

The deployed tag is mutable. It resolved during this research to the
multi-platform repository digest
`sha256:4538137bc5af902dece165f2bf73776d9cf4eafb6dd714670724af8f3eb77764`;
the locally inspected Linux/amd64 image ID was
`sha256:2742757d9c41bcb4acb76c86c4ce23a8c54d5dbe93a698c815a9a34bed0b18d0`.
The exact upstream `v2.7.3` tag is commit
[`e842036fafc9818ea38d2e19d4adb66e17e2aebf`](https://github.com/seerr-team/seerr/commit/e842036fafc9818ea38d2e19d4adb66e17e2aebf).
The image contains Jellyseerr 2.7.3, Node 22.18.0, `sqlite3` 5.1.7, and TypeORM
0.3.12. It has no declared `USER`; local inspection confirmed it runs as root.
That is exact 2.7.3 behavior and must not be confused with the newer Seerr 3
image's non-root runtime.

For a repeatable local drill, pin the above repository digest and assert the
resolved Linux/amd64 image ID. At production rollout, first pin an immutable
digest in infrastructure; do not infer which historical bytes a production
host pulled merely from the mutable tag.

The current Homelab Backup backend has no Jellyseerr config bind, no media
network, and no lifecycle control. It mounts only `/backups`, its own database,
and Jellyfin's native-backup directory
(`docker.compose/system/homelab-backup/homelab-backup.yaml:1-26`). This is a
deployment gate, not a reason to weaken the consistency contract.

## Authoritative state boundary

### Include the complete SQLite database

With no `DB_TYPE` override, Jellyseerr selects SQLite, stores it at
`config/db/db.sqlite3`, and enables write-ahead logging. These are exact source
defaults in
[`server/datasource.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/datasource.ts)
and are also described in the versioned
[`database-config.mdx`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/docs/extending-jellyseerr/database-config.mdx).

Back up the whole database, not a table allowlist. The exact entity inventory is
`Blacklist`, `DiscoverSlider`, `Issue`, `IssueComment`, `Media`,
`MediaRequest`, `OverrideRule`, `Season`, `SeasonRequest`, `Session`, `User`,
`UserPushSubscription`, `UserSettings`, and `Watchlist`, plus TypeORM's
`migrations` table. A whole-database copy automatically includes future tables
within this fixed application version and avoids silently dropping relations.
The exact entities are under
[`server/entity`](https://github.com/seerr-team/seerr/tree/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/entity).

After the application is stopped, open the source database through SQLite in
read-only mode and use the SQLite Backup API (or CLI `.backup`) to create one
standalone private `database.sqlite3`. Do not use a raw `cp` of a live main
database file. SQLite documents that its Backup API creates a consistent
destination snapshot, including for an online database
([SQLite Backup API](https://www.sqlite.org/backup.html)); Jellyseerr's own
versioned backup documentation gives the `.backup` command
([Jellyseerr 2.7.3 backups](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/docs/using-jellyseerr/backups.md)).
Stopping first is still required here to coordinate the database with settings.

Validate the standalone output before publication:

- it is a non-empty regular SQLite database;
- `PRAGMA integrity_check` returns exactly `ok`;
- `PRAGMA foreign_key_check` returns no rows;
- all exact 2.7.3 entity tables and the `migrations` table exist;
- the applied migration set ends at SQLite migration
  `UpdateWebPush1745492372230` and has no unknown later migration; and
- the application-level counts and bounded non-secret metadata used in the
  manifest can be queried without errors.

SQLite explicitly notes that `integrity_check` does not detect foreign-key
violations, which is why both checks are mandatory
([SQLite PRAGMA documentation](https://www.sqlite.org/pragma.html#pragma_integrity_check)).

### Include `settings.json` exactly

Vendor documentation says every web-UI configuration, including Jellyfin,
Plex, Radarr, Sonarr, and notifications, lives in `settings.json`; all other
user/request data lives in the database. Although the vendor restore page calls
settings optional for a reconfigurable instance, Homelab Backup's contract is
stricter: a **complete functional restore requires it**. Recreating integrations
manually is not a verified restore of the source state.

The exact settings type and defaults show that this file includes:

- `main.apiKey`, policies, quotas, locale, application identity, and login mode;
- `clientId`, used by Express as the session secret, plus VAPID private/public
  keys;
- Jellyfin and Plex server identity, libraries, and API credentials;
- Tautulli, Radarr, and Sonarr endpoints, API keys, profiles, root paths, tags,
  and synchronization policy;
- SMTP authentication, PGP, webhook URLs/auth, and notification service tokens;
- background-job schedules; and
- proxy endpoints and credentials, CSRF, and other network settings.

Evidence:
[`server/lib/settings/index.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/lib/settings/index.ts)
and the session setup in
[`server/index.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/index.ts).
The settings writer first writes `settings.json.tmp` and then renames it, so an
individual published settings file is atomic. It is not transactional with
SQLite.

Require a non-empty regular `settings.json`, parse it as a JSON object, and
validate the known top-level 2.7.3 structure and critical field types. Capture
the exact bytes and SHA-256 after quiescence. Never serialize a parsed and
reformatted replacement: byte-for-byte preservation avoids changing values or
dropping vendor-managed fields. Never expose field values in the internal
manifest, sidecar, metrics, errors, or logs.

### Exclude generated and external state

| State | Disposition and reason |
| --- | --- |
| Jellyfin/Plex media | Exclude. No media path is mounted; content belongs to those services/storage. |
| Radarr/Sonarr application state and downloads | Exclude. Jellyseerr stores only integration configuration and request linkage. Their own plugins own their state. |
| `config/cache/images` | Exclude. Generated proxy image cache; the exact image proxy can repopulate it. |
| `config/anime-list.xml` | Exclude. Downloaded Anime-Lists mapping refreshed by the application. |
| `config/logs` and `.machinelogs*.json` | Exclude. Operational logs, not restore state. |
| `settings.old.json` | Exclude. Settings migrator's previous-file rollback copy, not current authoritative configuration. |
| `settings.json.tmp` | Exclude. Transient unpublished write. Its presence after quiescence is a STOP condition. |
| `db.sqlite3-wal` and `db.sqlite3-shm` | Do not archive. They are SQLite runtime adjuncts. The Backup API output is the standalone database contract. |
| `DOCKER` marker | Exclude. Static installation/runtime marker, not user state. |

Primary implementation evidence for generated state:
[`imageproxy.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/lib/imageproxy.ts),
[`animelist.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/api/animelist.ts),
[`logger.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/logger.ts), and
[`settings/migrator.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/lib/settings/migrator.ts).

## Why the reliable boundary is offline

Jellyseerr's versioned documentation draws the boundary directly: an atomic
filesystem snapshot may capture the data folder online; otherwise stop the
application and back up `config`. Its advanced SQLite `.backup` alternative
solves SQLite consistency but not a simultaneous settings snapshot.

The source proves this is a real cross-store race, not merely theoretical:

- first Plex administrator setup saves `settings.json` and then saves the user
  row; and
- first Jellyfin/Emby administrator setup saves the user row and later saves
  server identity and the created API key to `settings.json`.

Both sequences are visible in
[`server/routes/auth.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/routes/auth.ts).
An independent online database backup and settings read can therefore represent
a state that never completed as one logical setup. Similar settings changes can
race request/session writes and integrations. Atomic rename prevents torn JSON,
not a mixed point in time.

The production plugin must therefore:

1. acquire Homelab Backup's serialized lease for the target;
2. ask a narrow coordinator for the exact container identity and current state;
3. gracefully stop only Jellyseerr and verify it is stopped within a timeout;
4. take and validate the database and settings snapshot from a read-only source
   mount;
5. publish the final artifact and sidecar atomically;
6. restart Jellyseerr in a `finally` path if this run stopped it; and
7. require bounded readiness at `/api/v1/settings/public` without logging its
   body before declaring the run successful.

Do not use `/api/v1/status` for the isolated readiness proof: exact 2.7.3 code
calls GitHub's release API from that route. `/api/v1/settings/public` loads
database/session and public settings locally and is sufficient when paired with
authenticated semantic probes in the restore drill.

A stop, backup, and successful restart is one backup attempt. If backup succeeds
but restart/readiness fails, record a failed or partial attempt with an
actionable recovery message; never report success while leaving Jellyseerr
down. If the container was already stopped, leave it stopped and clearly record
that original state.

## Least-privilege production shape

The plugin needs only:

- read-only access to the exact Jellyseerr config root;
- write access to its normal `/backups` destination;
- narrow status/stop/start authority for the single allowlisted Jellyseerr
  workload; and
- loopback or bounded service access solely for the post-start public readiness
  request.

It does **not** need:

- the Docker socket, even mounted read-only (it is host-equivalent authority);
- shell/exec in arbitrary containers or general Portainer/host administration;
- write access to the source config;
- Jellyfin, Plex, Radarr, Sonarr, TMDB, or notification credentials as plugin
  fields;
- media/download mounts or the whole media network;
- root on the host; or
- any production restore capability.

A purpose-built local coordinator can expose only `status`, graceful `stop`,
and `start` for one immutable container/Compose identity and authenticate only
the Homelab Backup service. A host-side snapshot coordinator that returns an
immutable read-only snapshot handle could instead avoid downtime, but only if
its atomicity over the whole config bind is guaranteed and audited. Selecting
and approving one mechanism is the user decision gate.

The config contains credentials, so grant only traversal/read ACLs to a
dedicated backup identity; do not make it world-readable. The current
Jellyseerr image's root runtime may have created root-owned files, but that does
not justify running the backup engine with host root. `test()` remains strictly
non-destructive: verify configuration, source type/path, regular-file safety,
readability, JSON shape, SQLite read-only open/schema, and coordinator status.
It must never stop the service or contact any configured integration.

## Proposed artifact contract

Publish one private archive through `create_backup_artifact()` or
`write_backup_bytes()` only after validation. The internal v1 layout is exact:

```text
jellyseerr-v1/
├── manifest.json
├── database.sqlite3
└── settings.json
```

`manifest.json` contains only non-secret recovery metadata:

- artifact contract version and creation timestamp;
- application `jellyseerr`, source version `2.7.3`, source commit, and pinned
  image digest;
- database engine `sqlite`, schema/table inventory, latest migration name, and
  bounded row counts;
- member names, sizes, modes, and SHA-256 hashes;
- excluded-state declarations; and
- required restore image and `restore_capability`.

The archive and external disaster-recovery sidecar must be regular files with
mode `0600`. Member names are fixed; reject duplicates, extras, absolute paths,
traversal, links, devices, FIFOs, sockets, sparse/oversized members, unreasonable
compression ratios, and any source that crosses a mount or symlink. Do not put
secret values in filenames. A failed validation must leave no published
artifact or sidecar.

The artifact legitimately contains secrets inside `database.sqlite3` and
`settings.json`; tests should prove secrets are absent from manifest, sidecar,
logs, exceptions, and metrics rather than incorrectly asserting they are absent
from the encrypted-by-storage-policy artifact payload.

## Supported restore contract

Declare `restore_capability = "partial"` with a create-only, local-disposable
workflow. “Partial” describes operational automation: the artifact restores all
authoritative Jellyseerr state, but Homelab Backup must not overwrite a live
deployment or restore production.

Before any destination mutation:

1. verify the outer sidecar, artifact size/hash, archive bounds, exact member
   set, internal manifest, and every member hash;
2. parse settings without logging it and re-run all database integrity,
   foreign-key, schema, and migration checks;
3. require an explicitly labeled disposable restore root that is new or empty;
4. reject symlinks, mount escapes, non-regular parents, source/backup overlap,
   the production source path, and any destination without the local-drill
   sentinel; and
5. verify enough free space and the exact 2.7.3 image identity.

Restore into a private staging directory, create `db/db.sqlite3` and
`settings.json` with restrictive modes, fsync files and directories, and rename
the complete tree into its fresh destination atomically. Never perform an
in-place merge. Do not restore cache, logs, old settings, temporary files, or
WAL/SHM. The destination must be writable by the isolated Jellyseerr container
because startup may run migrations, write settings migration backup/temp files,
and recreate caches/logs.

Boot only the exact 2.7.3 image for this contract. Starting current Seerr 3 on
the restored directory is a separate upgrade/migration exercise, not proof that
the Jellyseerr backup restored correctly. No production restore endpoint should
exist.

## Exact local two-backup / two-fresh-restore drill

The entire drill runs on the dev VM with synthetic credentials and state. It
uses unique temporary directories, container names, ports, and one Docker
`internal: true` network. It must have no route to production, no production
DNS or secrets, no production mounts, and no shared names with production. All
HTTP probes run inside the isolated network or container.

### Fixture and backup A

1. Pin
   `fallenbagel/jellyseerr:2.7.3@sha256:4538137bc5af902dece165f2bf73776d9cf4eafb6dd714670724af8f3eb77764`
   and assert version/source identity before use.
2. Create source, restore-A, restore-B, artifact, and stub directories with
   mode `0700`. Write a sentinel to each restore root before it is handed to the
   restore harness.
3. Start local HTTP stubs for Jellyfin/TMDB/Radarr/Sonarr/notification targets
   on the internal network. Their unique synthetic credentials and markers must
   never match production names or values.
4. Create an exact 2.7.3 instance. Seed through supported local API flows where
   feasible. The exact upstream
   [`prepareTestDb.ts`](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/scripts/prepareTestDb.ts)
   is the fixture precedent: it creates an admin and friend with local
   passwords. Run the exact migrations and add representative media, movie and
   TV requests/seasons, user settings, a blacklist entry, issue/comment,
   watchlist, override rule, and session through the application/API-backed
   test harness. Do not handcraft an incomplete schema.
5. Set distinctive A-only non-secret markers in application title, schedule,
   users, and request/status data. Authenticate at `/api/v1/auth/local`, then
   verify `/api/v1/auth/me`, `/api/v1/request`, and relevant admin/user APIs.
   Fixture users retain a non-null synthetic Plex ID so local login does not
   attempt a Plex synchronization.
6. Invoke backup A through the real plugin/lifecycle adapter. Assert graceful
   stop, source read-only behavior, successful restart and
   `/api/v1/settings/public` readiness, exact members, valid sidecar and hashes,
   both SQLite checks, restrictive modes, and no synthetic secret in observable
   output.

### Source mutation and backup B

1. With the source running, mutate it through supported test APIs: change the
   application marker/schedule, add a second user and user preference, create a
   second media/request/season, change a request status, and add distinct
   blacklist, issue/comment, watchlist, and override-rule markers. The internal
   stubs satisfy all integrations; no outbound request is possible.
2. Invalidate or replace a session deliberately while preserving another, so
   session and `clientId` restoration are observable.
3. Invoke backup B through the same adapter and repeat every stop/restart,
   artifact, sidecar, validation, permissions, and redaction assertion.
4. Require A and B to have different artifact hashes, database hashes, settings
   hashes where settings changed, timestamps, and semantic inventories. A must
   not contain any B-only marker.

### Fresh restore A

1. Restore artifact A through the real create-only restore path into fresh
   destination A. The source instance and artifact directory remain mounted
   nowhere in the restore container.
2. Re-run offline hashes, JSON shape, `integrity_check`, `foreign_key_check`,
   schema, and latest-migration assertions.
3. Boot the exact pinned image with only destination A and the isolated stub
   network. Never edit restored `settings.json` to make it boot.
4. Require `/api/v1/settings/public`, local admin login, `/api/v1/auth/me`, user
   and request APIs, expected A users/requests/statuses, blacklist,
   issue/comment, watchlist, override rule, settings markers, unchanged API key
   and `clientId`, and valid session behavior. Compare secret values only in
   process memory using hashes/constant-time equality; never print them.
5. Prove all B-only markers are absent. Prove excluded cache/log/anime-list
   state is absent initially or recreated without affecting semantics.

### Fresh restore B

Repeat the same workflow into independent destination B and require every B
delta while retaining A baseline state. Destination B must have a different
path, inode tree, container, and volume. Destroy both disposable restore stacks
after recording only non-secret assertions.

The drill passes only if **both independently created artifacts restore into
two independently fresh destinations** and every semantic difference is
correct. Starting containers or passing SQLite checks alone is insufficient.

### Mandatory negative and interruption cases

Automated local tests must also prove:

- missing, empty, malformed, or type-invalid `settings.json` fails closed;
- corrupted/truncated SQLite, failed integrity or foreign-key check, missing or
  unknown migration, and unexpected database engine fail closed;
- missing/bad sidecar, altered member hash, extra/duplicate member, traversal,
  absolute path, symlink/hardlink, special file, bomb bounds, or non-empty
  destination is rejected before mutation;
- a symlinked/mount-escaped source or a changing source after verified stop is
  rejected;
- stop timeout, backup timeout, cancellation, disk-full, validation failure,
  restart failure, and readiness timeout never publish success;
- every path that stopped a healthy source attempts exactly one bounded restart
  in `finally`, while an initially stopped source remains stopped; and
- no lifecycle or restore operation can name anything except the disposable
  local Jellyseerr fixture.

## STOP conditions

Stop research-to-implementation or fail the run rather than guessing when:

- the user has not explicitly accepted the brief production outage and
  approved the exact narrow lifecycle/snapshot mechanism;
- effective production configuration selects PostgreSQL, a custom config path,
  or an environment-provided `API_KEY`; that is a different researched
  contract;
- the deployed image/digest is not the pinned 2.7.3 identity or its database
  contains a later/unknown migration;
- more than one process/container can write the same config root, or the exact
  writer cannot be fully quiesced;
- the lifecycle coordinator cannot prove original state, graceful stop, stopped
  state, and restart/readiness, or exposes general Docker/host authority;
- the config source is writable by the plugin, unreadable, a symlink, crosses a
  mount, changes after quiescence, or lacks either authoritative file;
- `settings.json.tmp` remains after graceful stop, JSON is invalid, or required
  critical structure is inconsistent with database initialization state;
- SQLite Backup API/CLI cannot open the source read-only, does not produce a
  standalone database, or either validation PRAGMA fails;
- a future release stores authoritative bytes outside the two documented
  stores, or an integration is mistaken for media ownership;
- artifact secrecy, bounds, atomic publication, or restart-on-failure cannot be
  guaranteed; or
- any restore destination is production, reachable as production, non-empty,
  unlabeled, path-overlapping, or not fully isolated from production networks,
  DNS, credentials, and storage.

## User decision required

One decision is required before a production-capable plugin plan can proceed:

> Approve a short Jellyseerr-only outage for every backup and choose a narrow
> allowlisted lifecycle coordinator, or provide an audited atomic snapshot
> mechanism over `/docker-apps/jellyseer/config`.

Recommended: approve the brief outage and a purpose-built coordinator exposing
only Jellyseerr `status`, graceful `stop`, and `start`. This is simpler to prove
than live cross-store snapshot plumbing and materially safer than a Docker
socket. Everything else in the artifact and local restore contract is fully
buildable and testable on the dev VM without further user input.

## Primary sources

- [Jellyseerr 2.7.3 backup and restore documentation](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/docs/using-jellyseerr/backups.md)
- [Jellyseerr 2.7.3 database configuration](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/docs/extending-jellyseerr/database-config.mdx)
- [Jellyseerr 2.7.3 SQLite datasource](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/datasource.ts)
- [Jellyseerr 2.7.3 settings implementation](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/lib/settings/index.ts)
- [Jellyseerr 2.7.3 startup and session implementation](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/index.ts)
- [Jellyseerr 2.7.3 authentication/setup routes](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/routes/auth.ts)
- [Jellyseerr 2.7.3 public/status routes](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/server/routes/index.ts)
- [Jellyseerr 2.7.3 Dockerfile](https://github.com/seerr-team/seerr/blob/e842036fafc9818ea38d2e19d4adb66e17e2aebf/Dockerfile)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [SQLite PRAGMA integrity and foreign-key checks](https://www.sqlite.org/pragma.html#pragma_integrity_check)
- [SQLite write-ahead logging](https://www.sqlite.org/wal.html)
