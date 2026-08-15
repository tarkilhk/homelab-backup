# Plex 1.43.2 control-plane backup and restore research

Research date: 2026-08-15

Scope: the Plex deployment declared in `homelab-infra`, the exact LinuxServer
container contract, and first-party Plex backup, restore, migration, database,
and API documentation. No production endpoint or host was contacted and no
production state was read or changed.

## Decision summary

Plex's supported disaster-recovery boundary is its complete **Plex Media Server
data directory**. For this Linux container that is
`/config/Library/Application Support/Plex Media Server`. It contains the core
and blobs databases, server identity and settings, watch state, library and
matching data, playlists, metadata, artwork, and other server-managed state.
The source media mounted at `/tv`, `/movies`, `/music`, and `/photos` is not
part of this plugin.

The reliable contract is a filesystem archive taken while Plex is stopped (or
from one atomic filesystem snapshot captured while it is stopped). Plex's
official server-move procedure explicitly stops the source before copying the
data directory, and its database-restore procedure explicitly stops Plex
before replacing either database. The scheduled database backup is not an
equivalent boundary: it runs every three days, retains at most three copies,
and Plex warns that it covers only the core databases rather than all metadata.

The Homelab Backup deployment on the Plex host currently has no Plex source
mount and no Docker socket. Adding a read-only source/snapshot mount is narrow
and appropriate; adding the Docker socket or an owner Plex token is not. This
leaves a product decision before implementation:

1. **Recommended:** approve a short scheduled Plex maintenance window managed
   outside the plugin: stop Plex, expose/capture a quiesced read-only source,
   run the backup, then start Plex. Homelab Backup only reads and validates.
2. Approve a purpose-built, narrowly scoped host helper that can stop/start
   only the `plex` service and expose no general Docker API. This is more code
   and operational surface than option 1.
3. Accept an online, best-effort composite of a native scheduled database copy
   plus live metadata. This is weaker, can be stale by days, and is not the
   selected reliability contract.

Do not implement an internal Butler/API trigger, raw live-directory copy, or
Docker-socket fallback. Plex's public API and support documentation do not
document an on-demand complete backup endpoint, and a Plex owner token is broad
server/account authority rather than a backup-only credential.

