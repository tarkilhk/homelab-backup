# Bazarr 1.5.6 control-plane backup and restore research

Research date: 2026-08-15

Scope: the exact Bazarr deployment declared in `homelab-infra`, LinuxServer
image tag `v1.5.6-ls349`, and Bazarr 1.5.6's first-party backup, database,
configuration, and API contracts. No production host, endpoint, container,
configuration, or data was contacted or inspected, and no production state was
changed.

## Decision summary

**A Bazarr plugin is warranted and is fully buildable and testable on the dev
VM without another user decision.** Bazarr has unique, recovery-relevant
control-plane state: language profiles, cutoffs and rules, subtitle history and
upgrade chains, subtitle blacklists, failed-attempt history, enabled languages,
notifiers, provider settings, path mappings, and its Sonarr/Radarr integration
configuration.

Bazarr 1.5.6 already supplies the right online snapshot primitive. Its native
backup routine uses SQLite's online backup API and then writes one ZIP containing
`bazarr.db` and `config.yaml`. The plugin should trigger that routine through
`POST /api/system/backups`, identify the one newly-created ZIP through the list
API plus a narrowly mounted read-only backup directory, validate it strictly,
and publish the native ZIP as the Homelab Backup artifact
([exact backup implementation](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/utilities/backup.py),
[exact API resource](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/api/system/backups.py)).
It must never call Bazarr's restore or delete API methods.

Movies, episodes, embedded subtitles, and external subtitle sidecar files under
`/Movies` and `/TVShows` are deliberately excluded. They are payload managed by
the separate NAS media-backup policy. Bazarr's database records paths and
subtitle observations, not the subtitle file contents. The honest declaration
is therefore `restore_capability = "partial"`: the plugin restores Bazarr's
control plane, but full service recovery also requires a matching media/subtitle
snapshot and working Sonarr/Radarr instances.

No Bazarr downtime is required for the selected native backup. Production
activation later needs a Bazarr API key and one read-only mount of Bazarr's
native backup folder into Homelab Backup. The key is not backup-scoped—Bazarr
has one broad application API key—so this is the upstream least-privilege limit,
not an ideal scoped credential.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/media/bazarr/bazarr.yaml`](../../../homelab-infra/docker.compose/media/bazarr/bazarr.yaml):

| Property | Declared value |
| --- | --- |
| Image | `ghcr.io/linuxserver/bazarr:v1.5.6-ls349` |
| Container | `bazarr`, 256 MiB memory limit, restart unless stopped |
| Identity | `PUID=0`, `PGID=0` |
| Persistent application state | `/docker-apps/bazarr/config:/config` |
| Movie payload | `/mnt/nas-media/Video/Movies:/Movies` |
| TV payload | `/mnt/nas-media/Video/TV shows:/TVShows` |
| Published port | `6767:6767` |
| Health check | unauthenticated `GET http://localhost:6767/` |
| Compose network | the fragment's private `default_network` |

The LAN HAProxy declaration routes `bazarr.hollinger.asia` to the Docker host's
published port 6767. This demonstrates intended service use, not current
runtime health. Production contact is forbidden, so activity, effective
settings, database backend, API credentials, and backup folder were not queried.

Radarr and Sonarr mount the same Movies and TV trees read-write. Those services
are relevant to a complete disaster recovery sequence, but they are not part of
this artifact. The Docker-host Homelab Backup backend currently has no Bazarr
mount and no Docker socket
([Homelab Backup declaration](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml)).
The production wiring change should add only Bazarr's effective native backup
folder, read-only, not `/config`, `/Movies`, `/TVShows`, or the Docker socket.

The current `PUID=0`/`PGID=0` deployment is broader privilege than LinuxServer's
normal non-root `abc` process needs. It is an infrastructure hardening item, not
a reason for the backup plugin to run as root. The plugin only needs network
access to Bazarr, read access to the dedicated backup mount, and write access to
its own Homelab Backup artifact directory.

### Exact image and source provenance

