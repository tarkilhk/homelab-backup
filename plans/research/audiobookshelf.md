# Audiobookshelf 2.36.0 backup and restore research

Research date: 2026-08-15

Scope: the deployment declared in `homelab-infra`, exact upstream
Audiobookshelf v2.36.0 source, and first-party Audiobookshelf/SQLite
documentation. Audiobook/ebook payload is explicitly excluded and remains
under the separate media data-protection policy. No production endpoint or
host was contacted and no production state was changed.

## Decision summary

Protect Audiobookshelf's authoritative control-plane state with an online
SQLite snapshot plus the native `metadata/items` and `metadata/authors` trees,
read through two dedicated read-only bind mounts. Package the result in the
application's `.audiobookshelf` ZIP shape, validate it more strictly than the
native implementation, and publish it atomically through Homelab Backup.

This needs no Audiobookshelf credential, network attachment, downtime, or
production-side backup file. It covers users, password hashes, JWT/API/session
state, server and library configuration, catalog metadata, listening progress,
bookmarks, collections, playlists, shares, feeds, playback history, covers
stored under `/metadata`, and author images.

The SQLite snapshot is transactionally consistent online. The database and
metadata files do not share one atomic snapshot, so this contract guarantees a
**usable, referentially complete** artifact through before/after manifests and
content validation, not one exact instant for cover/metadata presentation. If
an exact cross-component point-in-time boundary is required, stop: the app must
be quiesced or both host directories must be captured by one atomic filesystem
snapshot.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/media/books/books.yaml:40-55`:

| Property | Declared value |
| --- | --- |
| Image | `ghcr.io/advplyr/audiobookshelf:2.36.0` |
| Container | `audiobookshelf`, running as `0:0`, 256 MiB limit |
| Network | the books fragment's `default_network` inside the Portainer `media` stack |
| Published port | host 13378 to container 80 |
| Media | `/mnt/nas-media/eBooks/audiobooks:/audiobooks` |
| Metadata | `/docker-apps/audiobookshelf/metadata:/metadata` |
| Config | `/docker-apps/audiobookshelf/config:/config` |

The tag resolves to upstream release commit
[`96d4021a3cd45f67bf374b65abafbe5d73e926b5`](https://github.com/advplyr/audiobookshelf/commit/96d4021a3cd45f67bf374b65abafbe5d73e926b5)
([v2.36.0 release](https://github.com/advplyr/audiobookshelf/releases/tag/v2.36.0)).
On 2026-08-15, public GHCR metadata resolved the tag to OCI index
`sha256:180acad33d69c99ed208676465d8edcb268fa46967735579a7810859885b1a8e`
and Linux/amd64 manifest
`sha256:e388e90e381ae3fa8660346612b2955f2c555ede81c9c286e2218bdf966b4de8`.
The infrastructure declaration is tag-only, so these values identify the
reproducible drill input, not the historical bytes already pulled by a host.
Pin the multi-architecture digest in the later infrastructure change.

Audiobookshelf's Docker documentation confirms that `/config` contains the
SQLite database and migrations, `/metadata` contains metadata, cover/author
images, logs and backups, and the SQLite config directory must be local rather
than on a network filesystem
([official Docker documentation](https://audiobookshelf.org/docs/documentation/install/docker/#mount-points)).
The deployed config and metadata binds are local `/docker-apps` paths; only the
excluded media bind is on the NAS.

## State boundary

### Authoritative included state

The exact database path is `/config/absdatabase.sqlite`
([`Database.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/Database.js#L165-L194)).
Its model set includes users, sessions, API keys, libraries and folders, books,
podcasts/episodes, library items, progress, series/authors, collections,
playlists, devices, playback sessions, feeds, settings, custom metadata
providers, and media shares
([`Database.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/Database.js#L323-L349)).

Notable secret-bearing state is also in the DB. Unless `JWT_SECRET_KEY` is
externally supplied, the server generates and stores its JWT signing secret in
server settings
([`TokenManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/auth/TokenManager.js#L38-L56)).
The inspected deployment does not declare that override, so the database is
self-contained for token continuity. Never print settings, user rows, password
hashes, tokens, session values, OIDC secrets or API keys.

Include only these metadata subtrees, matching the native format:

- `/metadata/items`: covers stored outside media folders and item metadata
  files;
- `/metadata/authors`: author images.

Exact source sets those native roots
([`BackupManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/BackupManager.js#L20-L28))
and archives only the SQLite snapshot, those two trees, and a `details` member
([`BackupManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/BackupManager.js#L431-L502)).

### Explicitly excluded

- `/audiobooks` and every audio/ebook/media byte. The official backup page
  states native backups do not include media or covers stored with library
  items
  ([official backup documentation](https://audiobookshelf.org/docs/documentation/server-management/backups/)).
- Covers and metadata configured to live beside media are part of that external
  media policy, not this plugin. The database references remain in the artifact
  so those external paths reattach when the media tree is restored separately.
- `/metadata/cache`, logs, native `backups`, temporary/transcode data and
  generated thumbnails are cache, operations history, or derived state.
- `/config/migrations` is version support material, not application data. The
  exact-version image owns the migration source; v2.36.0 records its database
  version in `migrationsMeta`
  ([`MigrationManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/MigrationManager.js#L32-L69)).
- Compose, proxy and environment configuration are infrastructure-as-code.

## Native backup and restore assessment

Audiobookshelf has a first-party online backup manager and `.audiobookshelf`
ZIP format. It uses SQLite's online backup API to create a standalone
`absdatabase.sqlite`, with a bounded roughly two-minute wait, then streams the
two metadata trees into the ZIP
([`BackupManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/BackupManager.js#L333-L390),
[`BackupManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/BackupManager.js#L404-L429)).
SQLite guarantees the online backup result is a consistent database snapshot
even while the source is active
([SQLite Online Backup API](https://www.sqlite.org/backup.html)).

Native backup is supported, but triggering/downloading it through the API is
not the selected production mechanism:

- every `/api/backups` route is guarded only by `isAdminOrUp`
  ([`ApiRouter.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/routers/ApiRouter.js#L197-L206),
  [`BackupController.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/controllers/BackupController.js#L158-L177));
- an API key authenticates as its associated user and its per-key permission
  object is not applied by that authentication path
  ([`TokenManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/auth/TokenManager.js#L279-L307));
- the same credential can reach the destructive `GET /api/backups/:id/apply`
  endpoint, whose exact implementation disconnects the DB, removes the live
  database, replaces it, extracts metadata into live directories, and
  reconnects
  ([`BackupManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/BackupManager.js#L200-L267)); and
- triggering a native backup writes an additional production copy under the
  configured backup path and its retention can delete older native copies.

The native archive remains the interoperability target, but a pair of
read-only mounts is materially narrower than a full-admin credential. Do not
add an API fallback without explicit approval.

The native restore endpoint is never called by this plugin. Official manual
restore instructions require stopping Audiobookshelf and replacing its DB and
metadata trees
([official backup documentation](https://audiobookshelf.org/docs/documentation/server-management/backups/#restoring-backups-for-23x-and-newer)).
They refer to an older database filename in one paragraph; exact v2.36.0 source
and archives use `absdatabase.sqlite`, which is authoritative for this contract.

## Online consistency and selected backup algorithm

The DB snapshot is exact, but `metadata/items` and `metadata/authors` are added
after it without an outer transaction or filesystem snapshot. This matters:
cover upload/download writes the file before saving its DB path
([`CoverManager.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/managers/CoverManager.js#L88-L123),
[`LibraryItemController.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/controllers/LibraryItemController.js#L295-L331)),
while author-image deletion removes the file before clearing the DB path
([`AuthorController.js`](https://github.com/advplyr/audiobookshelf/blob/96d4021a3cd45f67bf374b65abafbe5d73e926b5/server/controllers/AuthorController.js#L306-L328)).
The native algorithm is consequently not one atomic composite point in time.

The plugin should reproduce the supported format with stricter fail-closed
validation:

1. Open `/sources/audiobookshelf/config/absdatabase.sqlite` using SQLite URI
   `mode=ro`; use `sqlite3.Connection.backup()` to a new private local file.
   Never copy the live DB/WAL/SHM files directly.
2. Require `PRAGMA quick_check = ok`, an empty `PRAGMA foreign_key_check`, the
   complete required table/column set, a root user, and `migrationsMeta.version
   = 2.36.0` in the snapshot.
3. Derive the DB reference manifest for item/podcast covers and author images.
   Classify references under `/metadata/items` or `/metadata/authors` as
   included, references under `/audiobooks` as explicitly external, and reject
   unexpected absolute roots.
4. Snapshot sorted source metadata entries (relative path, regular-file type,
   size and timestamps), copy only the two native trees into private staging,
   then snapshot the source entries again. Retry a bounded number of times if
   the trees changed; fail if they never stabilize.
5. Require every DB-referenced included file to exist in staging, be non-empty,
   remain under its expected root, and decode as a supported image. Parse every
   staged `metadata.json`; reject malformed JSON. Hash every staged file.
6. Take a second read-only SQLite snapshot and require the relevant reference
   rows and database version to match the first. A mismatch retries the whole
   operation, never just the file copy.
7. Create one bounded, safe `.audiobookshelf` ZIP containing exactly
   `absdatabase.sqlite`, `metadata-items/`, `metadata-authors/`, and a versioned
   `details`/Homelab Backup manifest. Validate the completed ZIP independently.
8. Publish mode 0600 through `create_backup_artifact()` only after validation;
   let the normal sidecar record producer, target, timestamp, size and SHA-256.

This proves that the artifact's DB references resolve and its copied content is
well-formed. It cannot prove that a cover's bytes represent precisely the same
instant as the DB because the DB stores paths, not content hashes. That does not
prevent a usable restore: an old or new valid image at the same referenced path
is presentation data. If that distinction is unacceptable, require quiescence
and stop rather than claim stronger online semantics.

The artifact is secret-bearing. Never log archive members' content, DB values,
usernames, paths containing personal titles, tokens, hashes stored in the DB,
or image bytes.

## Minimum production integration

Add only these read-only backend mounts during the later infrastructure change:

```text
/docker-apps/audiobookshelf/config:/sources/audiobookshelf/config:ro
/docker-apps/audiobookshelf/metadata:/sources/audiobookshelf/metadata:ro
```

The plugin schema needs only fixed absolute `config_path` and `metadata_path`
values (plus bounded retry/timeout controls if the canonical plugin schema
allows them). It needs no Audiobookshelf URL, username, API key, network,
Docker socket, host root mount, media mount, or writable source access.

`test()` is non-destructive: validate both paths and their containment, open the
DB read-only, check exact schema/version and integrity, and verify the included
metadata roots are readable. Do not create a source-side probe file.

## Create-only isolated restore contract

Declare `restore_capability = "partial"`. The plugin can safely materialize and
validate the control-plane state, but exact app boot and the separately managed
media payload remain external verification. Production restores are forbidden.

Restore must:

1. Stage and inspect the whole artifact before destination mutation. Enforce a
   member allowlist, safe relative names, no duplicate members, links/devices or
   traversal, bounded member/count/total/compression ratio, exact `details`
   version, and sidecar size/hash.
2. Extract the DB privately and require exact v2.36.0 migration metadata,
   required schema, `quick_check`, `foreign_key_check`, root user, and the
   database-to-metadata reference contract.
3. Require two new empty destinations, one for `/config` and one for
   `/metadata`, each carrying a fixed versioned restore sentinel. Refuse
   symlinks, existing DB/items/authors, source mounts, `/backups`, production
   paths, and any artifact/destination overlap.
4. Stage and fsync all output on the destination filesystems. Publish
   `absdatabase.sqlite`, `items/`, and `authors/` create-only; never merge into
   or clean a pre-existing tree.
5. Reopen and revalidate the published DB and every included reference. Remove
   only files and directories created by this invocation if final validation
   fails.
6. Return `partial` with explicit requirements to mount the independently
   protected media at the same container path and boot/verify the exact 2.36.0
   Docker image in isolation.

Do not use Audiobookshelf's in-place apply endpoint, even for local restore
implementation. The isolated drill may use first-party APIs **after** the fresh
container boots to prove the result, but the restore operation itself remains
filesystem create-only.

## Exact-version two-run disposable Docker drill

Run entirely on the development VM with the Linux/amd64 image digest above, an
internal Docker network, synthetic credentials, fresh local config/metadata
directories, and a small synthetic media fixture that is never included in an
artifact:

1. Start source instance A by digest with `/config`, `/metadata`, and
   `/audiobooks` mapped to disposable local paths. Assert version 2.36.0 and
   initialize a synthetic root user.
2. Through first-party local UI/APIs, create an additional user, library,
   synthetic book, custom metadata/cover and author image, collection,
   playlist, bookmark, playback/listening progress, a completed session, and a
   revocable API key. Record only non-secret expected IDs/counts and fixture
   hashes.
3. Run the real plugin online through read-only source mounts to create artifact
   A. Keep playback/progress writes active during at least one snapshot attempt
   to prove SQLite online behavior and bounded retry handling.
4. Mutate progress/bookmarks, rename metadata, replace a cover, remove/add an
   author image, and change collection/playlist membership. Create distinct
   artifact B through the same real path.
5. Prove neither artifact contains the synthetic audio/ebook fixture or any
   `/metadata/cache`, logs, tmp or native-backups content. Independently verify
   ZIP safety, mode 0600, sidecar SHA-256/size, SQLite integrity/version,
   JSON/images and all included references.
6. Restore A and B into four separate sentinel-marked empty directories (fresh
   config and metadata pair per artifact). Never reuse a destination.
7. Boot two fresh exact-digest containers with those restored pairs and the
   same external synthetic media bind. Require exact version, successful login,
   expected users/settings/library/catalog, correct A-versus-B collections,
   playlists, bookmarks and listening progress, and successful cover/author
   image retrieval.
8. Restart both restored containers and repeat DB, API and image checks to prove
   persistence. Confirm the external media path reattaches but was not restored
   from the plugin artifact.
9. Exercise fail-closed cases: source mutation beyond retry limit, live DB file
   copied without snapshot, corrupt/truncated DB or image, malformed metadata
   JSON, missing referenced image, unsafe/duplicate ZIP member, zip bomb,
   wrong migration version, non-empty destination, missing/wrong sentinel and
   artifact/destination overlap. None may publish success or alter existing
   destination state.
10. Tear down only disposable containers/networks/directories and repeat the
    two-artifact sequence from clean state. No production hostname, mount,
    credential or data is permitted in the drill.

## STOP conditions

Stop rather than weaken the contract if any of these is true:

- `/config` is not a local filesystem or cannot be mounted read-only together
  with its WAL/SHM companions;
- `/metadata/items` and `/metadata/authors` cannot be mounted read-only without
  broad host or media access;
- the source DB is not exact v2.36.0, fails integrity/foreign-key checks, lacks
  its root user, or contains an unexpected schema;
- an included DB reference escapes the declared metadata roots, is missing,
  empty or invalid, or metadata files never stabilize within bounded retries;
- the user requires exact point-in-time DB-plus-metadata content but does not
  approve quiescence or an atomic host snapshot;
- preserving covers/metadata stored beside media is required but that media
  remains excluded from this plugin;
- either exact-version isolated restore run fails login, state, progress,
  reference, restart or persistence verification; or
- only a broad admin API credential, Docker socket, writable source mount, or
  production restore path is available.

Do not silently fall back to a raw live SQLite copy, DB-only backup, media
inclusion, native destructive restore, or full-admin API access.
