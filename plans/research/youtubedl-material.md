# YouTube-DL Material 4.3.2 backup and restore research

Research date: 2026-08-15

Scope: the deployment declared in `homelab-infra`, exact upstream
YouTube-DL Material v4.3.2 source, and first-party MongoDB documentation.
Downloaded audio/video and their thumbnails/info files are explicitly excluded;
they remain under the separate media data-protection policy. No production
endpoint or host was contacted and no production state was changed.

## Decision summary

The control-plane boundary is small but composite:

- MongoDB database `ytdl_material` is authoritative for subscriptions, users,
  password hashes, roles, playlists/categories, download/archive history,
  task schedules, notifications, and file metadata;
- selected files in `/app/appdata` are authoritative for application settings,
  API/integration credentials, cookies, the JWT signing secret and the local-DB
  fallback; and
- `/app/audio`, `/app/video`, `/app/users` and `/app/subscriptions` are media
  payload trees and are excluded. Their `subscription_backup.json` files are
  useful rebuild hints but are derived from Mongo subscription rows, not the
  primary source.

**Do not implement an allegedly consistent online plugin against the current
deployment.** The app's native backup reads collections one after another, and
the deployed MongoDB is a standalone server. Neither produces a single-time
snapshot while the app is writing. A strict artifact therefore needs a brief
YouTube-DL Material quiescence while Mongo is dumped and the selected appdata
files are copied. Homelab Backup currently has no narrow mechanism to quiesce
this one container, so implementation has a STOP gate until the operator
accepts that downtime and chooses an appropriately constrained orchestration
mechanism. Converting Mongo to a replica set is an online alternative for the
database, but it is a larger infrastructure change and still does not make the
Mongo-plus-appdata boundary atomic.

The eventual plugin should declare `restore_capability = "partial"`: it can
create and validate the complete control plane in an isolated destination, but
the excluded media must be reattached separately. Production restore is
forbidden.

## Exact declared deployment

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/misc/ytdl_material/ytdl_material.yaml:1-33` and
`docker.compose/misc/ytdl_material/ytdl_material.env:1-3`:

| Property | Declared value |
| --- | --- |
| Application image | `tzahi12345/youtubedl-material:4.3.2` |
| Database image | `mongo:8.2.6` |
| Application / database | `youtubedl-material` / `ytdl-mongo-db` |
| Limits | 256 MiB each |
| Network | `default_network` in the Portainer `misc` Compose project |
| Exposure | host 8998 to application port 17442; LAN HAProxy exposure |
| Database mode | Mongo URL `mongodb://ytdl-mongo-db:27017`; local DB disabled at startup |
| App state | `/docker-apps/youtubedl-material/appdata:/app/appdata` |
| User/subscription trees | `/docker-apps/youtubedl-material/users:/app/users`, `/docker-apps/youtubedl-material/subscriptions:/app/subscriptions` |
| Media | NAS Audio and Video binds to `/app/audio` and `/app/video` |
| Mongo data | `/docker-apps/youtubedl-material/mongodb:/data/db` |