**Feasibility verdict:** the plugin and its two-backup/two-restore drill are
technically buildable entirely on the dev VM, but the correct production
contract is **not fully decidable without the user's maintenance-orchestration
choice**. Stop before implementation rather than bake an unschedulable or
weaker online behavior into the plugin.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/media/plex_misc/plex_misc.yaml`](../../../homelab-infra/docker.compose/media/plex_misc/plex_misc.yaml):

| Property | Declared value |
| --- | --- |
| Image | `ghcr.io/linuxserver/plex:1.43.2.10687-563d026ea-ls307` |
| Container | `plex`, `PUID=0`, `PGID=0`, 1024 MiB limit |
| Network | the fragment's bridge `default_network` |
| Published port | host 32400 to container 32400 |
| Plex config | `/docker-apps/plex/config:/config` |
| Source media | NAS-backed TV, movie, music, and photo directories mounted separately at `/tv`, `/movies`, `/music`, and `/photos` |
| Runtime behavior | `VERSION=docker`, advertised through the declared HTTPS origin; the claim value is supplied by secret indirection and is not repeated here |

LinuxServer documents `/config` as the Plex library/configuration location and
warns that it can grow beyond 50 GB for a large collection. It documents the
media volumes separately, matching the deployed split
([LinuxServer Plex image](https://github.com/linuxserver/docker-plex)). Plex's
official Docker image likewise separates `/config` from arbitrary `/data`
media mounts and requires the configuration filesystem to support file locking
([official Plex Docker repository](https://github.com/plexinc/pms-docker)).

The Docker-host Homelab Backup declaration at the same commit mounts only its
backup root, its own database, and a Jellyfin backup directory. It does not
mount `/docker-apps/plex/config` or `/var/run/docker.sock`
([declared backend](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml)).
The Plex service and Homelab Backup backend are on different compose bridge
networks. No undeclared network reachability or host authority should be
assumed.

The image tag is version-specific but not immutable. Before the local drill,
resolve its current multi-platform and Linux/amd64 digests, record them in the
plan/tests, pull once, then run the drill with outbound network denied. If the
tag no longer resolves to Plex Media Server 1.43.2.10687 / LinuxServer build
ls307, stop and re-research; do not silently substitute `latest`.

## Authoritative state boundary

### Included

Archive the full contents of:

`/config/Library/Application Support/Plex Media Server`

except the top-level `Cache` subtree. Plex explicitly directs operators to
back up the main server data directory and says `Cache` may be excluded on
Linux. It also says Linux/NAS `Preferences.xml` is the corresponding additional
settings store, so it must remain included
([Backing Up Plex Media Server Data](https://support.plex.tv/articles/201539237-backing-up-plex-media-server-data/)).

Important in-scope state includes:

- `Preferences.xml`: server identity, machine/account association, server
  settings, and secret-bearing authentication material;
- `Plug-in Support/Databases/com.plexapp.plugins.library.db`: library
  definitions, matching information, view/watch state, ratings, playlists,
  collections, users/shares as represented by the server, and media indexes;
- `Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db`: database
  blob state paired with the core database;
- `Metadata` and `Media`: Plex-managed metadata bundles, artwork, thumbnails,
  analysis/index data, and custom selections that may be expensive or
  impossible to reproduce exactly;
- plug-in support, scanners, agents, and other server-managed files under the
  canonical data directory.

Plex describes the data directory as holding databases, metadata, artwork,
caches, and more
([Why is my Plex Media Server directory so large?](https://support.plex.tv/articles/202529153-why-is-my-plex-media-server-directory-so-large/)).
Its database-restore documentation identifies both the core and blobs
databases as a restore pair and names their WAL/SHM companions
([Restore a Database Backed Up via Scheduled Tasks](https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/)).

This artifact is secret-bearing. `Preferences.xml` can contain the server's
Plex account token and machine identity; the databases reveal viewing history,
library contents, paths, sharing state, and user-associated data. Publish it
with private permissions, never log file contents or preference values, and do
not put tokens or identifiers in sidecar metadata. Plex documents that a
claimed server requires authenticated requests and that the `X-Plex-Token`
grants access to server endpoints
([authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/),
[local authentication](https://support.plex.tv/articles/200890058-authentication-for-local-network-access/)).

### Explicitly excluded

- `/tv`, `/movies`, `/music`, and `/photos`, including every source-media byte.
  Those NAS paths remain under the separate media data-protection policy.
- `/config/Library/Application Support/Plex Media Server/Cache`, which Plex
  explicitly permits omitting on Linux.
- Any transcode directory outside the canonical server data directory,
  container layers, downloaded image/package bytes, process memory, sockets,
  and runtime locks.
- Compose, environment, proxy, DNS, certificates external to Plex, and media
  mount declarations. They remain infrastructure-as-code.
- Tautulli and Wrapperr, despite sharing the compose fragment. They have their
  own configuration volumes and need separate targets/contracts.
- Plex cloud-account state, connected-client state held by plex.tv, and source
  media. Restoring a server directory cannot guarantee recreation of external
  account grants; Plex notes that a moved server may not retain all library
  access granted to other accounts
  ([Move an Install to Another System](https://support.plex.tv/articles/201370363-move-an-install-to-another-system/)).

Do not opportunistically exclude `Metadata`, `Media`, logs, codecs, plug-ins,
or other top-level members merely because some are regenerable. The supported
boundary is the data directory with only `Cache` explicitly optional. A later
space-optimization decision would require its own restore proof and approval.

## Consistency and downtime

Plex's official migration sequence says to stop/quit/exit the **source** server
before copying its data directory, and to stop the destination before placing
the copy. It also instructs operators to stop Plex before restoring scheduled
database copies and to move aside the active `.db`, `-wal`, and `-shm` files
([server move](https://support.plex.tv/articles/201370363-move-an-install-to-another-system/),
[database restore](https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/)).

Therefore the selected backup precondition is:

1. Plex has exited cleanly.
2. No process can write the source tree.
3. The quiesced tree is presented to Homelab Backup read-only, either directly
   for the short copy window or through an atomic host snapshot created after
   shutdown.
4. The tree remains identity-stable through manifest, archive, and validation.
5. Plex starts only after the reader has released the source or snapshot
   capture has completed.

A failed HTTP health request is not proof of quiescence; a network fault can
make a live server unreachable. The plugin cannot infer process state from a
read-only directory. The external maintenance workflow must provide a bounded,
authenticated quiescence/snapshot attestation (for example, an immutable
snapshot path plus a generated manifest/run identifier), and the plugin must
refuse a live mutable source. Do not use the existence or absence of WAL/SHM
files alone as proof; stale files can remain and WAL behavior can change.

Plex's scheduled maintenance creates a database backup every three days and
keeps up to three rotating copies. Plex explicitly warns that this covers only
the core SQL database and is not a replacement for backing up all server
metadata
([Scheduled Tasks](https://support.plex.tv/articles/201553286-scheduled-tasks/)).
Those copies are useful secondary recovery material but cannot satisfy an
on-demand, complete plugin run. Plex can emit an
`admin.database.backup` webhook after that scheduled task, but that confirms
only the same database-only operation
([Webhooks](https://support.plex.tv/articles/115002267687-webhooks/)).

## Least-privilege production shape

Once the maintenance contract is approved, use:

- one dedicated read-only bind such as
  `/docker-apps/plex/config/Library/Application Support/Plex Media Server:/sources/plex/server:ro`,
  or preferably an immutable per-run host snapshot mounted at a fixed source
  root;
- no Plex account, owner token, claim token, username/password, API key, or
  network attachment;
- no Docker socket, SSH key, host root mount, privileged mode, or write access
  to the Plex tree;
- the existing writable `/backups` volume only;
- a separate, explicitly configured create-only restore root on dev/test only,
  never a production Plex path.

The application currently runs as root, and the deployed Plex files are
root-owned through `PUID=0`/`PGID=0`. That explains access but is not a reason
to broaden authority. The later infrastructure change should prefer a
dedicated numeric reader group/ACL and non-root backend if practical. If the
only way to read the tree is host root or the only way to coordinate downtime
is a general Docker socket, stop and choose an external snapshot/maintenance
mechanism.

`test()` must be non-destructive. It should validate configuration, open the
declared source without following symlinks, establish the expected canonical
directory shape, read only bounded metadata/stat information, and confirm the
external quiescence/snapshot attestation. It must not contact Plex, plex.tv, a
Docker daemon, or source media.

## Backup artifact contract

After the user selects a quiescence mechanism, the plugin should:

1. Open the source root and all descendants with descriptor-relative,
   no-symlink traversal. Reject symlinks, hard-link surprises, devices, FIFOs,
   sockets, mount escapes, non-regular files, and path/identity changes.
2. Require `Preferences.xml`, both primary database files, and the canonical
   top-level directory shape. Reject an empty or obviously new/unconfigured
   server unless the target explicitly declares that expectation.
3. Record a bounded before-manifest of relative path, type, size, mode, mtime,
   device/inode identity, and SHA-256 for regular files, excluding only the
   canonical top-level `Cache` path.
4. Reject any active/mutable source evidence and verify the external
   attestation before and after reading.
5. Stream a deterministic archive into `create_backup_artifact()` rather than
   materializing a second plaintext tree. Apply configurable member, per-file,
   total-uncompressed, archive, filename, depth, and runtime limits suitable
   for a Plex directory that can legitimately be tens of gigabytes.
6. Compute an after-manifest and require an exact match. A mismatch is a failed
   backup, not a partial success or an automatic live-copy retry.
7. Validate the staged archive independently: exact root/member allowlist,
   safe paths and types, required files, manifest/hash agreement, private
   permissions, and database headers. Run `PRAGMA quick_check` or
   `integrity_check` only with a proven-compatible SQLite implementation; Plex
   ships and documents its own `Plex SQLite` tool for database diagnostics
   ([Repair a Corrupted Database](https://support.plex.tv/articles/repair-a-corrupted-database/)).
   If standard SQLite cannot validate the exact database, record the limitation
   and make the exact-image restore drill's `Plex SQLite` check mandatory.
8. Publish atomically with a sidecar containing only non-secret facts: plugin
   and artifact format versions, declared Plex/image version, included and
   excluded roots, counts/sizes, archive SHA-256, and quiesced snapshot/run ID.

Never include `Preferences.xml` values, paths revealing media titles, machine
identifier, tokens, account/user names, library names, database rows, or
filenames below metadata bundles in logs, errors, metrics, or the sidecar.

## Restore contract

Declare `restore_capability = "partial"`. The plugin can safely validate and
materialize a complete server data directory, but applying it still requires
an exact compatible Plex image, stopped service, correct ownership, unchanged
media mount paths, and operator-controlled service startup. It must never
replace a running or existing Plex directory.

The restore operation is create-only:

1. Accept only a Homelab Backup Plex artifact with a matching sidecar hash and
   supported artifact-format/Plex-version contract.
2. Fully prevalidate archive structure, hashes, member limits, path safety,
   required files, and database identity before creating the destination.
3. Require a configured restore destination whose final leaf does not exist.
   Resolve and pin its parent by descriptor; reject symlinks, mount changes,
   aliases to source/backups, non-local/production paths, and unsafe ownership.
4. Extract into a private sibling staging directory with 0700 directories and
   0600 files, no links or special files. Fsync files, nested directories,
   staging, and parent.
5. Recompute and compare the artifact manifest. Validate both databases with
   the exact image's `Plex SQLite` before any server starts.
6. Atomically rename the staging directory to the absent destination. On any
   failure, remove only the pinned staging inode; leave the absent destination
   and every existing path untouched.
7. Return the materialized path and manual next steps. Do not start a container,
   claim a server, connect to plex.tv, edit `Preferences.xml`, remap library
   locations, mount source media, or call any Plex restore/API endpoint.

Production restore is forbidden. The official procedure also requires Plex to
be stopped before data placement, correct Linux ownership on the restored tree,
and a compatible installation before startup
([Move an Install to Another System](https://support.plex.tv/articles/201370363-move-an-install-to-another-system/)).

## Exact local two-backup/two-restore drill

Run this only after the maintenance boundary is chosen. Everything is
synthetic and confined to the dev VM.

1. Resolve and pin the exact image digest for
   `ghcr.io/linuxserver/plex:1.43.2.10687-563d026ea-ls307`; pull it once. Create
   a deny-production Docker network, temporary config root, backup root, two
   absent restore roots, and synthetic `/movies`, `/tv`, `/music`, and `/photos`
   roots. Block production DNS/routes and plex.tv after the image is present.
2. Start the exact image with `PLEX_CLAIM` unset, private loopback-only access,
   no real account, and only synthetic media mounts. LinuxServer documents
   `PLEX_CLAIM` as optional; Plex's official Docker guide explains that
   unclaimed bridge-mode first-run setup must be reached through localhost
   ([LinuxServer image](https://github.com/linuxserver/docker-plex),
   [official Docker repository](https://github.com/plexinc/pms-docker)). Use a
   helper sharing the Plex network namespace so bootstrap/API traffic remains
   loopback. If exact 1.43.2 cannot be initialized without a real account or
   public network, stop; do not use a production claim/token.
3. Seed state A through supported local UI/API behavior: server name
   `Plex Drill A`, one movie library rooted at `/movies`, one tiny valid
   synthetic media fixture, a custom poster/artwork marker if supported, and
   unwatched state. Wait for all scanner/metadata activity to become idle.
4. Stop Plex cleanly and prove it exited. Present the config tree read-only
   using the selected attestation/snapshot mechanism. Run Backup A. Verify the
   artifact and sidecar, then release the source.
5. Start the same exact image and mutate through supported local UI/API
   behavior: change the name to `Plex Drill B`, add a second synthetic item,
   mark the first watched, create a playlist/collection if supported, and
   replace the custom artwork marker. Wait for all activity to settle.
6. Stop Plex cleanly, capture a distinct immutable source, and run Backup B.
   Require different artifact hashes and exact manifests. Corrupt a copy of
   each archive, sidecar hash, database, and path table to prove restore rejects
   them without creating a destination. Prove backup refuses a live/mutable
   source and a stale or forged quiescence attestation.
7. Restore A into the first absent sentinel destination. Validate both DBs with
   the `Plex SQLite` binary from the pinned image while the service is stopped.
   Start a new exact-image container against only restored A plus identical
   synthetic media mounts. Verify through the local API/UI: name A, one item,
   unwatched state, original artwork, and absence of every B-only marker. Stop
   it cleanly.
8. Restore B into a second absent sentinel destination; never reuse A. Validate
   both DBs with exact `Plex SQLite`, start the exact image, and verify name B,
   two items, watched state, playlist/collection membership, and new artwork.
   Confirm source paths reattach only to synthetic mounts and that `Cache` was
   regenerated rather than restored.
9. Repeat cancellation and timeout during manifest, archive, validation, and
   extraction. Repeat destination/symlink swaps around publication. In every
   case require no published partial artifact, no existing destination change,
   no orphaned staging tree, and no worker/process leak.
10. Record exact image/index/amd64 digests, Plex runtime version, artifact
    hashes, database integrity output, A/B assertions, member/size limits,
    network-denial evidence, and cleanup result. Keep only synthetic evidence;
    destroy containers, volumes, media, and artifacts.

The drill passes only when two distinct quiesced backups restore to two distinct
new destinations, the exact Plex image boots each, A and B recover their own
control-plane state without cross-contamination, corrupt/adversarial inputs are
rejected, and no production identity, credential, network, media, or artifact
was involved.

## Explicit STOP conditions

Stop without fallback if any condition below occurs:

- Any request, mount, Docker action, API call, backup, restore, or drill traffic
  would reach a production host, Plex server, plex.tv account/claim path,
  production DNS/IP, media tree, credential, certificate, or artifact.
- A production restore, restore test, container start against restored state,
  file replacement, ownership change, service restart, or media remap is
  proposed. Production restores remain absolutely forbidden.
- The user has not selected and approved a maintenance/quiescence mechanism.
  Do not silently choose downtime, a host helper, a Docker socket, or the
  weaker online composite.
- The deployed image tag/digest/runtime version cannot be established, differs
  from 1.43.2.10687 / ls307, or the local drill cannot run the identical image.
- The source is live, mutable, not externally attested, or changes between
  manifests; Plex did not exit cleanly; a snapshot is writable; or its identity
  cannot be pinned for the whole read.
- Completion would require a general Docker socket, privileged container,
  `/`/host-root mount, SSH/root credential, Plex owner/account token, claim
  token, unauthenticated production network exception, or source write access.
- An internal/undocumented Butler route, scheduled-backup trigger, database
  copy heuristic, raw live SQLite copy, or failed-health-check-as-shutdown proof
  is proposed. The documented scheduled backup is incomplete and can be stale.
- The canonical server data root, `Preferences.xml`, either primary database,
  or expected directory shape is absent; source media enters the traversal; or
  anything other than the exact top-level `Cache` exclusion is proposed without
  a new approved restore proof.
- A symlink, hard-link escape, device, FIFO, socket, path traversal, case/path
  collision, duplicate member, identity swap, unsupported file type, oversized
  member/archive, compression bomb, hash mismatch, or sidecar mismatch appears.
- Database validation fails, the exact-image `Plex SQLite` tool reports anything
  other than `ok`, WAL recovery is unexpectedly required, or the artifact
  cannot boot exact Plex in the isolated drill.
- Secrets, preference values, tokens, machine/account identifiers, database
  rows, library/media names, or sensitive internal paths would be logged,
  emitted as metrics, put in the sidecar, or exposed with non-private artifact
  permissions.
- The restore destination exists, aliases source/backups/production, is not
  create-only and local, can be swapped through a symlink/mount race, or any
  failure would modify an existing path or leave published partial state.
- The local Plex instance cannot be bootstrapped and mutated without a real
  account/claim/token or outbound public access; the test network can reach
  production; source and restored instances could run concurrently with the
  same identity; or synthetic media paths differ between source and restore.
- A/B assertions do not prove server settings, library/catalog state, watched
  state, playlist/collection membership, artwork, and intentional Cache/media
  exclusions; any B-only state appears in A or vice versa.

## Version-coupling rule

This note is coupled to the deployment declaration at commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` and image tag
`1.43.2.10687-563d026ea-ls307`. Plex Media Server is closed-source and image
tags are not immutable, so the later implementation must pin observed OCI
digests and the runtime version in the drill evidence. Re-research on any Plex
version, LinuxServer build, config layout, database pair, supported backup
guidance, or deployment topology change. Do not add compatibility aliases,
alternate roots, online fallbacks, or older-version archive handling without
explicit approval.
