# Immich v3.1.0 backup and restore research

## Decision

The recoverable Immich boundary is one coordinated snapshot of the PostgreSQL
database, the effective managed-media root, and every external-library root.
For this deployment that means the database plus the actual v3.1.0 media root
and `/nas-photos`; PostgreSQL data files, Valkey, the machine-learning cache,
and generated container state are not independent backup sources.

**Implementation is blocked.** A guaranteed in-sync backup needs a short
write outage, and the current Homelab Backup backend has neither the Immich
network/storage access nor a narrow way to quiesce all writers. Raw Docker
socket access would be a broad, host-equivalent execution grant and is not the
default recommendation. In addition, the declared v3.1.0 media mount does not
match v3.1.0's documented media root, so the live authoritative path must be
resolved before any backup code is written.

This research contacted no production host or endpoint, contains no secret
values, authorizes no production changes, and forbids every production
restore.

## Exact deployment researched

The infrastructure declaration at
`/home/dev/projects/homelab-infra/docker.compose/misc/immich/immich.yaml`
defines:

- `ghcr.io/immich-app/immich-server:v3.1.0` and
  `ghcr.io/immich-app/immich-machine-learning:v3.1.0`;
- `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`, with
  `/docker-apps/immich/pgdata` mounted at PostgreSQL's data directory;
- unpinned `docker.io/valkey/valkey:9`;
- `/mnt/nas-shared/immich` mounted at `/usr/src/app/upload` and the external
  photo tree `/mnt/nas-media/Image/Photos` mounted at `/nas-photos`;
- third-party `immich-folder-album-creator:1.0.0`, which has the same
  `/nas-photos` mount and an Immich API key; and
- third-party Immich Power Tools v0.22.0, which has Immich API and database
  connectivity plus its own `/app/data` bind mount.

