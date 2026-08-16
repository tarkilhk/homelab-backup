# Readarr and Prowlarr native backup/restore research

Research date: 2026-08-16

Scope: the Readarr and Prowlarr deployments declared in `homelab-infra`, the
currently resolved public container manifests, Readarr 0.4.18.2805, and
Prowlarr 2.4.0.5397. No production host, endpoint, container, configuration,
credential, or data was contacted. The only registry operations were read-only
manifest/config lookups.

An additional disposable local-image probe used the pinned manifests on an
isolated Docker network. It confirmed Readarr migration 158, Prowlarr migration
44, lowercase `config.xml`, the command-result difference described below, and
Forms-login redirects for `/backup/manual/...` despite a valid `X-Api-Key`. All
probe containers, networks, config directories, credentials, and native archives
were removed afterward.

## Decision summary

Both applications have a native online SQLite backup and upload/restart restore
protocol. Readarr can become a thin `ServarrPlugin` subclass after the shared
Servarr validator is made version-aware and strict. Prowlarr can use the same
deep module after one additional shared-core seam: its exact command resource
does **not** expose Readarr's `result` field, so successful completion must be
defined by `status == "completed"` for Prowlarr rather than by both status and
result.

Neither plugin is ready for production activation yet:

1. Both infrastructure image selectors are mutable. Git does not prove which
   bytes are running in production, and production inspection is forbidden.
   The implementation and drill must select immutable linux/amd64 digests.
2. Native manual backups are never covered by upstream retention. The current
   shared flow leaves every source backup behind. Deleting the one uniquely
   attributed source backup is a separate production write and requires an
   explicit product/permission decision; otherwise the native backup directory
   grows without bound.
3. `/backup/...` is a UI/static-file route, not an API controller. An exact
   local probe proved that it redirects to Forms login even with a valid
   `X-Api-Key`. The selected contract therefore uses a narrowly scoped read-only
   native-backup-folder mount and forbids UI-cookie support.
4. The native routine only adds the database for SQLite. `test()` and every
   backup must require `databaseType == "sqlite"` and reject a config-only ZIP.

Once those gates are resolved, both capabilities are honestly `automatic` for
their application control planes: the plugin validates a native artifact,
uploads it to an isolated destination, requests a restart, authenticates with
the API key restored from `config.xml`, and waits for a new ready process.
Readarr book files and Prowlarr's external indexers, download clients, and
connected applications remain separate recovery prerequisites.

## Exact declared topology and image provenance

The inspected `homelab-infra` revision is
`eeed77a76fbc23db3da8470011535ad64cf0bc75`.

| Property | Readarr | Prowlarr |
| --- | --- | --- |
| Declaration | [`docker.compose/media/books/books.yaml`](../../../homelab-infra/docker.compose/media/books/books.yaml) | [`docker.compose/media/download/download.yaml`](../../../homelab-infra/docker.compose/media/download/download.yaml) |
| Declared image | `ghcr.io/home-operations/readarr:rolling` | `ghcr.io/linuxserver/prowlarr:2.4.0-develop` |
| Container / host port | `readarr`, `8787:8787` | `prowlarr`, `9696:9696` |
| Memory / restart | 256 MiB, `unless-stopped` | 256 MiB, `unless-stopped` |
| Identity | explicit `user: 0:0` | LinuxServer `PUID=0`, `PGID=0` |
| Persistent control plane | `/docker-apps/readarr/config:/config` | `/docker-apps/prowlarr/config:/config` |
| Other mounts | eBooks at `/eBooks`; downloads at `/Download` | none |
| LAN routing | `readarr.hollinger.asia` to Docker host port 8787 | `prowlarr.hollinger.asia` to Docker host port 9696 |