Bazarr tag `v1.5.6` resolves to source commit
`5dc1d278e1b459b8bcb388097d150074307cb9ae`. LinuxServer tag
`v1.5.6-ls349` resolves to repository commit
`a7a7114ee805e7926cdbeea865691d10d69f821a`. That image starts Bazarr with
`--no-update --config /config`, and LinuxServer documents `/config` as the
persistent configuration volume
([exact Bazarr source](https://github.com/morpheus65535/bazarr/tree/5dc1d278e1b459b8bcb388097d150074307cb9ae),
[exact LinuxServer source](https://github.com/linuxserver/docker-bazarr/tree/a7a7114ee805e7926cdbeea865691d10d69f821a),
[service command](https://github.com/linuxserver/docker-bazarr/blob/a7a7114ee805e7926cdbeea865691d10d69f821a/root/etc/s6-overlay/s6-rc.d/svc-bazarr/run),
[LinuxServer image documentation](https://docs.linuxserver.io/images/docker-bazarr/)).

At research time, the deployed tag resolved to OCI index
`sha256:95f27692c3de6dbe130cd035d342d8138ec74ade7b62cfc52e11ae222c52c855`
and Linux/amd64 manifest
`sha256:4b00f5886f3307563cf06c1068037eccfc529f04070d42e2aa47f53128eed17e`.
Pin the amd64 digest in the local drill. If it does not report Bazarr 1.5.6 and
LinuxServer ls349, stop and re-research rather than silently testing a different
contract.

## Authoritative state and exclusions

### Native recovery unit

With `--config /config`, Bazarr's expected SQLite database is
`/config/db/bazarr.db`, its application settings are
`/config/config/config.yaml`, and its default backup folder is
`/config/backup`. The exact 1.5.6 routine:

1. opens the live SQLite database and copies it with `sqlite3.Connection.backup`;
2. creates `bazarr_backup_v<version>_<timestamp>.zip` in the configured backup
   folder;
3. stores the snapshot as root member `bazarr.db`; and
4. stores the settings as root member `config.yaml`.

That pair is Bazarr's native recovery unit. Preserve it whole rather than
selecting individual tables. The database source defines, among others:

- `table_languages_profiles`: named profiles, cutoff, required/forbidden
  release terms, original-format behavior, items, and tags;
- `table_history` and `table_history_movie`: action history, provider,
  language, score, paths, matches, and upgrade chains;
- `table_blacklist` and `table_blacklist_movie`: rejected subtitle/provider
  identifiers and timestamps;
- `table_settings_languages` and `table_settings_notifier`: enabled languages
  and notifier configuration; and
- shows, episodes, movies, root folders, failed attempts, subtitle indexes, and
  migration state.

See the
[exact database model](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/app/database.py).
Movie/show catalogs, posters, descriptions, media characteristics, cached
probe results, missing-subtitle calculations, and most recorded subtitle paths
can be rebuilt from Sonarr, Radarr, and disk. They remain in the native database
because selective reconstruction would depart from Bazarr's supported restore
unit and could break relationships.

`config.yaml` is authoritative and highly sensitive. It can contain Bazarr's
own API key and UI password, Sonarr/Radarr API keys, subtitle-provider accounts
and tokens, notification URLs, proxy credentials, translation keys, Plex
credentials/encryption material, webhook settings, path mappings, and custom
post-processing commands
([exact configuration schema](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/app/config.py),
[settings documentation](https://wiki.bazarr.media/Additional-Configuration/Settings/)).
The artifact and native source ZIP require secret-bearing permissions. Never
log, return, diff, or copy configuration values into a sidecar. Validation may
record only approved structural key names and booleans such as the detected
database backend.

### Explicitly excluded

- All movie and episode video/audio bytes under `/Movies` and `/TVShows`.
- External subtitle files (`.srt`, `.ass`, `.ssa`, `.sub`, `.vtt`, and similar)
  stored beside media or in configured subtitle folders.
- Embedded subtitle streams inside media containers.
- Sonarr and Radarr databases/configuration; their own plugins own that state.
- Logs, caches, temporary SQLite copies, restore staging, native historical
  backup ZIPs other than the one selected for the current run, and process
  memory.
- Docker state, container layers, the socket, compose/reverse-proxy/DNS/TLS
  configuration, NAS mount credentials, and media-server state.

Bazarr describes itself as a companion to Sonarr/Radarr and says it manages the
series and movies indexed there rather than discovering a separate media
catalog
([upstream README](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/README.md)).
Its documented subtitle setting recommends writing sidecars alongside media.
Thus a control-plane restore without the separately protected subtitle/media
tree is useful but intentionally incomplete.

## Consistency boundary and backup protocol

### Selected boundary: Bazarr's online native snapshot

Use the application-supported native backup while Bazarr remains running. The
SQLite online backup API produces one coherent database snapshot even while the
source database is active. The subsequent config-file read is separate, so
Bazarr does not guarantee one filesystem-atomic instant across database and
YAML. That small boundary is the behavior of Bazarr's own backup facility and
is acceptable here. Operators should avoid saving settings during the few
seconds of a backup, and the Homelab Backup target must serialize its runs.

Do not raw-copy the live SQLite files. Do not stop or restart Bazarr, Sonarr, or
Radarr merely for this backup. No media files are read, so concurrent media
changes do not affect artifact consistency.

The declared compose file does not configure PostgreSQL directly, but an
included environment file and runtime settings mean the effective backend
cannot be proven from static infrastructure. In Bazarr 1.5.6, the native backup
routine omits the database when PostgreSQL is enabled and still writes a
config-only ZIP. Therefore this plugin supports SQLite only and must reject any
archive that does not contain both required members. `POSTGRES_ENABLED=true`
can override the YAML setting
([database selection source](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/app/database.py),
[backup behavior](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/utilities/backup.py)).
PostgreSQL needs a separate database-native dump design and is out of scope.

### Exact execution protocol

Target configuration should contain only:

- a Bazarr base URL;
- its API key as a secret field;
- the literal local read-only mount path for Bazarr's native backup directory.

Connect, request, poll, stability, and operation deadlines are fixed plugin
limits rather than user-facing compatibility knobs.

`test()` performs authenticated `GET /api/system/status` and
`GET /api/system/backups`, requires exact version 1.5.6 with
`package_version` equal to `v1.5.6-ls349 by linuxserver.io`, migration head
`df76a4410347`, and `database_engine` formatted as `Sqlite <non-empty-version>`.
It validates the backup-list response shape and confirms the configured local
directory is readable and resolves inside the one allowlisted mount. The status
check is required because
`POSTGRES_ENABLED` can override the YAML database setting, so an apparently
valid native backup configuration cannot prove the live engine. `test()` does
not trigger a backup or inspect configuration values. Authentication uses
`X-API-KEY`; do not use query or form authentication because URLs are commonly
logged. Bazarr accepts all three but offers no backup-scoped token
([authentication source](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/api/utils.py)).

`backup()` executes this bounded state machine:

1. Acquire the target's overlap lock. Reconfirm exact version 1.5.6 and SQLite
   mode through authenticated `GET /api/system/status`, list existing native
   backups through `GET /api/system/backups`, and take a local directory baseline.
2. Record a monotonic start time, then send exactly one authenticated
   `POST /api/system/backups`. A 204 only means the job was queued; it is not
   proof of artifact creation.
3. Poll the list API and read-only directory until exactly one new regular file
   appears, with a basename matching the expected native Bazarr 1.5.6 pattern.
   Wait until size and metadata are stable across consecutive observations and
   the ZIP can be read fully. Use a fixed deadline and capped polling interval.
4. If no file appears, more than one candidate appears, the filename collides,
   another run is evident, or the candidate changes while being read, fail
   rather than guessing which “latest” file belongs to this run.
5. Stream the source through Homelab Backup's atomic artifact helper while
   hashing it. Never rename, modify, or delete Bazarr's source backup; native
   retention remains Bazarr's concern and Homelab Backup retention governs only
   its own artifacts.
6. Validate the published bytes and sidecar before reporting success. If any
   required validation fails, record the attempt as failed and do not claim a
   usable artifact.

Only `GET /api/system/status` plus `GET` and `POST` on
`/api/system/backups` are allowlisted. The backup resource also exposes `PATCH`
restore and `DELETE`; the plugin must have no code path that can issue either
verb. The broad API key is stored encrypted/secret through the
existing target mechanism, sent only to the configured Bazarr origin, redacted
from exceptions, and never placed in metrics, sidecars, filenames, or logs.

### Artifact validation

Treat the native ZIP as untrusted input even though it came from Bazarr:

- require a valid, unencrypted ZIP with passing CRC checks and bounded member
  count, compressed size, uncompressed size, and expansion ratio;
- require exactly two distinct regular root members named `bazarr.db` and
  `config.yaml`; reject missing, duplicate, extra, nested, absolute, traversal,
  symlink, device, or encrypted members;
- require non-empty members and a `config.yaml` that parses with a safe YAML
  loader and has the expected top-level structure, without logging values;
- require the SQLite header, a successful read-only `PRAGMA quick_check`, a
  successful `PRAGMA foreign_key_check`, the expected 1.5.6 application and
  Alembic tables, no active migration/temp tables, and sane table counts;
- reject effective PostgreSQL configuration and every config-only archive,
  including those produced when Bazarr catches a SQLite backup failure; and
- bind the artifact SHA-256, byte count, source/application versions, database
  backend, validation outcome, and non-sensitive table counts into the sidecar.

Do not put media paths, subtitle paths, notifier URLs, usernames, provider
names, filenames derived from private media, configuration values, or database
rows in the sidecar. Store the native ZIP itself with secret-bearing file mode
and publish it only through `write_backup_bytes()` or
`create_backup_artifact()`.

## Restore contract

Declare `restore_capability = "partial"`. Restore means:

- recreate Bazarr 1.5.6 control-plane state from the validated native pair;
- preserve profiles, history, blacklists, settings, integrations, and secrets;
- start an exact pinned Bazarr image against the reconstructed `/config`; and
- prove that the restored app can read the expected state without contacting
  production or external providers.

It does **not** recreate media, external/embedded subtitles, Sonarr/Radarr,
their credentials/endpoints, the reverse proxy, or NAS storage. A real disaster
recovery run must restore those independent payloads/services and reproduce the
same container paths before enabling Bazarr jobs.

The restore implementation must not invoke Bazarr's `PATCH` endpoint. Bazarr
1.5.6's own preparation routine calls `ZipFile.extractall()` and then restarts
the running instance; that is unsuitable for a portable untrusted artifact and
violates the no-production-restore rule
([exact restore source](https://github.com/morpheus65535/bazarr/blob/5dc1d278e1b459b8bcb388097d150074307cb9ae/bazarr/utilities/backup.py)).
Instead, the plugin performs the strict validation above, safely streams the two
known members into a newly-created isolated destination as
`config/config.yaml` and `db/bazarr.db`, fsyncs them, and refuses overwrite,
links, path escape, pre-existing destinations, or production-looking paths.

Restore verification runs in an ephemeral local network with outbound access
denied. Exact restored configuration may contain real endpoints and secrets, so
network isolation—not editing the recovered artifact—is the primary containment
boundary. A disposable verification copy may have explicitly documented local
endpoint overrides only after byte-for-byte/hash comparison with the restored
source. Synthetic `/Movies` and `/TVShows` mounts are empty for the
control-plane-only proof or are populated from the drill's separate synthetic
payload fixture for the paired-payload proof.

## Exact local two-backup/two-restore drill

All hosts, keys, accounts, media names, subtitle text, and database rows are
synthetic. The Bazarr container is the exact pinned Linux/amd64 image. It has no
route to production and no general internet egress. Sonarr/Radarr behavior is
served by deterministic local HTTP mocks; tiny fake media and subtitle files
live only under a temporary directory.

### Fixture and backup A

1. Start Bazarr with fresh temporary `/config`, `/Movies`, and `/TVShows`
   directories plus mock Sonarr/Radarr services. Wait for its own initialization
   and migrations. Confirm reported version 1.5.6.
2. Through supported local UI/API flows, configure a synthetic Bazarr API key,
   mock Arr endpoints, one notifier pointed to a mock sink, two enabled
   languages, and a named language profile with a cutoff plus must/must-not
   contain terms. Sync one synthetic movie and one episode from the mocks.
3. Add a tiny valid subtitle through Bazarr's local manual-upload flow so the
   exact application creates history. Exercise a rejected/blacklisted synthetic
   subtitle through the supported local flow. If a stable upstream flow cannot
   deterministically create one edge table, stop Bazarr, seed only that row into
   the application-migrated SQLite fixture, restart, and verify it through
   Bazarr's read API before backup. Never hand-create the schema.
4. Record the expected profile, history, blacklist, settings, and table counts.
   Separately hash the synthetic media/subtitle payload fixture; do not give the
   plugin its media mounts.
5. Run plugin `test()`, then backup A. Prove one POST occurred, the queued job was
   polled to one stable new ZIP, the artifact contains exactly the two native
   members, validation passes, and the sidecar contains no fixture secrets or
   private paths.

### Mutation and backup B

6. Change the profile cutoff/rules, enable a third language, change the mock
   notifier, add a second movie/episode, produce a second history action and
   blacklist difference through supported local flows, and alter the separate
   synthetic subtitle payload. Capture the new expected state.
7. Run backup B. Assert A remains immutable, B has a different digest, only the
   new native ZIP was selected, B's database/config reflect the mutations, and
   neither artifact contains media or subtitle bytes.

### Restore A and B independently

8. Restore A into an absent temporary root. Prove strict extraction created only
   `config/config.yaml` and `db/bazarr.db`, both matching the members in A. Start
   an exact pinned Bazarr container with production network routes denied and
   mock/empty payload mounts. Through Bazarr's read APIs/UI, verify A's profile,
   languages, history, blacklist, notifier structure, and table counts; prove
   B-only state is absent.
9. Repeat into a different absent root with B. Verify every B mutation and prove
   A/B are distinguishable. Stop both restored instances cleanly and retain no
   secret-bearing fixture output.
10. For each restore, repeat the boot once with the matching separate synthetic
    media/subtitle payload fixture. Run a local disk/Arr rescan and verify Bazarr
    recognizes the expected sidecars. Then omit or swap that payload and prove
    the verifier reports the partial/mismatched recovery instead of claiming a
    complete restore.

### Required negative proofs

Automated tests and the local drill also cover:

- 401/403, timeout, connection failure, malformed list response, and a 204 POST
  followed by no artifact;
- two new candidates, same-second filename collision, overlapping run, unstable
  source file, source disappearance, and poll deadline;
- bad ZIP/CRC, duplicate or extra members, config-only/native DB-failure ZIP,
  path traversal, absolute path, directory/symlink/device member, encrypted
  entry, size/ratio bomb, and trailing/ambiguous archive content;
- empty/bad YAML, SQLite corruption, failed integrity/foreign-key check, wrong
  schema/migration/version, and PostgreSQL enabled through config or environment;
- artifact-helper failure, disk full, cancellation, and secret-redaction tests
  for headers, exceptions, logs, metrics, and sidecars;
- restore to an existing or production-like path, overwrite attempt, network
  escape, exact-image mismatch, missing mock Arr dependency, and swapped/missing
  external payload; and
- a spy transport proving the plugin never sends `PATCH` or `DELETE` and never
  contacts any host other than the configured local Bazarr origin.

## STOP conditions

Stop without reporting a successful backup or restore when any of these holds:

- the destination or endpoint resolves to production during a restore/drill, or
  any restore would mutate a production component;
- the implementation would call Bazarr `PATCH`/`DELETE`, upload a restore, stop
  the production service, or write to its config/media tree;
- effective PostgreSQL mode is enabled, or the native ZIP lacks either
  `bazarr.db` or `config.yaml`;
- image/application version, native filename, database schema, Alembic state,
  integrity, config structure, ZIP safety, digest, or sidecar binding is not the
  pinned and validated contract;
- no unique stable post-trigger candidate can be attributed to the run,
  including overlap, multiple files, collision, timeout, or mutation while
  reading;
- the configured backup folder is not the exact dedicated allowlisted mount,
  resolves through a link or path escape, or requires write access;
- backup would require full `/config`, media mounts, host root, Docker socket,
  root privileges, or secrets in a URL/log/metric/sidecar;
- any movie, episode, embedded subtitle, or external subtitle byte is proposed
  for inclusion in this control-plane artifact;
- restore destination exists, is not isolated and create-only, has external or
  production network reachability, or cannot pin the exact image; or
- local verification cannot distinguish A from B and cannot demonstrate the
  dependency on the separately managed media/subtitle payload.

## Build/activation verdict

The plugin, strict validator, safe local restore, mocked unit/integration tests,
and exact two-run drill are **fully buildable on the dev VM now**. There is no
open product decision and no required downtime decision.

Production activation is a later infrastructure step, not a design blocker. It
must verify SQLite mode and the effective backup folder, provision the existing
broad Bazarr API key as a secret, mount only that folder read-only into Homelab
Backup, and configure a reachable Bazarr URL. A production backup trigger is a
permitted backup operation; production restore remains absolutely forbidden.
