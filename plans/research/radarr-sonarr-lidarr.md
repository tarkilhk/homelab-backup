# Radarr, Sonarr, and Lidarr exact native recovery research

## Outcome

The exact locally declared Radarr, Sonarr, and Lidarr releases share the native
Servarr online SQLite backup and staged-restart restore protocol already
hardened for Readarr and Prowlarr. They can therefore be implemented as three
thin, clean-breaking `ServarrPlugin` adapters. No compatibility alias, alternate
API version, HTTP-download fallback, cookie credential, or second protocol is
needed.

Local implementation and isolated recovery proof do **not** require production
contact or source downtime. Production activation must stop for explicit user
approval of three separate changes:

1. grant Homelab Backup the applications' single, application-wide API keys;
2. add one narrow read-only native `Backups/manual` bind per application; and
3. allow deletion of only the newly attributed native manual backup after its
   validated Homelab Backup artifact and sidecar are durably published.

Upstream exposes no backup-scoped credential. The API key can call mutating
endpoints beyond backup, so it is broader than least-privilege backup authority.
The production compose also currently runs all three applications as UID/GID 0;
that existing application-container privilege is not required by the plugin and
must not be copied into a backup worker.

This research used the read-only `homelab-infra` checkout, exact OCI registry
manifests, exact tagged upstream source, exact LinuxServer image source, and the
current Homelab Backup source. No production endpoint was contacted and no
container, deployment, target, schedule, or credential was changed.

## Exact local declarations and immutable identities

The authoritative local declaration is
`homelab-infra/docker.compose/media/radarr_sonarr_lidarr/radarr_sonarr_lidarr.yaml`
at commit `10e47e90989406143cf32f743bf545afc9ee964b`. The current Lidarr
declaration is `ls38`; Homelab Backup's compatibility matrix still saying
`ls29` is stale.

Registry identities were resolved directly from the GHCR OCI index and config
objects on 2026-08-16. The selected drill identity is always the linux/amd64
manifest, not the mutable tag or the multi-platform index.