The declarations establish topology, not live state. In particular, neither
image is digest-pinned. Home Operations explicitly documents that even its
versioned tags are mutable and that only a digest-pinned reference is immutable
([container policy](https://github.com/home-operations/containers#tag-immutability)).
LinuxServer likewise describes `develop` as a development channel
([Prowlarr image documentation](https://docs.linuxserver.io/images/docker-prowlarr/)).

Read-only registry resolution on 2026-08-16 produced:

| Image | OCI index | linux/amd64 manifest | Embedded version/provenance |
| --- | --- | --- | --- |
| Readarr | `sha256:8f7551205fbdccd526db23a38a6fba18b0f40726e63bb89be0fb2333ff4ee4cd` | `sha256:440dc56b904d7363468c1b19e60ccd9dd18b69bdccdb9712d5718779cc48d279` | `0.4.18.2805`; Home Operations build commit [`9dc16d9`](https://github.com/home-operations/containers/commit/9dc16d9042bcbb3ed55716e3344bb1073b367401); exact upstream tag commit [`7cc02f9`](https://github.com/Readarr/Readarr/commit/7cc02f95afaabebfe515dd36384387d9d02e31c5) |
| Prowlarr | `sha256:2ebc057c64eaea0fe07f57e0b3f3f67f63fd99ec717d9aaed326a9532144c5c4` | `sha256:a82572d17330327d1efd3d2242eac03b95402607dc96f620447a8426be2f7bd1` | `2.4.0.5397-ls265`; LinuxServer build commit [`ecb0729`](https://github.com/linuxserver/docker-prowlarr/commit/ecb0729422871fc4719f59df8f4fb94c12552305); exact upstream tag commit [`d6e8466`](https://github.com/Prowlarr/Prowlarr/commit/d6e8466d3ee32915d35476b9c225453984992697) |

These are reproducible implementation candidates, **not proof of the live
production digests**. If the project chooses different immutable digests, the
source contract and drill must be re-pinned to the versions reported by those
images.

Readarr is retired and its repository is archived. Upstream explicitly says it
will receive only a brief transition support window and encourages migration
to alternatives
([retirement announcement](https://github.com/Readarr/Readarr#announcement-retirement-of-readarr)).
That does not prevent backing up the currently deployed instance, but it makes
version drift a stop-and-research event rather than a compatibility promise.

## Exact common native protocol

The two tagged implementations share the same recovery shape.

### Connectivity and version gate

Send API credentials only in `X-Api-Key`; the exact authentication handlers
also accept query and bearer forms, but query credentials are prone to URL
logging and are unnecessary
([Readarr handler](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Http/Authentication/ApiKeyAuthenticationHandler.cs),
[Prowlarr handler](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Http/Authentication/ApiKeyAuthenticationHandler.cs)).

`test()` is non-destructive:

- `GET <base_url>/api/v1/system/status`;
- require the exact expected `appName` and version;
- require `databaseType` to be SQLite;
- require migration 158 for Readarr 0.4.18.2805 or 44 for Prowlarr 2.4.0.5397;
- `GET <base_url>/api/v1/system/backup` and validate the list shape; and
- require the exact configured backup directory to be a genuine read-only mount;
  and, when a harmless entry exists, verify its basename maps to one regular,
  non-symlink file directly inside that mount. Absence is not a reason to mutate
  in `test()`.

The status resource owns the version, database type, migration, URL base,
start time, and package fields
([Readarr status controller](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Api.V1/System/SystemController.cs),
[Prowlarr status controller](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Api.V1/System/SystemController.cs)).

### Backup state machine

Under the shared target/origin lock:

1. Reconfirm exact status and SQLite mode. List
   `GET /api/v1/system/backup` and record every `(id, type, path, size, time)`.
2. Send exactly one `POST /api/v1/command` with `{"name":"Backup"}` and
   require a numeric command id.
3. Poll `GET /api/v1/command/{id}` to a fixed deadline. Readarr requires
   `status == "completed"` and `result == "successful"`; its command resource
   exposes both fields
   ([controller](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Api.V1/Commands/CommandController.cs),
   [resource](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Api.V1/Commands/CommandResource.cs)).
   Prowlarr requires `status == "completed"`; its exact resource has no result
   field, while failure states are represented by `failed`, `aborted`,
   `cancelled`, or `orphaned`
   ([controller](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Api.V1/Commands/CommandController.cs),
   [resource](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Api.V1/Commands/CommandResource.cs)).
4. Poll the backup list until exactly one new `manual` identity appears. Reject
   ambiguity, missing/unsafe paths, mismatched names, and entries older than the
   run boundary.
5. Validate the returned relative `/backup/manual/...` path, then map its exact
   basename to one regular non-symlink file in the fixed read-only native-backup
   mount. The exact local probe showed the static mapper redirects to Forms login
   even with `X-Api-Key`, so HTTP download and UI-cookie credentials are forbidden.
   The mapper's underlying source-directory behavior is documented in
   ([Readarr mapper](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Http/Frontend/Mappers/BackupFileMapper.cs),
   [Prowlarr mapper](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Http/Frontend/Mappers/BackupFileMapper.cs)).
6. Stream the stable local file into `create_backup_artifact()` with hard time
   and byte ceilings, validate
   before publication, and let the helper atomically publish the artifact and
   sidecar.

The upstream services use SQLite's online backup API with journal mode
`truncate`, so the source application need not stop
([Readarr snapshot implementation](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/NzbDrone.Core/Backup/MakeDatabaseBackup.cs),
[Prowlarr snapshot implementation](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/NzbDrone.Core/Backup/MakeDatabaseBackup.cs)).
They copy `config.xml`, snapshot the SQLite database, add `INFO`, and ZIP the
three files. PostgreSQL mode deliberately skips the database
([Readarr backup service](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/NzbDrone.Core/Backup/BackupService.cs),
[Prowlarr backup service](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/NzbDrone.Core/Backup/BackupService.cs)).

The official Servarr documentation presents “Backup Now”, ZIP download, ZIP
upload, and restart restore as the supported workflows
([Readarr System](https://github.com/Servarr/Wiki/blob/master/readarr/system.md#backup),
[Readarr FAQ](https://github.com/Servarr/Wiki/blob/master/readarr/faq.md#how-do-i-backuprestore-my-readarr),
[Prowlarr System](https://github.com/Servarr/Wiki/blob/master/prowlarr/system.md#backup),
[Prowlarr FAQ](https://github.com/Servarr/Wiki/blob/master/prowlarr/faq.md#how-do-i-backuprestore-prowlarr)).

### Native retention warning

API-triggered commands have a manual trigger, and `BackupCommand` therefore
creates a `manual` backup. Both exact `BackupService` implementations skip
`CleanupOldBackups()` for manual backups. The plugin cannot honestly claim
bounded production behavior while leaving one permanent native ZIP per run.

Preferred resolution: after the artifact has been fully copied, validated,
and atomically published, delete **only** the exact newly attributed native
entry through `DELETE /api/v1/system/backup/{id}`. This is recoverable from the
Homelab Backup artifact but is still an additional production mutation; obtain
explicit approval before activating it in production. If approval is withheld, STOP and
require an operator-owned cleanup/retention mechanism before scheduling the
target.

## Per-application contract

### Readarr 0.4.18.2805

The subclass surface is:

```python
class ReadarrPlugin(ServarrPlugin):
    app_name = "Readarr"
    api_prefix = "/api/v1"
    database_members = ("readarr.db",)
    expected_migration = 158
    native_backup_directory = "/sources/readarr/backups"
    restore_capability = "automatic"
```

Do not add `nzbdrone.db` as a legacy alias without explicit compatibility
approval. The exact native producer and restore service use `readarr.db`.

The artifact must be a private, bounded ZIP containing exactly these three
regular root members, case-insensitively unique:

- `config.xml`;
- `readarr.db`; and
- `INFO`, whose first line is `v0.4.18.2805`.

`config.xml` contains the API key used after restore and may contain other
security settings. `readarr.db` owns authors, author metadata, books, editions,
book-file records, series links, root folders, quality and metadata profiles,
custom formats, naming, indexers, import lists, download clients, notifications,
history, blocklist, tags, mappings, users, and configuration
([exact table mapping](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/NzbDrone.Core/Datastore/TableMapping.cs)).
The `/eBooks` payload and `/Download` working tree are not in the native ZIP.

### Prowlarr 2.4.0.5397

After adding a shared completion-policy seam, the subclass surface is:

```python
class ProwlarrPlugin(ServarrPlugin):
    app_name = "Prowlarr"
    api_prefix = "/api/v1"
    database_members = ("prowlarr.db",)
    expected_migration = 44
    native_backup_directory = "/sources/prowlarr/backups"
    command_result_required = False
    restore_capability = "automatic"
```

The exact restore service also recognizes historical `nzbdrone.db`, but the
exact native producer writes `prowlarr.db`. Do not preserve that legacy alias
without the user's explicit compatibility approval.

The artifact must contain exactly:

- `config.xml`;
- `prowlarr.db`; and
- `INFO`, whose first line is `v2.4.0.5397`.

The database owns indexers and their settings/cookies, applications and
application-indexer mappings, app sync profiles, download clients, proxies,
notifications, history, tags, users, configuration, status, and definition
versions
([exact table mapping](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/NzbDrone.Core/Datastore/TableMapping.cs)).
External tracker/Usenet services, FlareSolverr/proxies, download clients, and
connected Servarr applications are dependencies rather than artifact payload.

## Shared artifact validation and sidecar

The current Servarr core's `config.xml` parse plus SQLite `quick_check` is a
useful baseline, but the new exact contracts should first deepen the shared
module rather than duplicating code in subclasses:

- require exactly three root regular files with the expected names; reject
  duplicate, nested, absolute, traversal, symlink/device, encrypted, extra, or
  unsupported-compression members;
- bound archive bytes, member count, compressed/uncompressed bytes, expansion
  ratio, XML bytes, database bytes, and streamed copy duration before
  resource exhaustion;
- require ZIP CRC success and no trailing data;
- safely parse `config.xml`, require a `Config` root and one non-empty `ApiKey`,
  but never return or log values;
- require the exact `INFO` version and a parseable timestamp;
- open the database read-only, require `PRAGMA quick_check == ok`, an empty
  `PRAGMA foreign_key_check`, no hot journal/WAL members, and exact latest
  `VersionInfo` migration 158 (Readarr) or 44 (Prowlarr);
- require a conservative set of application tables from the mappings above;
  empty tables are valid on a fresh instance, but missing tables are not; and
- perform final file identity/size/hash checks before publication.

The sidecar may include application/version, source package/digest selected by
the drill, database backend and migration, member names, artifact byte count and
SHA-256, table names/counts, command id, source backup id/type/time, and
validation outcome. It must not include API keys, UI credentials, usernames,
provider settings, indexer URLs, cookies, media/book paths, download-client
settings, database rows, or `config.xml` values.

## Restore contract and safety

For a validated ZIP, the exact APIs are:

1. `GET /api/v1/system/status` with the disposable destination API key; record
   `startTime`.
2. `POST /api/v1/system/backup/restore/upload` as multipart field `file`;
   require `{"restartRequired": true}`.
3. `POST /api/v1/system/restart`; require `{"restarting": true}`.
4. Extract the restored API key from the already-validated `config.xml` in
   memory, then poll status with that key until a different non-empty
   `startTime` and exact version are ready.

The tagged restore controllers stage the upload, hand `config.xml` and the
expected database to the restoration service, and return the restart-required
flag
([Readarr controller](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/Readarr.Api.V1/System/Backup/BackupController.cs),
[Prowlarr controller](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/Prowlarr.Api.V1/System/Backup/BackupController.cs)).
At next process start, database restoration removes old `-shm`, `-wal`, and
`-journal` files and replaces the database
([Readarr restoration service](https://github.com/Readarr/Readarr/blob/v0.4.18.2805/src/NzbDrone.Core/Datastore/DatabaseRestorationService.cs),
[Prowlarr restoration service](https://github.com/Prowlarr/Prowlarr/blob/v2.4.0.5397/src/NzbDrone.Core/Datastore/DatabaseRestorationService.cs)).

Restore is destructive to the destination. It is permitted only through the
repository's isolated local restore workflow and never against a production
URL, container, or volume. The exact image must run with an automatic container
restart policy during the drill because the API restart exits/restarts the app
process. A source target must never be eligible as a restore destination.

The selected runtime guard requires both the explicit isolated-restore flag and
an exact allowlisted disposable destination origin. This works on the internal
Docker drill network without granting production restore authority; normal
backend deployments configure neither value.

## Least production access

For backup-only production use, each target needs:

- network access from Homelab Backup to the single Readarr or Prowlarr origin;
- one application API key, sent only as `X-Api-Key`;
- one narrow read-only mount of that application's native backup directory,
  defaulting to `/sources/readarr/backups` or `/sources/prowlarr/backups`; and
- no Docker socket, no `/config` mount, no media/download mount, no host root,
  and no SSH access.

The application API key is broad; upstream provides no backup-scoped token.
Schema secrets must use password widgets/storage and every exception/log path
must redact them. The selected read-only mount avoids storing UI credentials and
must expose only the native backup directory, never `/config`.

## Exact local acceptance drill

Run the same finite matrix separately for Readarr and Prowlarr. All containers,
networks, volumes, ports, credentials, and data are disposable and local to the
dev VM. Reference images by the selected linux/amd64 manifest digest, never by
`rolling` or `2.4.0-develop`.

1. Start a source container with a fresh private config directory, the exact
   infrastructure identity/restart behavior, and only local fixture payload
   mounts that the application requires. Mount its native backup directory
   read-only into the backend runner. Assert OCI labels and
   `/api/v1/system/status` match the contract and SQLite migration.
2. Seed marker A through the application's supported API (a uniquely named tag
   is sufficient and requires no external service). Run `test()` and backup 1.
3. Seed marker B, run backup 2, and prove distinct command ids, native backup
   identities, artifact hashes, timestamps, and sidecars. Validate both ZIPs
   independently. Exercise failed command, timeout, ambiguous list, unsafe
   path, cross-origin redirect, oversized/truncated/corrupt ZIP, wrong member,
   wrong INFO version, wrong migration, and PostgreSQL/config-only negatives
   with deterministic mocks.
4. Create fresh destination 1 with a separate empty private config directory
   and sentinel/allowlist accepted by `RestoreService`. Restore artifact 1,
   allow only that exact container to restart, wait for a new start time, and
   prove marker A exists and marker B does not.
5. Destroy destination 1. Create fresh destination 2 and restore artifact 2.
   Prove both markers exist and the expected structural table/config evidence
   matches. Destroy destination 2.
6. Repeat both backup captures and both fresh-destination restores in a second
   independent drill run to detect stale native-backup attribution or accidental
   destination reuse. Assert teardown leaves no drill containers, networks,
   volumes, bind directories, or listeners.

This is two online backups and two fresh restores **per application per drill
run**, with no production contact. The source may remain live throughout native
backup. The two destinations must never share a config directory.

## STOP conditions

Stop implementation or activation and re-research rather than guessing if any
of these occur:

- the chosen immutable manifest reports a different app/package version,
  architecture, database type, or migration;
- infrastructure remains floating and no immutable implementation/drill digest
  is selected;
- the native backup is config-only, lacks one exact member, has extra/unsafe
  members, fails strict ZIP/SQLite/XML/INFO validation, or changes while read;
- backup command completion/list behavior differs from the exact state machine,
  including Prowlarr unexpectedly requiring or returning a result contract;
- the configured native-backup mount is absent, writable, not a mount, broader
  than the backup folder, or fails safe API-basename correlation;
- no approved bounded policy exists for the accumulating native manual backups;
- the destination is not demonstrably disposable/local, source and destination
  identities overlap, or a restore could reach production;
- a fresh exact-image destination does not accept the native ZIP, restart, use
  the restored API key, report a new start time, or preserve the correct marker
  boundary; or
- accepting `nzbdrone.db`, another image line, schema migration, API prefix,
  fallback credential, or archive shape would introduce unapproved backward
  compatibility.

## Implementation handoff

1. Deepen `ServarrPlugin` once with strict bounded artifact/version/database
   validation, explicit command-completion policy, read-only source strategy, and
   native-source cleanup policy.
2. Add Readarr as the thin exact contract above and prove its full drill.
3. Add Prowlarr as the thin exact contract above and prove its full drill.
4. Only after both local drills pass, prepare production targets. Production
   may trigger backups, but production restore testing is forbidden.