The application tag is upstream release commit
[`6eadb37532063017c0087d6fd1a8845ba0984f2c`](https://github.com/Tzahi12345/YoutubeDL-Material/commit/6eadb37532063017c0087d6fd1a8845ba0984f2c)
([v4.3.2 release](https://github.com/Tzahi12345/YoutubeDL-Material/releases/tag/v4.3.2)).
On 2026-08-15, public registry metadata resolved the tag to OCI index
`sha256:2f943d584711cb07c3535b518939fabb2ab90fdd7452d9a9938cd05378468ed9`
and Linux/amd64 manifest
`sha256:989af148df5f71ba41a79b4ce71bceb21417c2de7f309ed6d5c2042160dfaa22`.
The Mongo tag resolved to OCI index
`sha256:9690b268c50317b2988a7f84f514a2cdcf4de6836e12c295b48f3a203731fce1`
and Linux/amd64 manifest
`sha256:9c97228da610a4a1a407e34fe790dcdf5a4273c1e84d1c74cd2231b72881ffca`.
Both infrastructure declarations are tag-only, so these digests identify the
reproducible drill input, not bytes proven to be running on a production host.
Pin both OCI indexes in the later infrastructure change.

Upstream v4.3.2's own Compose file uses `mongo:4`, while this deployment uses
8.2.6 ([exact Compose source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/docker-compose.yml#L1-L28)).
The exact app bundles MongoDB Node driver 3.7.3
([lockfile](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/package-lock.json#L2519-L2526)).
Do not replace the declared pair with upstream's older example during a drill;
the purpose is to prove the deployed pair. Failure of that exact pair is a STOP,
not permission to silently test another version.

The runtime digest and active database mode remain unverified because this
research deliberately made no production call.

## State boundary

### Authoritative included state

The app always opens lowdb files `appdata/db.json`, `appdata/users.json` and
`appdata/local_db.json`, while its mutable settings are
`appdata/default.json`
([application setup](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/app.js#L41-L64),
[`config.js`](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/config.js#L8-L27)).
Include this exact allowlist when present:

- `default.json`: settings and integration/API values. It may contain API keys,
  notification tokens/webhooks and LDAP bind credentials; never log its values
  ([default schema](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/config.js#L176-L263));
- `users.json`: legacy/migration state and the JWT signing secret. The app
  generates and persists that secret there when absent
  ([authentication source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/authentication/auth.js#L36-L60));
- `db.json`: migration markers used by exact-version startup;
- `local_db.json`: all logical tables if the app has fallen back to local DB;
- `cookies.txt`, if present: intentionally supplied session cookies required to
  reach age/account-restricted sources. Treat the whole artifact as a secret;
  and
- a manifest identifying which optional files existed, their modes, sizes and
  SHA-256 hashes, without recording secret content.

Mongo uses the fixed database name `ytdl_material` and eleven collections:
`files`, `playlists`, `categories`, `subscriptions`, `downloads`, `users`,
`roles`, `download_queue`, `tasks`, `notifications`, `archives`, plus the test
collection created by this release
([exact table and connection source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L21-L74),
[`client.db`](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L128-L159)).
Mongo is authoritative when active. Notably, subscription rows live there
([subscription query](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/subscriptions.js#L448-L474))
and user rows include password hashes and authorization state
([user shape](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/authentication/auth.js#L434-L448)).
Never print documents, usernames, hashes, URLs, tokens or cookie content.

Source selection must be strict. If Mongo connection retries fail, v4.3.2
silently sets `use_local_db=true`, writes that setting and continues against
`local_db.json`
([fallback source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L95-L125)).
A plugin must inspect the captured `default.json` during the quiesced window:

- active Mongo: require a validated Mongo dump and still include
  `local_db.json` as fallback evidence;
- active local DB: require valid JSON and the exact table set in
  `local_db.json`; record that source explicitly and do not present a Mongo-only
  dump as success; or
- ambiguous/mode-changing capture: fail and retry only after the source is
  stable.

### Explicitly excluded or reproducible

- `/app/audio` and `/app/video`: downloaded media payload.
- `/app/users`: per-user audio, video and subscription media. Exact source
  constructs those roots from the user and subscription rows
  ([directory mapping](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L175-L241)).
- `/app/subscriptions`: single-user subscription media, thumbnails, info JSON,
  chat/NFO files and temporary archive files.
- `subscription_backup.json` beneath the last two trees: a derived recovery
  hint written after the Mongo subscription operation, and used by the native
  rebuild task
  ([write path](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/subscriptions.js#L489-L495),
  [rebuild reader](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/tasks.js#L280-L341)).
  Do not mount or traverse the media trees merely to collect this redundant
  file.
- `appdata/logs`, generated ZIPs and `appdata/db_backup`: operations history or
  previous backups, not live state. Do not recursively archive `appdata`.
- Mongo `/data/db`: never copy live database files. Compose/proxy/env
  declarations are infrastructure-as-code.

The Mongo `files` collection is included even though its referenced media is
not. It preserves download/archive history and metadata, but those references
will be external/missing until media is reattached. Record counts by
classification in the artifact manifest; do not log individual paths or
titles.

## Native backup and restore assessment

The v4.3 release introduced native Backup/Restore DB, and the official release
notes say Mongo is serialized to a local JSON file
([v4.3 release](https://github.com/Tzahi12345/YoutubeDL-Material/releases/tag/v4.3)).
In exact v4.3.2, `backupDB()` loops through the collection list sequentially and
writes `appdata/db_backup/remote_db.json.<timestamp>.bak`
([source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L709-L726)).
It does not include settings, cookies or the JWT secret and has no transaction
or cross-collection snapshot. Its restore clears every collection before
inserting them one at a time
([source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/db.js#L728-L752)).
That in-place restore must never be called by Homelab Backup.

The task can be triggered through `POST /api/runTask`, but the API uses one
global query-string API key before routing and does not provide a backup-only
scope
([authentication middleware](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/app.js#L692-L715),
[`runTask`](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/app.js#L1803-L1812)).
The same credential reaches configuration changes and the destructive native
restore route
([restore route](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/app.js#L1852-L1881)).
Therefore do not use or add this credential for the plugin.

The release's updater also has a "non-video/audio" ZIP, but it globs the
application working tree, is intended only before self-update, and does not
create a coherent Mongo-plus-appdata backup
([source](https://github.com/Tzahi12345/YoutubeDL-Material/blob/6eadb37532063017c0087d6fd1a8845ba0984f2c/backend/app.js#L393-L428)).

## Consistency boundary and selected algorithm

MongoDB states that `mongodump` without `--oplog` is not a single moment in
time if writes occur, and `--oplog` requires a full dump of a replica-set
member
([official `mongodump` documentation](https://www.mongodb.com/docs/database-tools/mongodump/index.html#std-option-mongodump.--oplog)).
The deployment declares one standalone `mongod`, so an online `--db
ytdl_material` dump cannot make the required guarantee. Copying `/data/db`
while Mongo runs is also invalid; MongoDB requires a point-in-time filesystem
snapshot with journaling on the same volume, or all writes stopped for a
non-atomic file copy
([official backup methods](https://www.mongodb.com/docs/manual/core/backups/)).

The minimal correct boundary is consequently:

1. Quiesce the **application container only** and require that it has exited;
   leave Mongo running. Bound the interruption and always fail closed if the
   application cannot be quiesced.
2. Re-read `default.json` and establish the active database mode.
3. For Mongo mode, run a version-pinned Mongo Database Tools `mongodump` of
   exactly `ytdl_material` to a private archive. For local mode, copy
   `local_db.json` only after strict JSON/schema checks.
4. Copy only the selected appdata files to private staging with no symlink
   following. Read each twice and require stable size/hash; reject sockets,
   devices, links, unexpected ownership or oversized files.
5. Restart the application through the same bounded orchestration seam even if
   capture/validation fails. Restart failure makes the backup run fail and
   raises an operator-visible error; it never makes an incomplete artifact
   successful.
6. Validate the dump by restoring it create-only to a disposable exact-version
   local Mongo, then require the expected collections, unique-key indexes, an
   admin user, at least one role, subscription reference validity, valid
   appdata JSON and agreement between recorded mode and included source.
7. Package one private bounded archive with a versioned manifest, Mongo dump
   (or explicitly local DB), appdata allowlist and external-media reference
   summary. Validate the completed archive independently and publish through
   `create_backup_artifact()` only after all checks pass.

`mongodump` is suitable for small deployments and emits documents, collection
metadata/options and index definitions
([official definition](https://www.mongodb.com/docs/database-tools/mongodump/)).
The artifact is `partial`, not degraded: the excluded media boundary is an
intentional product decision and is stated in its manifest and sidecar.

Do not substitute `db.fsyncLock()` as a hidden convenience. It blocks all
writes, carries a lock count that must be correctly released, and would require
a materially broader operational Mongo privilege
([official `fsync` command](https://www.mongodb.com/docs/manual/reference/command/fsync/)).

## Least privilege and minimum production integration

Current Mongo has no declared authentication: its connection URI contains no
credential and the service declares no authorization/init secret. Joining the
Homelab Backup backend to `misc_default_network` today would therefore grant it
unauthenticated read/write access to Mongo. That is a STOP.

Before a plugin is enabled, the infrastructure change must:

1. enable Mongo access control with secret-backed credentials;
2. give the application only the required `readWrite` access to
   `ytdl_material` and give Homelab Backup a separate `read` account restricted
   to that database; MongoDB documents `find` as the required dump access, while
   its broader built-in `backup` role covers an entire instance and is not
   necessary for a quiesced single-database dump
   ([required access](https://www.mongodb.com/docs/database-tools/mongodump/mongodump-behavior/),
   [built-in roles](https://www.mongodb.com/docs/manual/reference/built-in-roles/));
3. attach the backend to the existing external `misc_default_network` without
   publishing Mongo's port;
4. supply the backup URI as a secret, never a schema default or log field; and
5. add only this read-only mount:

   ```text
   /docker-apps/youtubedl-material/appdata:/sources/youtubedl-material/appdata:ro
   ```

This is a migration of an existing nonempty Mongo data directory, not a fresh
image initialization. The official image says its `MONGO_INITDB_*` variables
and `/docker-entrypoint-initdb.d` scripts do not change a pre-existing database
([official image documentation](https://hub.docker.com/_/mongo/#environment-variables)).
The infrastructure plan must therefore spell out and locally rehearse the
user-creation/auth cutover; merely adding initialization variables would lock
the app out or leave the intended users absent.

Do not mount `/data/db`, `/app/users`, `/app/subscriptions`, Audio, Video, a host
root, or the Docker socket. The plugin needs no YouTube-DL Material HTTP/API
credential.

The unresolved quiescence seam is the only larger integration question. A
general Portainer token, unrestricted Docker socket, SSH key or socket proxy
with stack-wide mutation authority is not acceptable. Select and review a
mechanism constrained to stop/start this exact app container, with timeout,
finally-restart semantics and an auditable result. If that cannot be provided,
do not claim strict consistency; leave the plugin unimplemented or explicitly
obtain approval for a weaker best-effort contract.

`test()` remains non-destructive: validate the fixed appdata root/allowlist,
parse the JSON files without revealing values, connect with the Mongo read
account, require database/collection/index/source expectations, and prove no
write privilege is granted from the declared/provisioned role contract without
issuing a write probe. It must not trigger the native task, create a source-side
probe, stop a container or call any restore route.

## Create-only isolated restore contract

Restore never contacts or mutates production. It must:

1. verify artifact and sidecar size/hash, format/version/source image and Mongo
   version, then inspect every member before writing anything;
2. enforce an exact member allowlist, bounded count/member/total/compression
   ratio, safe relative names, no duplicates, links, devices or traversal, and
   strict JSON/BSON/schema validation;
3. require two new empty sentinel-marked destinations: an appdata directory and
   a fresh isolated Mongo endpoint/database. Refuse production addresses,
   source paths, `/backups`, artifact overlap, symlinks, an existing
   `ytdl_material` database or any nonempty destination;
4. restore appdata files create-only with private modes. Never overwrite or
   merge, and never emit their values;
5. use `mongorestore` **without `--drop`** into a fresh isolated Mongo server
   using the original empty `ytdl_material` database; exact v4.3.2 hardcodes
   that database name. When restore is used only as an artifact validator on a
   shared disposable Mongo, `--nsFrom='ytdl_material.*'` and a fresh random
   `--nsTo` database can instead preserve create-only behavior. MongoDB
   documents this database rename workflow
   ([official example](https://www.mongodb.com/docs/database-tools/mongorestore/mongorestore-examples/#copy-clone-a-database)).
   For any future full `--oplog` dump, namespace remapping is incompatible with
   `--oplogReplay`; restore it only into a completely fresh disposable server
   ([official option constraints](https://www.mongodb.com/docs/database-tools/mongorestore/));
6. re-query the published collections/indexes and validate users, roles,
   subscriptions, archive references, source mode and the classified missing
   media references; clean up only resources created by this invocation if
   validation fails; and
7. return `partial` with an explicit requirement to attach separately protected
   media at the original container paths before any real service cutover.

Never call `/api/restoreDBBackup`; its exact implementation clears live tables
before serial reinsertion. A local drill may boot the exact app only after the
create-only restore is complete and only on an internal disposable network.

## Exact-version two-run disposable drill

Run the drill with the pinned Linux/amd64 manifests above (or the matching
architecture manifests), no production data, no production endpoint and no
route to the Internet:

1. Start fresh `mongo:8.2.6` and YouTube-DL Material 4.3.2 containers on a
   disposable internal Docker network with private appdata/users/subscriptions/
   audio/video directories. Disable subscription execution for the boot proof
   so a synthetic subscription cannot initiate an outbound download.
2. Seed synthetic state only: a root/admin user, non-secret config marker,
   roles, one paused synthetic subscription, archive/task rows, and one file
   metadata row whose media path is deliberately absent. Also create a dummy
   cookie/config marker that can be compared without printing it.
3. Exercise `test()`, quiesced backup, artifact/sidecar validation and restore
   into brand-new appdata and Mongo destinations. Prove collection/index counts,
   config/JWT/cookie hash continuity, subscription equality, explicit missing-
   media classification and `partial` status. Boot exact v4.3.2 against a copy
   of the restored destination on the internal network and verify health/config/
   subscription reads without using the native restore route.
4. Mutate the synthetic source to a second distinguishable state and repeat
   backup/restore into a second pair of fresh destinations. Prove the first
   artifact and destination hashes did not change.
5. Negative cases must prove refusal of existing destinations/database,
   artifact/source/destination overlap, wrong source or Mongo version, unknown
   collection/schema, missing root user, dangling subscription references,
   malformed JSON/BSON, missing/tampered sidecar, traversal/link/device/archive
   bombs, local-vs-Mongo mode mismatch, timeouts, cancellation, quiesce failure,
   restart failure and concurrent restore races.
6. Tear down only the disposable resources created by the drill. A teardown
   failure is reported and never broadens cleanup scope.

The drill must use the same database tools version intended for the backend
image and record that version in the artifact manifest. MongoDB supports
restoring a dump into the same major version/feature-compatibility version
([official compatibility statement](https://www.mongodb.com/docs/database-tools/mongodump/)).

## STOP conditions

Stop research-to-implementation handoff if any of these remains unresolved:

- the operator has not approved brief application downtime and a narrow,
  reviewed quiesce/restart mechanism;
- Mongo authentication and a separate database-scoped read credential are not
  enabled;
- the exact application/Mongo OCI digests or compatible database-tools version
  are not pinned for the drill and later deployment;
- runtime source mode cannot be proven or changes during capture;
- the exact 4.3.2 + Mongo 8.2.6 pair cannot boot and restore twice locally;
- validation cannot distinguish expected external media references from a
  corrupt control-plane reference;
- a restore destination is not demonstrably isolated, fresh, empty and
  create-only;
- any design requires the app's global API key, unrestricted Docker/Portainer/
  SSH access, a live Mongo data-file copy, in-place merge, `--drop`, or any
  production restore; or
- secrets would appear in logs, filenames, command arguments, artifacts with
  broad permissions, or test output.

If downtime is declined, return this service to the conditional ledger with
the explicit reason: **current standalone Mongo plus mutable appdata has no
strict online composite snapshot boundary**. Do not weaken that fact silently.