| App | Local tag | OCI index | linux/amd64 manifest | Image source revision | Exact upstream tag revision |
| --- | --- | --- | --- | --- | --- |
| Radarr | `ghcr.io/linuxserver/radarr:6.3.0.10514-ls313` | `sha256:a45b5ab0f850f39edb4cc9c95bbd967b52ddc3d4574a4dfb45561177db6c88f4` | `sha256:263be1036419fcb38fc1cf76be90db8db4b0dc49fd492617b17cc58e9e0bf1b5` | [`b8e3a21`](https://github.com/linuxserver/docker-radarr/commit/b8e3a21dae7a54d6521f71c63a88e6f4cd977ac1) | [`7827e53`](https://github.com/Radarr/Radarr/commit/7827e5368947f158ad06f757334f5cde6c406411) |
| Sonarr | `ghcr.io/linuxserver/sonarr:4.0.19.2979-ls320` | `sha256:24acea2956a0ccb11f103877d9f4f8576600fb34bff34820ed749c2256dab89f` | `sha256:f6bf16c4c5a0c6c99833eab891671ded0f06f553f30c7b0702e98f455c5642cc` | [`5283fcb`](https://github.com/linuxserver/docker-sonarr/commit/5283fcba39c6cd7593fae6ad43b2a34356c864f9) | [`4ff1b78`](https://github.com/Sonarr/Sonarr/commit/4ff1b780010d3d9ec76a4864dce96b6494e9caea) |
| Lidarr | `ghcr.io/linuxserver/lidarr:3.1.0.4875-ls38` | `sha256:bfec0ec2dc351fa5928379d785b08be395886f109393b9040ed7973bd1008060` | `sha256:0199ff56d973da7b66158ba8823cf3eac905d47b6ab7524d213931debfa75225` | [`6b0f771`](https://github.com/linuxserver/docker-lidarr/commit/6b0f77114b8a057434e052086198444ceac2510a) | [`350860e`](https://github.com/Lidarr/Lidarr/commit/350860e524029b7fb4165ed14fbcabb11217ada2) |

Primary registry objects: [Radarr tag manifest](https://ghcr.io/v2/linuxserver/radarr/manifests/6.3.0.10514-ls313),
[Sonarr tag manifest](https://ghcr.io/v2/linuxserver/sonarr/manifests/4.0.19.2979-ls320),
and [Lidarr tag manifest](https://ghcr.io/v2/linuxserver/lidarr/manifests/3.1.0.4875-ls38).
Their OCI labels report the exact full package versions and the same LinuxServer
source revisions shown above. The image build histories fetch exact upstream
application versions rather than resolving a channel at runtime.

## Exact application contract

| App | API | `appName` | App version | `packageVersion` | SQLite migration | Exact DB member |
| --- | --- | --- | --- | --- | --- | --- |
| Radarr | `/api/v3` | `Radarr` | `6.3.0.10514` | `6.3.0.10514-ls313` | 242 | `radarr.db` |
| Sonarr | `/api/v3` | `Sonarr` | `4.0.19.2979` | `4.0.19.2979-ls320` | 217 | `sonarr.db` |
| Lidarr | `/api/v1` | `Lidarr` | `3.1.0.4875` | `3.1.0.4875-ls38` | 80 | `lidarr.db` |

The highest tagged migrations are explicitly numbered
[Radarr 242](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Datastore/Migration/242_add_movie_keywords.cs),
[Sonarr 217](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/NzbDrone.Core/Datastore/Migration/217_add_mal_and_anilist_ids.cs),
and [Lidarr 80](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/NzbDrone.Core/Datastore/Migration/080_update_redacted_baseurl.cs).
The status controllers return the database's applied maximum migration, database
type, version, package identity, and process start time
([Radarr](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Api.V3/System/SystemController.cs),
[Sonarr](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/Sonarr.Api.V3/System/SystemController.cs),
[Lidarr](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/Lidarr.Api.V1/System/SystemController.cs)).

### Exact status gate

`test()`, every backup, and both sides of restore must require an authenticated
`GET <api>/system/status` with all of the following:

- exact `appName`, application `version`, and full LinuxServer
  `packageVersion` from the table above;
- `databaseType` equal to `sqlite` and exact `migrationVersion`;
- a non-empty parseable `startTime`;
- `isDocker == true` in the exact-image drill; and
- OCI label architecture `amd64`, full image version, and source revision equal
  to the immutable matrix before any application API mutation.

The application API key is sent only as `X-Api-Key`. Reject redirects before a
credential can cross origins. Do not accept query credentials, bearer aliases,
UI credentials, an alternate API prefix, a version range, a package-version
fallback, PostgreSQL, or the historical `nzbdrone.db` alias.

## Native online backup semantics

For SQLite, all three tagged `BackupService` implementations:

1. empty a private application temp directory;
2. copy `config.xml`;
3. use SQLite's online `BackupDatabase` operation into the temp directory;
4. force the backup DB journal mode to truncate and remove its journal file;
5. write `INFO`; and
6. ZIP the temp directory into `Backups/manual` for a manual command.

The exact implementations are
[Radarr BackupService](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Backup/BackupService.cs),
[Sonarr BackupService](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/NzbDrone.Core/Backup/BackupService.cs),
[Lidarr BackupService](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/NzbDrone.Core/Backup/BackupService.cs),
and their SQLite backup helper
([Radarr copy](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Backup/MakeDatabaseBackup.cs)).
The helper is shared byte-for-byte by these Radarr and Sonarr tags; Lidarr uses
the same SQLite online-backup operation and truncate-journal contract.

No application stop, source restart, database lock escalation, Docker socket,
SSH, `/config` mount, or media/download access is required. The SQLite backup
API establishes a consistent database snapshot while the application remains
online. A PostgreSQL-configured instance would emit a config-only ZIP because
these services conditionally skip `BackupDatabase`; the exact contract must
reject it.

### Exact command and attribution state machine

The trigger is authenticated `POST <api>/command` with exactly
`{"name":"Backup"}`. Require a numeric command ID, then poll
`GET <api>/command/<id>`. All three exact command resources expose both
`status` and `result`; success is only `status == "completed"` and
`result == "successful"`. The command manager turns an otherwise unknown result
into successful on normal completion. Treat `failed`, `aborted`, `cancelled`,
and `orphaned` as terminal failures
([Radarr command resource](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Api.V3/Commands/CommandResource.cs),
[command manager](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Messaging/Commands/CommandQueueManager.cs)).

Before triggering, record the complete identity of every manual list entry from
`GET <api>/system/backup`: `id`, `name`, `path`, `type`, `size`, and `time`.
After command success, poll until exactly one previously unknown `manual` entry
at or after the whole-second trigger boundary exists. Reject zero, multiple,
stale, malformed, or unsafe candidates. The API constructs each path as
`/backup/manual/<native-name>` and its ID as a deterministic hash of type and
name
([Radarr backup controller](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Api.V3/System/Backup/BackupController.cs),
[Sonarr](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/Sonarr.Api.V3/System/Backup/BackupController.cs),
[Lidarr](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/Lidarr.Api.V1/System/Backup/BackupController.cs)).

Native filenames have only whole-second resolution. Serialize backups per exact
application origin and ensure A and B do not trigger within the same second.

### Why HTTP artifact download is forbidden

`/backup/...` is not an API endpoint. `BackupFileMapper` serves it through the
UI static-resource controller, whose `UI` authorization policy uses the
configured UI authentication scheme. The `X-Api-Key` scheme is the fallback for
API controllers, not the UI policy
([Radarr mapper](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Http/Frontend/Mappers/BackupFileMapper.cs),
[static controller](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Http/Frontend/StaticResourceController.cs),
[UI policy provider](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Http/Authentication/UiAuthorizationPolicyProvider.cs)).
With Forms authentication, an API-key request is redirected to login rather
than authorized to download the ZIP.

Do not store or automate a UI cookie and do not add an HTTP fallback. Bind only
the native manual directory read-only into the backend:

| App | Host source | Fixed backend path |
| --- | --- | --- |
| Radarr | `/docker-apps/radarr/config/Backups/manual` | `/sources/radarr/backups` |
| Sonarr | `/docker-apps/sonarr/config/Backups/manual` | `/sources/sonarr/backups` |
| Lidarr | `/docker-apps/lidarr/config/Backups/manual` | `/sources/lidarr/backups` |

Each bind must be a dedicated, genuine read-only mount. Correlate only the safe
API path basename to one regular non-symlink file directly inside the fixed
mount. Copy through a stable descriptor into a private transactional artifact,
with hard byte/time/member/expansion bounds and pre/post inode, size, timestamp,
and hash evidence.

### Native cleanup

Only after strict validation and durable atomic artifact plus sidecar
publication, call `DELETE <api>/system/backup/<attributed-id>`. The exact
controllers delete the file selected by that list ID. Never delete by guessed
filename, retention sweep, or baseline-relative position. If deletion fails,
fail the run but retain both the already published Homelab Backup artifact and
the native source copy. Production deletion requires the explicit approval
noted above.

## Exact artifact contract

Accept exactly three unique regular root ZIP members, with no aliases or extra
members:

| App | Required members |
| --- | --- |
| Radarr | `config.xml`, `radarr.db`, `INFO` |
| Sonarr | `config.xml`, `sonarr.db`, `INFO` |
| Lidarr | `config.xml`, `lidarr.db`, `INFO` |

`INFO` is UTF-8 with exactly two lines and a final newline:

```text
v<exact-application-version>
YYYY-MM-DD HH:MM:SS
```

This shape is written directly by each tagged `BackupService`. Require a root
`<Config>` XML element with exactly one non-empty `<ApiKey>`, but never record or
log the value. Require ZIP CRC, no trailing bytes, supported compression,
bounded size/ratio/member count, safe root names, no duplicate, link, device,
encrypted, nested, absolute, or traversing member, SQLite `quick_check == ok`,
zero foreign-key violations, exact migration maximum, and the conservative
tables below.

### Conservative required tables

These sets cover configuration/integration state, media-catalog linkage, file
inventory, policy, and history without pretending every transient table is a
completion signal. They derive from the exact tagged mappings
([Radarr](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Datastore/TableMapping.cs),
[Sonarr](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/NzbDrone.Core/Datastore/TableMapping.cs),
[Lidarr](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/NzbDrone.Core/Datastore/TableMapping.cs)).

- Radarr: `VersionInfo`, `Config`, `RootFolders`, `Indexers`,
  `DownloadClients`, `Notifications`, `Tags`, `Movies`, `MovieMetadata`,
  `MovieFiles`, `QualityProfiles`, `CustomFormats`, `ImportLists`, and
  `History`.
- Sonarr: `VersionInfo`, `Config`, `RootFolders`, `Indexers`,
  `DownloadClients`, `Notifications`, `Tags`, `Series`, `Episodes`,
  `EpisodeFiles`, `QualityProfiles`, `CustomFormats`, `ImportLists`, and
  `History`.
- Lidarr: `VersionInfo`, `Config`, `RootFolders`, `Indexers`,
  `DownloadClients`, `Notifications`, `Tags`, `Artists`, `ArtistMetadata`,
  `Albums`, `AlbumReleases`, `Tracks`, `TrackFiles`, `QualityProfiles`,
  `MetadataProfiles`, `CustomFormats`, `ImportLists`, and `History`.

The sidecar may contain only structural evidence: application and exact
versions, database backend/migration, command ID, native backup ID/type/time,
member names, artifact bytes/SHA-256, table names/counts, validation result, and
selected OCI identity. Never include API keys, UI credentials, URLs, paths,
provider settings, media titles, database values, or config contents.

## Authoritative control-plane scope

The native SQLite database plus `config.xml` is the authoritative application
control plane for each exact version. It includes users/API authentication,
settings, root folders, profiles, formats, tags, naming, indexers, download
clients, notifications, import lists, remote mappings, scheduled configuration,
the application media catalog, file inventory/linkage, and operational history.

The artifact intentionally excludes media payloads, downloaded/working files,
application binaries and container layers, log database and text logs, caches,
temporary/update directories, provider-side state, download-client queues and
payloads, indexer state, filesystem permissions/ownership, Docker/Compose and
proxy/network state, and externally stored secrets. The database's media paths
and file metadata are retained as control-plane linkage; the movie, TV, and
audio files themselves are not backed up. Root/media/download mounts are never
exposed to the backup worker.

## Exact isolated restore contract

Restore is destructive to its destination and is never permitted against a
production target. Require Homelab Backup's explicit isolated-restore flag, an
exact disposable local-origin allowlist, different source/destination target
IDs, immutable staged artifact size/hash evidence, complete archive
revalidation, and a demonstrably fresh exact-image destination before any
upload.

The exact upload endpoint is
`POST <api>/system/backup/restore/upload`, multipart field `file`. It extracts
the ZIP, immediately replaces `config.xml`, stages the DB as the application's
`.restore` file, and returns `{"restartRequired":true}`. Then call
`POST <api>/system/restart`, require `{"restarting":true}`, and let the
container's restart policy recreate the process. At startup the common database
restoration service deletes old `-shm`, `-wal`, and `-journal` files and the old
DB before moving the staged DB into place
([common tagged restoration service](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Datastore/DatabaseRestorationService.cs)).

The API authentication handler captures the key from `config.xml` when the
process is built. Therefore the old destination key remains valid long enough
to request restart, while the restarted process requires the restored source
key. Extract that already validated key only in memory, never persist or log it,
and poll status with it until the response matches the exact contract and has a
new non-empty `startTime`
([Radarr API-key handler](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Http/Authentication/ApiKeyAuthenticationHandler.cs),
[config provider](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/NzbDrone.Core/Configuration/ConfigFileProvider.cs)).
An upload acknowledgement alone is not recovery evidence.

### Fresh-destination resource lists

Before upload, each endpoint below must return `200` with an empty JSON list.
These resources have no required default rows and jointly prove that the
destination is not an existing configured control plane. They are declared by
the exact generated APIs
([Radarr OpenAPI](https://github.com/Radarr/Radarr/blob/7827e5368947f158ad06f757334f5cde6c406411/src/Radarr.Api.V3/openapi.json),
[Sonarr OpenAPI](https://github.com/Sonarr/Sonarr/blob/4ff1b780010d3d9ec76a4864dce96b6494e9caea/src/Sonarr.Api.V3/openapi.json),
[Lidarr OpenAPI](https://github.com/Lidarr/Lidarr/blob/350860e524029b7fb4165ed14fbcabb11217ada2/src/Lidarr.Api.V1/openapi.json)).

- Radarr: `tag`, `rootfolder`, `indexer`, `downloadclient`, `notification`,
  and `movie`.
- Sonarr: `tag`, `rootfolder`, `indexer`, `downloadclient`, `notification`,
  and `series`.
- Lidarr: `tag`, `rootfolder`, `indexer`, `downloadclient`, `notification`,
  and `artist`.

Quality profiles and other built-in defaults are deliberately not used as
freshness sentinels because a fresh instance legitimately creates them.

After the first successful readiness transition, query the phase marker and
structural state, explicitly restart the destination a second time, require a
second new `startTime`, and prove the same state again. This distinguishes a
one-process staged view from durable recovery.

## Fit with the hardened Servarr core

The current shared core already supplies the required origin lock, strict flat
configuration, read-only-mount verification, exact status/migration gates,
command polling and attribution, spawned bounded copy/validation worker,
transactional artifact/sidecar publication, exact post-publication cleanup,
strict three-member validation, immutable restore identity, fresh-destination
checks, isolated-origin authorization, restored-key transition, and restart
readiness proof.

Implement the trio as declarative adapters with exact version, package version,
migration, one database member, fixed native mount, conservative table set,
fresh resource paths, and `command_result_required = True`. If package version
is not yet a generic core field, deepen the core once with an optional exact
field; do not put protocol logic in the adapters.

Clean breaking changes required:

- Radarr accepts only `radarr.db`, not the upstream-supported historical
  `nzbdrone.db` restore alias.
- Sonarr accepts only `sonarr.db`, not `nzbdrone.db`.
- Lidarr accepts only `lidarr.db` and changes the stale documented/development
  image from `ls29` to the declared `ls38` contract.
- Each schema adds only its exact fixed `backup_directory` and removes any
  configuration shape that would permit HTTP fallback.

Preserving any old member alias, mount-less config, HTTP download, alternate
version/API, or package fallback would be backward compatibility and requires
separate user approval.

## Exact two-clean-round Docker acceptance drill

Run the following matrix separately for Radarr, Sonarr, and Lidarr. Use only
the linux/amd64 manifest digest from the identity table. All containers,
networks, config trees, credentials, source backup directories, and listeners
are synthetic, private, and disposable. Publish no ports and give the backend
runner no internet route, Docker socket, `/config`, media, or download mount.

### Clean round 1

1. Create exact-image source `S1` with private config and automatic container
   restart. Verify image labels and the exact status gate.
2. Mount only `S1`'s `Backups/manual` directory read-only at the adapter's fixed
   `/sources/<app>/backups` path. Prove it is a distinct read-only mount.
3. Create tag marker `<app>-round1-A` through the supported tag API. Run
   `test()` and backup A1.
4. Wait across the native whole-second filename boundary, create tag marker
   `<app>-round1-B`, and backup B1.
5. Prove A1 and B1 have different command IDs, native IDs/names/times,
   artifact sizes and SHA-256 hashes; validate each ZIP, SQLite snapshot,
   migration/table set, and secret-safe sidecar independently; prove each exact
   native source file was removed only after publication.
6. Create fresh destination `D1`, prove every fresh-resource list is empty,
   restore A1 through `RestoreService`, and require only marker A. Require exact
   new-process readiness, then a second restart and the same marker state.
7. Destroy `D1`. Create unrelated fresh destination `D2`, restore B1, and
   require markers A and B. Repeat readiness and second-restart persistence.
8. Destroy `D2`, `S1`, and every round-1 resource; assert absence.

### Clean round 2

Repeat the entire sequence from empty state with new source `S2`, credentials,
config tree, backup bind, and markers `<app>-round2-A/B`. Restore A2 into fresh
`D3` and B2 into fresh `D4`; neither destination nor config directory may be
reused from round 1 or from the other phase. Assert full teardown again.

The required total per application is four online backups and four independent
fresh restores across two clean rounds. Each A restore proves only A; each B
restore proves A+B. All four restores require exact version/package/database/
migration status, new `startTime`, phase-correct tags, conservative structural
table/config evidence, and persistence after a second restart. The three
applications therefore produce twelve backups and twelve fresh restores in the
complete trio drill.

Run the common malformed/status/command/mount/archive/cleanup/isolation negative
matrix once per application against the same exact images. It must include
wrong app/package/version/database/migration, unsuccessful/terminal/timed-out
command, ambiguous or unsafe attribution, absent/writable/swapped mount,
changed source file, corrupt/oversized/trailing/extra/wrong-member ZIP,
config-only/PostgreSQL backup, failed cleanup preserving both copies,
unauthorized/source-equals-destination restore, non-fresh destination, wrong
restored key, no restart transition, and state loss after the second restart.

## STOP conditions

Stop rather than weakening this contract if:

- any selected manifest reports a different architecture, label, app/package
  version, API prefix, database type/member, migration, command result, archive
  member set, or restore behavior;
- a production backup would require downtime, `/config`, Docker socket, SSH,
  root-host, media/download access, a UI cookie, or an HTTP download fallback;
- the user does not approve the broad application API key, narrow read-only
  bind, and exact native cleanup for later production activation;
- the bind is absent, writable, not a mount, broader than `Backups/manual`, or
  cannot be correlated safely to the API basename;
- an exact native backup cannot be attributed uniquely or safely removed only
  after publication;
- the artifact is config-only, PostgreSQL, incomplete, unsafe, unbounded,
  inconsistent, or secret-leaking;
- the destination is not demonstrably fresh, isolated, disposable, and local,
  or source/destination identity can overlap;
- upload, restored-key restart/readiness, phase marker boundary, or
  second-restart persistence fails; or
- supporting another database alias, version, tag, API, credential, mount, or
  protocol would add unapproved backward compatibility.