The release is Immich v3.1.0 at source commit `8aa95c6`; the official GHCR
package currently identifies the v3.1.0 server image by OCI index digest
`sha256:b434cb9287eea1471c9974845914d4dd328c9c2d652e446ed4930f99944f0ceb`.
The infrastructure pins only the tag, so the repository cannot prove that the
running host pulled that exact digest. See the [v3.1.0 release](https://github.com/immich-app/immich/releases/tag/v3.1.0)
and [official server package versions](https://github.com/immich-app/immich/pkgs/container/immich-server/versions?filters%5Bversion_type%5D=tagged).

### Hard media-root discrepancy

Immich v3.1.0 documents `IMMICH_MEDIA_LOCATION=/data` by default, and its
v3.1.0 release Compose file mounts `UPLOAD_LOCATION` at `/data`. The homelab
declaration instead mounts its intended managed-media directory at legacy
`/usr/src/app/upload`, and no repository-visible env file sets
`IMMICH_MEDIA_LOCATION`. Sources: [v3.1.0 environment variables](https://github.com/immich-app/immich/blob/v3.1.0/docs/docs/install/environment-variables.md#general),
[v3.1.0 Compose file](https://github.com/immich-app/immich/blob/v3.1.0/docker/docker-compose.yml),
and the local declaration at lines 22-30.

Do not infer that `/mnt/nas-shared/immich` contains current v3.1.0 managed
media. Before implementation, an explicitly approved **read-only** live check
must record the running image ID, effective `IMMICH_MEDIA_LOCATION`, container
mounts, and the storage folders under the effective root. If the effective
root is an unmounted container layer, stop and handle that incident separately;
do not silently make the backup plugin preserve the legacy path.

## Authoritative state

| State | Backup disposition |
| --- | --- |
| Immich PostgreSQL database | Required. It holds file paths and user/application metadata; Immich does not rebuild it by scanning the library. |
| Effective managed-media root | Required. Back up the whole root for the strongest recovery: `library`, `upload`, `profile`, `thumbs`, `encoded-video`, and `backups`. Original assets in `library`/`upload` and avatars in `profile` are critical; thumbnails and encoded video can be regenerated. |
| `/nas-photos` external library | Required restore prerequisite and, for a self-contained service artifact, include it under its own archive prefix. The restored stack must expose it at the same in-container path expected by the database. |
| PostgreSQL bind directory | Exclude. Use a logical dump, not a live copy of database files. |
| Valkey | Exclude. It is queue/cache runtime state, not part of Immich's documented restore inputs. |
| ML `model-cache` | Exclude. Models are downloadable cache data. |
| Infrastructure environment/secrets | Keep in the infrastructure/secret recovery system, not the artifact. They are bootstrap prerequisites and must never be logged. The database dump and media artifact are private because they contain personal data and may contain sensitive application configuration. |
| Power Tools `/app/data` and third-party behavior | Outside the native Immich contract. Research and protect separately if it is valuable; do not claim an Immich restore recovered it. Treat both add-ons as possible writers during the quiescence window until proven otherwise. |

Immich's official backup guide says a comprehensive backup needs both the
database and uploaded photos/videos; database-only automatic backups contain
metadata, not media. It recommends the entire upload location, identifies the
critical original/profile folders, and requires external libraries to be
remounted with the same structure on recovery. See [Backup and Restore](https://docs.immich.app/administration/backup-and-restore/)
and [External Library](https://docs.immich.app/guides/external-library/).

## Consistency boundary and downtime

Immich's documented strongest boundary is to stop `immich-server` while the
database and files are backed up. If it cannot be stopped, Immich recommends
database first and filesystem second so the likely mismatch is extra files
rather than database rows pointing at absent files. That ordered online method
is a degraded crash-consistency option, not the zero-ambiguity contract
required here. Source: [Immich backup ordering](https://docs.immich.app/administration/backup-and-restore/#backup-ordering).

The proposed production transaction is therefore:

1. acquire the target's normal Homelab Backup serialization;
2. use an explicitly approved, narrowly scoped orchestrator to stop writes
   from `immich-server` and both deployed third-party Immich integrations;
3. prove those writers are quiesced while PostgreSQL remains available;
4. run `pg_dump --clean --if-exists` against the named Immich database and
   stream it through gzip;
5. while quiescence remains in force, archive the effective managed-media root
   and `/nas-photos` read-only into the same staging artifact;
6. validate the complete artifact, then restart exactly the components that
   the transaction stopped and prove Immich v3.1.0 readiness; and
7. publish atomically only after restart/readiness succeeds. On every failure
   or cancellation, terminate/reap child work, discard the partial artifact,
   and make a bounded restart attempt before surfacing a secret-safe failure.

This is user-visible read-only downtime and requires explicit approval under
the repository's safety rules. Immich v3.1.0 also has an alpha maintenance API
that puts Immich into a read-only mode, stores maintenance state, and restarts
the application process. It does not establish that the two third-party
containers are quiesced, and using it is itself a production mutation/downtime
decision, so it is not a permission-free substitute. Sources: [maintenance-mode documentation](https://docs.immich.app/administration/maintenance-mode/),
[API maintenance contract](https://api.immich.app/endpoints/maintenance-%28admin%29),
and [v3.1.0 maintenance service](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/maintenance.service.ts).

## Privilege decision

The Docker-host Homelab Backup backend currently has only its backup/catalog
binds and a Jellyfin backup mount, on networks unrelated to the Immich Compose
project. It has no Immich storage mounts, database network, credentials, or
Docker socket (`homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml:7-26`).

Minimum data-plane access after approval would be:

- read-only mounts for the **verified effective** managed-media root and
  `/mnt/nas-media/Image/Photos`;
- network reachability and a secret-file PostgreSQL credential capable of
  dumping the one Immich database; and
- a narrow lifecycle operation that can stop/start only the three named
  writers and report their states.

Do not mount `/docker-apps/immich/pgdata`, do not copy live PostgreSQL files,
and do not grant a raw `/var/run/docker.sock` merely to obtain lifecycle or
`docker exec`. A purpose-built allowlisted helper or an operator-managed
pre/post hook is narrower, but choosing and operating it is an explicit
infrastructure/security decision. If no narrow mechanism is accepted, the
honest result is to remain blocked rather than fall back to an online composite
and label it consistent.

## Artifact contract

Produce one private, streamed archive through `create_backup_artifact()`; a
reasonable layout is:

```text
manifest.json
database/immich-v3.1.0-pg14.sql.gz
managed-media/<relative files from effective media root>
external-libraries/nas-photos/<relative files>
```

The manifest records schema version, exact source application version and
observed image digest, PostgreSQL major/version-extension image, UTC
quiescence/dump/archive times, host-to-container path mappings, member counts,
byte totals, and SHA-256 for the database dump and every regular file. Reject
symlinks, devices, sockets, absolute/traversing paths, duplicate members,
unexpected roots, changing files, missing required storage directories, empty
dumps, and unsupported versions. Validate gzip integrity plus PostgreSQL dump
header/trailer and require at least the expected Immich schema. Never log API
keys, database credentials, filenames containing personal information, SQL,
or media paths.

Immich v3.1.0's built-in database backup uses `pg_dump --clean --if-exists`,
gzip, a temporary file, and rename-on-success; its filename carries both
Immich and PostgreSQL versions. This is useful validation precedent, but its
database-only output is not a complete service artifact. Source: [v3.1.0 database backup service](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/database-backup.service.ts#L209-L240).

`test()` must remain non-destructive: verify the public `/api/server/version`
is exactly 3.1.0, make a bounded read-only database connection (`SELECT 1` and
server version), and verify both approved media roots are readable regular
directories. It must not enter maintenance, trigger a dump, stop a container,
or create/delete any file. The version endpoint is documented as public and
stable: [Get server version](https://api.immich.app/endpoints/server/getServerVersion).

## Safe restore contract

Declare `restore_capability = "partial"`. The plugin can safely materialize
the complete state into fresh isolated destinations and a fresh database, but
it should not receive production Docker control merely to boot and validate an
application. The local integration drill supplies that separate readiness and
content proof.

A restore must:

1. refuse any destination lacking an exact non-production restore sentinel;
2. require empty, local managed-media and external-library roots and a fresh
   isolated PostgreSQL 14 database; never overwrite an installation;
3. require the artifact version to be exactly 3.1.0 and reject unsafe archive
   members before mutation;
4. stage and hash-verify all files, then atomically place them into their fresh
   roots with the recorded container paths (`/data` and `/nas-photos` for the
   corrected v3.1.0 topology);
5. restore the SQL with the official single-transaction,
   `ON_ERROR_STOP=on` procedure, including the documented `search_path`
   adjustment, while only the fresh database is running; and
6. return `partial` with exact next steps to boot the digest-pinned v3.1.0
   disposable stack and run content/integrity proof.

Immich documents that command-line restore needs a completely fresh install,
starts only PostgreSQL before import, restores in one transaction, then starts
the remaining services. Cross-version restore may invoke migrations, so this
contract deliberately does not offer it. Source: [Immich restore via command line](https://docs.immich.app/administration/backup-and-restore/#restore-via-command-line).

## Disposable exact-version two-run drill

Use only temporary host directories, an internal Docker network, synthetic
credentials/media, and images resolved to immutable digests. Do not publish LAN
ports, join a production network, mount NAS/production paths, reuse production
secrets, or mount the Docker socket inside Homelab Backup. The test harness may
control its own disposable Docker project from the host.

Before the drill, resolve and record the exact v3.1.0 server and ML digests and
the dependency digests from the v3.1.0 release Compose file. Use PostgreSQL 14
with VectorChord 0.4.3 and pgvectors 0.2.0, the v3.1.0 Valkey digest, the
v3.1.0 server with its managed root mounted at `/data`, the ML image, and a
fresh external library mounted at `/nas-photos`. Do not start the two
third-party add-ons in the restore proof; their state and behavior are outside
the native Immich contract.

For each of two consecutive runs:

1. create a fresh source stack and seed a synthetic admin, one managed image,
   one managed video or second image, a profile image, an album/tag, and one
   external-library image; record original byte hashes and API-visible IDs;
2. quiesce the disposable writers through the same proposed production seam,
   invoke the real plugin backup path, restart, and prove source readiness;
3. require a distinct non-empty artifact/sidecar, independent SHA-256,
   manifest/member/hash validation, exact versions/digests, and no secret or
   absolute host paths in logs/metadata;
4. restore to a different fresh sentinel-marked pair of directories and fresh
   PostgreSQL database, then boot the exact digest-pinned v3.1.0 stack;
5. prove health, `/api/server/version == 3.1.0`, authenticated admin access,
   expected asset/album/tag counts, retrieval of both managed and external
   originals with byte equality, and no missing/checksum integrity findings;
   health alone is insufficient; and
6. destroy every disposable container, network, volume, credential, and temp
   directory even after injected failure.

Between run 1 and run 2, mutate only the disposable source with a distinct
second marker asset and metadata. Restore artifact A and artifact B to separate
fresh destinations and prove A contains only state A while B contains A+B.
Also inject at least dump failure, archive/hash failure, timeout/cancellation,
restart failure, unsafe archive member, non-fresh restore, and SQL restore
failure; none may publish an artifact or leave a source quiesced.

## STOP conditions

Stop before implementation or production work if any of the following holds:

- The effective v3.1.0 media root and every actual external-library path have
  not been established read-only, especially while the declaration maps the
  intended host root to `/usr/src/app/upload` instead of `/data`.
- The user has not explicitly approved a bounded production write outage and
  one narrow lifecycle design for all Immich writers.
- The only proposed lifecycle path is a raw Docker socket, unrestricted remote
  shell, or another host-wide control grant.
- Either authoritative media tree cannot be mounted read-only by the backup
  backend, the PostgreSQL dump requires host/data-directory access, or a single
  coordinated artifact cannot include both database and file state.
- A third-party integration can still mutate Immich database/media during the
  snapshot, or its exact role cannot be established.
- Any implementation would use online database-first/filesystem-second copying
  while claiming a guaranteed consistent point-in-time backup.
- The observed application/database/image versions or storage topology differ
  from this research; re-research instead of adding compatibility behavior.
- The restore destination is not local, isolated, sentinel-marked, empty, and
  disposable; the artifact is cross-version; or restore would run migrations
  under a newer image.
- A drill could contact production, mount production/NAS storage, use real
  credentials, or perform any production mutation or restore.

After local implementation and two passing drills, production still remains
backup-only. Deployment mounts, networks, secrets, lifecycle integration,
target/job creation, schedule activation, downtime, and the first production
backup trigger each require their normal explicit approvals. Production
restore is always forbidden.
