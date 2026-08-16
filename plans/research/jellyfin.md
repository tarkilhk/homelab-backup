# Jellyfin 10.11.11 backup and restore current-contract research

Research date: 2026-08-16

Scope: the existing `jellyfin` plugin, its tests and legacy evidence, the
current `homelab-infra` declaration, and exact first-party Jellyfin 10.11.11
source, documentation, packaging, and registry data. No production host,
endpoint, container, credential, configuration, or data was contacted or
changed. Network activity was limited to read-only official GitHub, Jellyfin,
and Docker Hub sources.

## Decision summary and active STOP

The 10.11 native backup API is useful, but it is not presently an honest
complete-service recovery boundary for this program. Three independent facts
require explicit user decisions before implementation or acceptance drilling:

1. **The live archive is not one coherent recovery point.** Jellyfin serializes
   every EF database table inside one read transaction, then copies
   configuration files, root-library files, collections, playlists, scheduled
   task state, and optional metadata sequentially after that transaction. No
   shared snapshot or mutation fence spans those reads. Jellyfin supports an
   online built-in backup but recommends low activity and no active scan; that
   is operational advice, not the program's required consistency proof
   ([official backup guide](https://jellyfin.org/docs/general/administration/backup-and-restore/),
   [exact backup implementation](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L264-L435)).
   Strict consistency therefore needs a proved source write-quiescence fence.
   The native API exposes none. The first-party dependable alternative is a
   stopped-server manual copy of the complete data/config directory, which
   means production downtime and a broader source mount or narrow snapshot
   helper. Both require approval.
2. **There is no least-privilege Jellyfin backup credential.** The whole backup
   controller requires elevation. Exact 10.11.11 authentication maps every API
   key to the Administrator role and calls API keys unrestricted
   ([controller policy](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Controllers/BackupController.cs#L18-L68),
   [API-key role mapping](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Auth/CustomAuthenticationHandler.cs#L43-L77),
   [default authorization](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Auth/DefaultAuthorizationPolicy/DefaultAuthorizationHandler.cs#L41-L56)).
   The user must approve either this global administrative authority or a
   method/path/body-restricting proxy that keeps the Jellyfin key out of
   Homelab Backup and makes the direct origin unreachable to it.
3. **The native archive is intentionally narrower than `/config`.** It omits
   `/config/plugins`, including plugin binaries and
   `plugins/configurations`, and omits other data files such as
   `/config/data/device.txt`. It cannot recreate a fresh empty Jellyfin system
   by itself. Jellyfin explicitly says the built-in system can restore only
   systems on which the backup was originally made and is not a migration tool
   ([10.11 release explanation](https://jellyfin.org/posts/jellyfin-release-10.11.0/#internal-backup--restore-support),
   [exact archive inventory](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L373-L435),
   [application paths](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Emby.Server.Implementations/AppBase/BaseApplicationPaths.cs#L31-L82)).
   Git cannot establish whether production has active plugin configuration or
   other omitted authoritative state. The user must choose a deliberately
   partial native-database/config claim after that state is inventoried, or a
   stopped full-`/config` recovery boundary.

Until all three policies are selected, **stop implementation, drills, and
production rollout**. Do not call an online low-activity export transactional,
silently retain the existing global API key, or describe a fresh empty-volume
restore as complete Jellyfin disaster recovery.

The existing `restore_capability = "automatic"` is not supported by exact
source. The restore endpoint only schedules a destructive restart; its startup
restore overwrites files, purges the database outside an encompassing
transaction, and has no terminal status callback or rollback. The existing
plugin can be at most `partial` unless a future implementation owns and proves
a disposable destination lifecycle and a narrower restorable-state claim.

## Exact declaration, image, and source identity

The inspected `homelab-infra` revision is
`01eae07691699a7f47a3794e9095240b672aa020`. It declares the mutable tag
`jellyfin/jellyfin:10.11.11` in
[`docker.compose/media/jelly_misc/jelly_misc.yaml`](../../../homelab-infra/docker.compose/media/jelly_misc/jelly_misc.yaml).
Git proves intended configuration, not the image currently running in
production.

Read-only official Git and registry resolution produced:

| Property | Exact identity |
| --- | --- |
| Declared image | `docker.io/jellyfin/jellyfin:10.11.11` |
| OCI index | `sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db` |
| linux/amd64 manifest | `sha256:0b901391a662862eddb5dc55d244d7883cbb6236ef5b9a6ea82abc78a89819f0` |
| linux/arm64 manifest | `sha256:7536c1009c6ea50dadd2b244165efb357504ca0f2670abefbceb1c773cc7e13d` |
| Exact amd64 drill reference | `jellyfin/jellyfin@sha256:0b901391a662862eddb5dc55d244d7883cbb6236ef5b9a6ea82abc78a89819f0` |
| Jellyfin server | v10.11.11, [`1fbd8739292cce610231be93daf43368733edf63`](https://github.com/jellyfin/jellyfin/commit/1fbd8739292cce610231be93daf43368733edf63) |
| Official packaging | [`a5c7e85a759ca5b038f943033f05be695fe7c16e`](https://github.com/jellyfin/jellyfin-packaging/commit/a5c7e85a759ca5b038f943033f05be695fe7c16e) |
| Backup engine | `0.2.0` in the exact server source |

The official [server release](https://github.com/jellyfin/jellyfin/releases/tag/v10.11.11)
and [packaging release](https://github.com/jellyfin/jellyfin-packaging/releases/tag/v10.11.11-202606061137)
resolve to those commits. The registry identities are reproducible with
`docker buildx imagetools inspect jellyfin/jellyfin:10.11.11` and
`docker manifest inspect --verbose jellyfin/jellyfin:10.11.11`; Docker Hub is
the first-party distribution source for the
[`10.11.11` tag](https://hub.docker.com/v2/repositories/jellyfin/jellyfin/tags/10.11.11).

The official image sets data `/config`, cache `/cache`, configuration
`/config/config`, logs `/config/log`, and its health endpoint on port 8096
([pinned Dockerfile](https://github.com/jellyfin/jellyfin-packaging/blob/a5c7e85a759ca5b038f943033f05be695fe7c16e/docker/Dockerfile#L122-L145),
[entrypoint and healthcheck](https://github.com/jellyfin/jellyfin-packaging/blob/a5c7e85a759ca5b038f943033f05be695fe7c16e/docker/Dockerfile#L247-L265)).
The deployment overrides the HTTP port to 56905 and separately mounts:

- `/docker-apps/jellyfin/config:/config`;
- `/docker-apps/jellyfin/cache:/cache`; and
- `/mnt/nas-media:/media`.

Homelab Backup receives only
`/docker-apps/jellyfin/config/data/backups:/jellyfin-backups` in
[`docker.compose/system/homelab-backup/homelab-backup.yaml`](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml).
That mount is exactly sufficient to collect native server-local archives. It
cannot read a full `/config` backup source. The two services are also declared
on different Compose networks, so the route used by a configured target must
not be inferred from Git.

The image tag is multi-platform and not digest-pinned. The dev server is
linux/amd64, but Git does not prove the platform or resolved digest of the
production container. A later approved deployment must inspect that identity;
the local drill must use the exact amd64 manifest above and must not silently
substitute the mutable tag.

## Authoritative recovery boundary

### What the native archive includes

With the existing request (`Database=true`, all optional payload flags false),
10.11.11 writes:

- JSON arrays for every public `DbSet` in `JellyfinDbContext`, plus EF migration
  history;
- all top-level `*.xml` and `*.json` files from `/config/config`;
- `/config/config/users` and `/config/config/ScheduledTasks` recursively;
- `/config/root` recursively;
- `/config/data/collections`, `/config/data/playlists`, and
  `/config/data/ScheduledTasks` recursively; and
- `manifest.json` containing `ServerVersion`, `BackupEngineVersion`,
  `DateCreated`, `DatabaseTables`, and the selected options.

The exact inventory is in the first-party
[`BackupService`](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L320-L435)
and its
[`BackupManifest`](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupManifest.cs).
The database model includes users, password/authentication state, permissions,
preferences, API keys, devices, library items and provider identifiers, image
records, media streams, collections, display preferences, and user watch/data
state
([exact context](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/src/Jellyfin.Database/Jellyfin.Database.Implementations/JellyfinDbContext.cs#L31-L169)).

This is secret-bearing state. It contains password material, unrestricted API
keys, internal paths, usernames, library catalog and watch history, devices,
and possibly private network configuration. Artifacts must be mode 0600;
member values, tokens, paths, usernames, titles, and identifiers must never be
logged or copied into sidecars.

### Optional native members

`Metadata=true` adds the internal and, when different, default metadata
directories. `Subtitles=true` adds extracted/downloaded subtitles.
`Trickplay=true` adds generated trickplay data. Jellyfin documents these four
choices and requires 5 GiB free in the backup directory regardless of the
actual archive size
([official guide](https://jellyfin.org/docs/general/administration/backup-and-restore/#create-a-built-in-backup),
[exact free-space check](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L275-L301)).

The current plugin excludes all three optional trees. Subtitles and trickplay
are reproducible/media-adjacent and fit the program exclusion. Metadata is more
ambiguous: it may contain fetched images but also user-selected or custom
artwork that cannot be reconstructed exactly. A partial native contract must
explicitly decide whether custom metadata is authoritative. `Metadata=false`
cannot be described as preserving it.

### What native backup omits

The archive does not copy the whole `/config` volume. Important omissions
include:

- `/config/plugins`, including installed packages and
  `/config/plugins/configurations`;
- `/config/data/device.txt`, the persistent device identity file;
- arbitrary files below `/config/data` outside the selected directories;
- `/config/log` and runtime locks; and
- `/cache` and every `/media` payload byte.

Plugins are rooted at `ProgramDataPath/plugins`, while backup only copies the
specific config/root/data paths listed above
([application paths](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Emby.Server.Implementations/AppBase/BaseApplicationPaths.cs#L31-L82),
[device path](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Emby.Server.Implementations/Devices/DeviceId.cs#L21-L35)).

The exclusions of `/cache`, logs/telemetry, transcodes, generated subtitles,
trickplay, and source media are aligned with the program. Plugin
configuration, custom metadata, and system identity are not safely classed as
reproducible from the declaration. A read-only production inventory is needed
to determine whether any are active, but this research did not contact
production. Until then, authoritative state is unresolved rather than silently
dropped.

Infrastructure remains separately owned by `homelab-infra`: image, port,
environment, volumes, media mounts, reverse proxy, DNS, TLS, and restart policy
are not artifact contents. Source media remains under its own NAS data policy.

## Exact native API, authentication, and shared-path contract

The exact controller exposes four elevated operations:

| Method and path | Semantics |
| --- | --- |
| `POST /Backup/Create` | Synchronously creates a server-local ZIP and returns its manifest/path. |
| `GET /Backup` | Lists manifests for ZIP files in the server backup directory. |
| `GET /Backup/Manifest?path=...` | Reads a manifest after reducing the supplied path to a basename under the backup directory. |
| `POST /Backup/Restore` | Schedules restore of a basename already in the backup directory and an in-process restart; returns 204 before restore. |

These are defined by the pinned
[`BackupController`](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Controllers/BackupController.cs).
There is **no archive download endpoint**. `Create` returns JSON with a
server-local `Path`; Homelab Backup must obtain the bytes from the declared
shared `/config/data/backups` bind. The official Docker path is documented as
`<volume>/config/data/backups`
([official guide](https://jellyfin.org/docs/general/administration/backup-and-restore/#create-a-built-in-backup)).

`POST /Backup/Create` completes only after the ZIP writer closes. It deletes
the candidate on a caught generation failure, so an exact 200 response plus a
valid returned manifest is the native completion boundary. The file is created
directly at its final server-side name rather than temporary-name-plus-rename,
so it is visible as partial while the request is still running. Consumers must
never scan/copy it before the HTTP response completes
([creation lifecycle](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L294-L460)).

The filename has only one-second resolution and `File.OpenWrite` is not an
exclusive target-level scheduler. The implementation must prevent concurrent
creates for one source, snapshot pre-existing basenames, require the returned
basename to be new, and never delete a collision it did not prove it owns.

Connectivity and readiness can use unauthenticated
`GET /System/Info/Public`; exact source exposes Version there
([system controller](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Controllers/SystemController.cs#L87-L105)).
The current authenticated `GET /System/Info` test needlessly exercises the
broad credential and accepts any nonempty version. The current contract must
require exactly `10.11.11` and refuse redirects/origin changes.

### Administrative authority is unavoidable natively

`Policies.RequiresElevation` requires the Administrator role
([policy registration](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server/Extensions/ApiServiceCollectionExtensions.cs#L66-L93)).
Every API key is mapped to that role, and first-party authorization comments
state API keys are unrestricted
([permission handler](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Auth/UserPermissionPolicy/UserPermissionHandler.cs#L27-L51)).
There is no native endpoint scope, backup-only key, or read-only key.

The existing plugin schema calls the secret `api_key`, and the plugin sends it
as a MediaBrowser token. Legacy documentation records successful connectivity
and native backup triggers. Those successful backup calls necessarily used an
elevation-capable credential; if it was an API key, it was globally
administrative by exact source. A user-admin access token would be equally
elevated for this controller. The present target and credential type cannot be
proved from Git because target data lives in the runtime database. No current
production credential should be inferred or inspected without separate
approval.

Backup creation is not semantically read-only inside Jellyfin: before opening
the transaction, it executes WAL checkpoint, `PRAGMA optimize`, `VACUUM`, and a
second checkpoint
([SQLite optimization](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/src/Jellyfin.Database/Jellyfin.Database.Providers.Sqlite/SqliteDatabaseProvider.cs#L98-L109)).
That is first-party behavior and part of why backup should run off-peak. It
does not change the program rule that production backup triggers occur only
after an approved deployment.

## Strict consistency analysis

The database portion is strong in isolation. Jellyfin obtains applied
migrations and every modeled table through one EF database context and one
`BeginTransactionAsync`, writing JSON arrays while that read transaction is
alive. This provides one database snapshot for all serialized tables
([database export](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L303-L370)).

The archive as a whole is not transactional. After the database transaction is
disposed, Jellyfin enumerates and copies mutable config, root, collection,
playlist, scheduled-task, and optional metadata files one at a time. A user
edit, API request, scan, scheduled task, plugin write, or metadata update can
therefore make the file projection disagree with the earlier database
projection or with itself. The API neither pauses jobs nor blocks writes.

First-party documentation says the built-in backup can run online but still
recommends low activity with no scan. That makes ordinary vendor-supported
online backups usable, but it does not prove the requested single coherent
recovery point. Two consecutive semantically equal exports reduce the chance
of a race but cannot turn the sequential algorithm into a vendor-guaranteed
snapshot.

The strict options are therefore:

- **Stopped full `/config` copy (recommended):** approve a short scheduled
  outage; stop Jellyfin cleanly; expose or snapshot `/config` read-only; archive
  the complete authoritative tree with explicit cache/log/backups exclusions;
  then restart. Jellyfin's manual backup instructions explicitly require stop
  before copying and its manual restore replaces the data/config directory
  while stopped
  ([official manual contract](https://jellyfin.org/docs/general/administration/backup-and-restore/#manual-backup)).
- **Externally proved native quiescence:** block every administrative, user,
  scan, task, and plugin mutation for the entire create interval while leaving
  the process up. No first-party API implements or proves such a fence. A new
  narrow orchestration component would need to make the guarantee observable.
- **Explicit weaker exception:** accept the vendor online, low-activity native
  archive as a deliberately non-atomic partial boundary. This is a policy
  relaxation, not an implementation fact, and is not selected by this note.

The current backend has neither a full `/config` mount nor service-lifecycle
authority. A stopped copy consequently entails downtime plus a deployment
mount/snapshot-orchestration change. Stop and obtain approval before choosing
it.

## Exact restore semantics and safety limit

The web/API restore requires the archive already exist in Jellyfin's backup
directory. `POST /Backup/Restore` sanitizes the request to a basename, verifies
existence, records the path, sets `ShouldRestart`, waits 500 ms in a background
task, and stops the application. It returns 204 immediately after scheduling,
not after validation or restore
([controller](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Api/Controllers/BackupController.cs#L50-L68),
[restart scheduling](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L70-L80)).

On the next in-process server cycle Jellyfin restores before normal startup,
then starts another normal cycle. The public API is unavailable during that
interval
([program loop](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server/Program.cs#L142-L152),
[restore startup branch](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server/Program.cs#L193-L238)).
The official UI documentation likewise warns that restore immediately restarts
the server and makes it unavailable. The supported CLI alternative is
`jellyfin --restore-archive PATH`
([official restore guide](https://jellyfin.org/docs/general/administration/backup-and-restore/#restore-from-a-built-in-backup)).

Before mutation the restore checks only that:

- the file and `manifest.json` exist;
- `ServerVersion` is not newer than the running server; and
- `BackupEngineVersion` equals exactly `0.2.0`.

It then overwrites represented config/data/root/metadata files without deleting
unrepresented destination files. For database restore it rewrites migration
history, executes deletes for all modeled tables, deserializes each available
table, and calls `SaveChangesAsync`. A missing non-history table is logged and
skipped. There is no transaction encompassing file writes, table purge, and
inserts
([exact restore](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/Jellyfin.Server.Implementations/FullSystemBackup/BackupService.cs#L83-L251),
[SQLite purge](https://github.com/jellyfin/jellyfin/blob/1fbd8739292cce610231be93daf43368733edf63/src/Jellyfin.Database/Jellyfin.Database.Providers.Sqlite/SqliteDatabaseProvider.cs#L188-L207)).

A failure can therefore leave overwritten files and a purged or partially
repopulated database. There is no rollback, restore job, completion endpoint,
or failure response to the original 204 caller. Readiness after a restart is
necessary but not sufficient: it proves only that some post-restore server
started. The current plugin's `partial` result when no outage was observed is
not restore evidence, and even an observed outage followed by Version is not
content evidence.

For local safety, every restore must target a disposable exact-image service
with a newly created destination volume, no published ports, no production
mounts, and no route to production. Yet a fresh empty volume is outside the
vendor's complete-system guarantee because omitted identity/plugin state is
not recreated. Such a drill can prove only the explicitly selected native
subset. General automatic disaster recovery remains false.

## Method/path proxy feasibility

A proxy can technically bound **production backup-trigger authority**, but only
if it is the credential boundary rather than a transparent route for the same
global key:

1. The global Jellyfin API key is stored only in the narrowly trusted proxy.
2. Homelab Backup holds a distinct proxy credential; the proxy strips any
   supplied Jellyfin authorization and injects its server-held key.
3. The proxy allows exact `POST /Backup/Create` with a fixed bounded JSON body
   and, if deliberately used, exact `GET /Backup` or
   `GET /Backup/Manifest`; it denies every other method/path/query before
   forwarding.
4. Connectivity/readiness uses direct or proxied unauthenticated
   `GET /System/Info/Public`.
5. The direct Jellyfin origin is unreachable from Homelab Backup, redirects are
   disabled, the upstream origin is fixed, and logs redact authorization and
   response paths.
6. Production never exposes `POST /Backup/Restore` through this proxy. Local
   disposable restore destinations use their own unique local administrative
   credential or a separate restore-only proxy.

Merely putting a proxy in front while giving Homelab Backup the global key is
not sufficient: a leaked key remains usable against the host-published or
HAProxy-exposed direct origin. Deploying the sufficient design changes
production topology, credential storage, and network policy, so it needs
explicit approval and its own denial tests.

The proxy solves authority scope, not archive consistency or omitted state.

## Strict artifact validation and publication

HTTP 200, a `.zip` suffix, nonempty bytes, `manifest.json`, and one `Database/`
member are insufficient. Before publication and again before restore, validate:

- source is a regular file below the configured shared root, not a symlink,
  hard-link surprise, device, or pre-existing basename;
- bounded file size, ZIP entry count, total compressed/uncompressed bytes,
  per-entry size, and compression ratios;
- central-directory integrity and CRC for every member;
- no encrypted, absolute, traversal, drive-prefixed, duplicate-normalized,
  control-character, symlink, or special-file member;
- exactly one root `manifest.json`, valid bounded UTF-8 JSON, exact
  `ServerVersion=10.11.11`, exact `BackupEngineVersion=0.2.0`, sane UTC date,
  and exact approved options;
- nonempty, unique `DatabaseTables`; one valid JSON-array member for every
  declared table; required migration `HistoryRow.json`; and no declared table
  silently absent even though upstream restore would continue;
- expected `Config/`, `Root/`, collections/playlists/tasks, and selected
  optional prefixes, with exact-path allowlists and semantic parsers for
  critical XML/JSON; and
- a canonical secret-safe member inventory/count/hash projection suitable for
  later round-trip comparison.

The API response manifest and archive manifest must agree. The plugin should
copy the closed source through `create_backup_artifact()` so only a fully
written, fsynced, nonempty artifact and valid sidecar become visible. Reopen
and validate the published path before deleting only the uniquely owned
server-side source. On any failure, preserve ownership boundaries, remove only
plugin-created temporary files, and publish nothing.

Sidecars may record exact server/engine/image identities, options, aggregate
member/table counts, byte size, SHA-256, and validator version. They must not
contain the server-returned absolute path, usernames, media paths, titles,
API-key names/values, device identifiers, or member contents.

The implementation must also enforce canonical HTTP(S) origin configuration:
no userinfo, query, fragment, ambiguous path, cross-origin redirect, or secret
in URLs/errors. Remove the schema's fake `your_api_key` default. Plain HTTP is
appropriate only on an isolated local drill network; production TLS policy
must be explicit.

## Current plugin and evidence gap

The existing
[`backend/app/plugins/jellyfin/plugin.py`](../../backend/app/plugins/jellyfin/plugin.py)
has useful staging and central artifact mechanics, but it does not meet this
contract:

- version checks accept any nonempty server version and archive server version;
- the schema ships a fake secret default;
- the broad administrative key is neither classified nor constrained;
- no concurrent-create or pre-existing-basename ownership check exists;
- archive validation lacks exact options/version/table completeness, bounds,
  duplicate/path/special-file rules, and semantic content checks;
- `Metadata=false` is undocumented as a possible loss of custom artwork;
- restore overwrites an existing staged name, leaves staged files on some
  exception paths, and has no isolation/freshness/provenance guard;
- restart/readiness is treated as success without restored semantic evidence;
  and
- `restore_capability = "automatic"` overstates the nontransactional,
  no-terminal-status upstream contract.

Legacy v0.2.1 documentation records connectivity, two native archives, and one
isolated restore/readiness transition. That is not the current program's two
independent backup-to-fresh-restore rounds, and the historical evidence did not
prove complete `/config` recovery, exact digest, strict consistency, least
privilege, archive table completeness, second-restart durability, or restored
semantic state. Current documentation must not present that legacy result as a
completed current contract.

## Two-clean-round drill feasibility after decisions

### Partial native subset

If the user explicitly accepts the narrower native boundary, resolves custom
metadata scope, provides a strict mutation fence or accepts the weaker online
exception, and chooses an authority policy, two local rounds are feasible for
that **partial subset**:

1. Pull the exact amd64 manifest once, record its config/manifest identity,
   then run with no outbound network and no published ports.
2. For round A and independently round B, create a new source volume, isolated
   network, backup share, unique credentials, and distinct synthetic semantic
   markers in users, configuration, library roots, collections/playlists, and
   API-key/database state. No scans or writes may overlap export.
3. Call public version readiness, then exact `POST /Backup/Create`; validate and
   publish one artifact and sidecar transactionally. A and B must have
   independent nonzero sizes, different SHA-256 values, and distinct content
   markers.
4. Destroy the source. Create a new isolated exact-image destination with a new
   empty volume and no production connectivity. Stage the artifact atomically,
   invoke restore with a destination-only administrative token, require an
   observed unavailable-to-ready transition, and require exact 10.11.11.
5. Prove restored state independently: authenticate with restored source
   identity, reject or retire the destination bootstrap identity, read and
   compare representative users/policies, configuration, libraries,
   collections/playlists, and markers. Take a post-restore native archive and
   compare its canonical restorable projection with the source archive.
6. Trigger a second normal server restart and repeat readiness and semantic
   assertions. Preserve secret-safe logs with container/image identity,
   transition timing, artifact size/hash/sidecar proof, canonical counts/hashes,
   and teardown evidence.
7. Destroy every round's containers, volumes, networks, credentials, source
   archives, and staged copies.

These rounds prove the declared native subset only. They must include negative
tests for wrong version/engine/options, missing table/history, CRC corruption,
zip bomb/path tricks, pre-existing destination, no restart, readiness without
markers, source-key failure, proxy-denied methods, and a restore that becomes
partially mutated/unready.

### Complete service boundary

A complete Jellyfin disaster-recovery drill is also technically feasible, but
it is a different, stopped-filesystem contract:

- create a local exact-image source with plugins/configuration/device identity
  and synthetic database/config/metadata state;
- stop it cleanly and archive the approved full `/config` projection;
- restore that archive into a fresh empty `/config` while Jellyfin is stopped;
- start and prove exact identity, plugin configuration, users, libraries,
  custom metadata, database state, readiness, and second-restart durability;
  and
- repeat from scratch for B.

That full contract matches Jellyfin's official manual backup/restore boundary
but production use requires a short outage and a broader read-only source mount
or narrowly scoped host snapshot helper. The current native-only plugin and
deployment cannot implement it without those approvals.

## Decisions required before implementation

The user must select and authorize all applicable items:

1. **Consistency and boundary:** approve the recommended stopped full-`/config`
   backup window plus a read-only mount/snapshot mechanism, or explicitly
   accept a narrower non-atomic native online boundary. A strict native
   quiescence fence may be selected only if its enforcement can be designed and
   proved.
2. **Omitted authoritative state:** inventory active production plugins and
   custom metadata, then either include them in the full boundary or explicitly
   classify each omitted item as rebuildable/external/deliberately unprotected.
3. **Authority:** approve a global Jellyfin administrative credential, or the
   recommended proxy-held key with exact production create-only allowlisting
   and direct-origin network isolation.
4. **Restore claim:** accept `partial` for the native subset, or choose the full
   stopped-filesystem contract before any capability is called automatic.

No production action is needed or permitted to resolve the source-code facts.
Any later production inventory, target/configuration change, key creation,
proxy deployment, broader mount, service stop, or backup trigger remains a
separate explicitly controlled step.
