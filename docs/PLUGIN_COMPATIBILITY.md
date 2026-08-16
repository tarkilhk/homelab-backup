# Plugin compatibility and deployment map

This matrix ties each plugin to the component declaration in `homelab-infra` and
to the isolated verification performed for the v0.2.1 baseline and subsequent
unreleased plugin milestones. It contains no service credentials. Re-run the
drill contract after changing any component image. A new release must update
this file from current manifests and fresh local drill evidence rather than
carrying these versions forward by assumption.

| Plugin | Homelab component declaration | Verification | Restore result |
| --- | --- | --- | --- |
| Audiobookshelf | `ghcr.io/advplyr/audiobookshelf:2.36.0` (drill pinned OCI index `sha256:180acad33d69c99ed208676465d8edcb268fa46967735579a7810859885b1a8e`) | Two online SQLite snapshots plus bounded item/author native metadata through genuine read-only binds, exact schema/integrity/reference validation, private artifacts, valid sidecars, and independent phase hashes passed locally; audiobook and ebook media are excluded | Two fresh create-only restores and separate exact-image boots/restarts proved login, libraries, items, authors, collections, playlists, bookmarks, covers, and phase-specific state; capability remains `partial` because the plugin does not control destination lifecycle |
| Bazarr | `ghcr.io/linuxserver/bazarr:v1.5.6-ls349` (drill pinned linux/amd64 manifest `sha256:4b00f5886f3307563cf06c1068037eccfc529f04070d42e2aa47f53128eed17e`) | Two native online SQLite backups attributed through exact status/list/trigger polling, copied from a genuine read-only backup bind, strictly validated, privately published, and bound to structural sidecar evidence passed locally | Two RestoreService-staged create-only restores and separate exact-image boots/restarts proved phase-specific profiles, languages, history, blacklist, notifier structure, and settings; capability remains `partial` because media, subtitles, Sonarr, and Radarr are external prerequisites |
| Cal.com | `calcom/cal.com:v6.2.0` (drill pinned linux/amd64 manifest `sha256:9d962292d21244382560a129fc0a5519b83fff9fd2ad77baa72947db2b3c5001`, source `1c193cca8682b33b9866c792186033f7ef886682`) with PostgreSQL 16.14 manifest `sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00` | Two clean rounds each produced distinct online PG16 custom archives through the real scheduler under a denied-write role; exact v6.2.0 migration/catalog/profile, private artifact/sidecar, size/SHA-256, A/B immutability, source privilege, RLS, and drift checks passed | Two fresh `template0` sentinel databases per round restored transactionally through RestoreService; separate exact-image boots and restarts proved phase-specific public event/booking and complete typed control-plane markers. Capability is `partial` because the encryption/deployment configuration, external providers, and application lifecycle remain operator prerequisites |
| Gitea | `gitea/gitea:1.27.1` on primary and NAS | Exact-image native dump, two fresh labeled restore destinations, nested package volumes, repository/issue/release/package markers, streamed size/hash/sidecar checks, absolute transfer deadlines, and bounded-memory evidence passed locally | Isolated SQLite import, hook regeneration, repository `git fsck`, exact file equality, health, and a third post-mutation rollback destination passed; production backup remains gated on downtime and Docker access |
| Hindsight | `ghcr.io/vectorize-io/hindsight:0.8.6` with `pgvector/pgvector:pg18-trixie` (drill pinned OCI manifests `sha256:47eba343fe1cc0feb30839fa9bae4d1bb592676a2e7a7c3b8c80689ac93fbf8c` and `sha256:ff8da7b0714e5efa413d77f43e24d93064dd66469d418d12608c1bbc91fcf045`) | Two online PostgreSQL 18 custom dumps under a denied-write role, supported concurrent API writes, complete normalized 0.8.6 TOC/schema validation, private artifacts, valid sidecars, and independent phase hashes passed locally | Two fresh sentinel-only transactional restores and separate exact-image boots/restarts proved retained/curated/deleted API state, native upload bytes, webhook-secret recovery with API redaction, phase separation, and real rollback; capability remains `partial` because OAuth/configuration is external and exact 0.8.6 has no supported HTTP file-download route |
| Homelab Backup | `tarkilhk/homelab-backup:backend-v0.2.1` on primary and NAS | Two online SQLite snapshots from a running exact-image backend, private artifacts, strict manifests, independent size/hash/sidecar evidence, and two fresh restored databases passed locally | Create-only offline restore and two isolated `--network none` exact-image boots passed; capability remains `partial` because plugin code does not control or prove a destination backend lifecycle |
| Invoice Ninja | `invoiceninja/invoiceninja:5` (drill pinned linux/amd64 manifest `sha256:5c051fd2a7914b05deb759556ba1a7959a86a22a8ffff488267f7cdd00713217`, MySQL `sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df`, and Nginx `sha256:963cfe6e75d1c292f66589d7e190b137cf89310414c0c1c5b476dfc61a4fcd0d`) | Two exact 5.13.31 native exports per clean round were streamed privately, strictly validated, bound to sidecars, and proved to contain phase-specific company/client/invoice graphs plus exact source document bytes | Four fresh RestoreService imports across two clean rounds proved phase-specific company/client/invoice state and honest `partial` audit outcomes; destination document records/bytes remained absent because the exact vendor importer cannot reliably recover embedded documents on a private fresh destination |
| Jellyfin | `jellyfin/jellyfin:10.11.11` | Connectivity and two validated native archives passed | Isolated official restore and restart/readiness transition passed |
| Lidarr | `ghcr.io/linuxserver/lidarr:3.1.0.4875-ls29` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| MySQL | `mysql:8.4.0` and `mysql:8.4.0-oraclelinux8` | MySQL 8.4 connectivity and two validated logical dumps passed | Empty isolated database import and `mysqlcheck` passed; capability remains `partial` because MySQL DDL is non-transactional |
| Pi-hole | `pihole/pihole:2026.07.2` | v6 SID authentication and two validated Teleporter exports passed | Isolated Teleporter import and post-import export proof passed |
| PostgreSQL | Production declares mutable `postgres:16`; the local drill pinned linux/amd64 PostgreSQL 16.14 manifest `sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00` | Two clean rounds each produced distinct online custom archives through the real scheduler under a denied-write role; strict source privilege, complete catalog/TOC, private artifact/sidecar, size/SHA-256, A/B immutability, and negative-path checks passed | Two fresh `template0` sentinel databases per round restored transactionally through RestoreService; exact rows, FK definition, indexes, sequence state, extension, large-object bytes, rollback, restart, and phase separation passed; capability is `automatic` only for the named database boundary, while cluster roles/configuration and application services remain external |
| Profilarr | `santiagosayshey/profilarr:v1.1.5` (drill pinned linux/amd64 manifest `sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad`, source `21c8eaeb93241588323672866854275ff7dbed67`) | Two live SQLite snapshots plus self-contained all-ref Git bundles through narrow read-only sources, stable clean-repository fences, exact schema/migration/ref/inventory validation, private three-member artifacts, and bound sidecars passed locally in two clean exact-image drills | Two RestoreService-staged create-only restores reconstructed the database and every captured Git ref without source repository configuration; exact-image boots/restarts proved distinct A/B application state, so capability is `automatic` for all authoritative Profilarr application state |
| Prowlarr | `ghcr.io/linuxserver/prowlarr:2.4.0-develop` (drill pinned linux/amd64 manifest `sha256:a82572d17330327d1efd3d2242eac03b95402607dc96f620447a8426be2f7bd1`) | Two exact 2.4.0.5397 native SQLite backups per clean round were uniquely attributed, copied from a narrow read-only native-backup bind, strictly validated, privately published with sidecars, and removed from the native source only after durable publication | Two fresh RestoreService-staged restores per clean round proved phase-specific tags, exact restart/readiness, and persistence after a second restart; capability is `automatic` for the Prowlarr control plane, while external indexers and download clients remain prerequisites |
| Radarr | `ghcr.io/linuxserver/radarr:6.3.0.10514-ls313` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| Readarr | `ghcr.io/home-operations/readarr:rolling` (drill pinned linux/amd64 manifest `sha256:440dc56b904d7363468c1b19e60ccd9dd18b69bdccdb9712d5718779cc48d279`) | Two exact 0.4.18.2805 native SQLite backups per clean round were uniquely attributed, copied from a narrow read-only native-backup bind, strictly validated, privately published with sidecars, and removed from the native source only after durable publication | Two fresh RestoreService-staged restores per clean round proved phase-specific tags, exact restart/readiness, and persistence after a second restart; capability is `automatic` for the Readarr control plane, while books and download data remain external |
| SFTPGo | `drakkan/sftpgo:v2.7.5-alpine` (`9888a3d`; drill pinned OCI index `sha256:d1e2877600aba270ac395bf76fc7c8a2a0bb4ac83c3e6c180a0540f5d4c3efb2`) | Two live WAL-backed online SQLite snapshots through a read-only bind, complete schema-33 validation, transient-state scrubbing, private artifacts, valid sidecars, independent hashes, and semantic phase differences passed | Two fresh create-only restores and separate exact-image boots proved authentication, SQLite readiness, users/public keys, admins, groups, folders, shares, API keys, roles, and event metadata; capability remains `partial` because the plugin does not control destination lifecycle |
| Sonarr | `ghcr.io/linuxserver/sonarr:4.0.19.2979-ls320` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| Termix | `ghcr.io/lukegus/termix:release-2.3.2` (drill pinned OCI index `sha256:06a27a3dc22ae426cf0681fcdbdb58732f2aab56d8ce9e95f4deea18306e5c2f`) | Two stable encrypted-state snapshots through a genuine read-only bind, strict v2/AES-256-GCM authentication, SQLite integrity/schema checks, private manifests/artifacts, valid sidecars, and independent phase hashes passed locally | Two fresh create-only restores and separate exact-image boots proved password authentication plus phase-specific host/snippet content; capability remains `partial` because the plugin does not control destination lifecycle |
| Vaultwarden | `vaultwarden/server:1.37.1` | Connectivity, two validated component archives, exact-image command checks, and synthetic component replacement passed | Isolated restore, SQLite validation, rollback-safe restart, and application readiness passed |

The repository still contains the previously verified WordPress plugin.
Current program scope excludes WordPress because the service is no longer used.
No new deployment or coverage claim should be inferred for it.

The authoritative declarations are currently under these `homelab-infra` paths:

- `docker.compose/dmz/calcom/calcom.yaml`
- `docker.compose/gitea/gitea/gitea.yaml`
- `docker.compose/misc/hindsight-db/hindsight-db.yaml`
- `docker.compose/system/homelab-backup/homelab-backup.yaml`
- `docker.compose/work/invoiceninja/invoiceninja.yaml`
- `docker.compose/media/jelly_misc/jelly_misc.yaml`
- `docker.compose/media/books/books.yaml`
- `docker.compose/media/profilarr/profilarr.yaml`
- `docker.compose/media/radarr_sonarr_lidarr/radarr_sonarr_lidarr.yaml`
- `docker.compose/misc/sftpgo/sftpgo.yaml`
- `docker.compose/misc/termix/termix.yaml`
- `docker.compose/tarkilnas-system/pihole/pihole.yaml`
- `docker.compose/system/postgres/postgres.yaml`
- `docker.compose/tarkilnas-system/vaultwarden/vaultwarden.yaml`
- `docker.compose/tarkilnas-system/gitea/gitea.yaml`
- `docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml`

## Current deployment state and prerequisites

The Docker-host and TarkilNAS deployments were moved to the v0.2.1 backend and
frontend images and redeployed. The Docker-host backend now has the declared
Jellyfin backup-directory mount used by the verified native backup flow. Exact
image tags, mounts, and network membership remain authoritative in
`homelab-infra`; inspect them again before every service-specific drill.

The TarkilNAS backend has the Docker socket required by Vaultwarden. Docker-socket
access is host-equivalent privilege even when the socket is mounted read-only, so
the backend must remain on a trusted network and target only the declared local
Vaultwarden container.

The repaired v0.2.1 deployment completed its backup-only production validation
for the configured targets after the isolated local drills. Historical runs made
by older images are not evidence for a newly changed plugin or component image.

No production restore is permitted. Production validation remains limited to
non-destructive connectivity checks and native backup/export triggers. Every
restore drill uses an isolated local destination.

The Gitea plugin is locally verified but not yet enabled in production. Its
consistent backup deliberately stops Gitea, and the primary backup backend does
not currently have the required constrained Docker execution path. Both remain
explicit production gates.

The Homelab Backup self-backup plugin is locally verified but not yet deployed.
It requires only read access to the instance's own
`/app/db/homelab_backup.db` and ordinary write access to `/backups`; it neither
uses nor needs the Docker socket. Production targets and schedules may be added
only after the containing release is deployed. Production restore remains
forbidden.

The SFTPGo plugin is locally verified but not yet deployed. The Docker-host
backend needs only
`/docker-apps/sftpgo/config:/sources/sftpgo/config:ro`; it does not need SFTPGo
network access, an administrator credential, the Docker socket, or downtime.
Pin the SFTPGo image digest and add this read-only mount during the production
rollout. `/srv/sftpgo` and all `/nas/*` payload remain outside the plugin and
must be classified separately before claiming file-data coverage.

The Termix plugin is locally verified but not yet deployed. The Docker-host
backend needs only
`/docker-apps/termix/data:/sources/termix/data:ro`; it does not need Termix
network access, an application credential, the Docker socket, or downtime. The
plugin captures the latest successfully persisted 2.3.2 state. Termix can keep
some acknowledged mutations only in memory until another save-triggering change
or graceful shutdown, so the plugin does not claim zero-second RPO. Production
restore remains forbidden; the create-only restore contract is for isolated
local drills.

The Audiobookshelf plugin is locally verified but not yet deployed. The
Docker-host backend needs only
`/docker-apps/audiobookshelf/config:/sources/audiobookshelf/config:ro` and
`/docker-apps/audiobookshelf/metadata:/sources/audiobookshelf/metadata:ro`.
It does not need Audiobookshelf network access, an administrator credential,
the Docker socket, downtime, or access to audiobook and ebook media. Production
restore remains forbidden; the create-only restore contract is for isolated
local drills.

The Hindsight plugin is locally verified but not yet deployed. Production
activation needs only a user-approved attachment to the private Hindsight
database network and a dedicated database-scoped read-only dump identity. It
does not need Hindsight API/OAuth credentials, a host mount, a Docker socket,
or downtime. Artifacts contain application data and secrets in plaintext and
therefore require the repository's protected backup destination. Production
restore remains forbidden. The restore code is disabled by default and runs
only when `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` is deliberately set for a
disposable local drill; destination-owner credentials exist only there.

The Bazarr plugin is locally verified but not yet deployed. Production
activation needs an approved Bazarr API key and network route plus only the
native backup directory mounted read-only at `/sources/bazarr/backups`. The
probe refuses versions other than Bazarr 1.5.6/LinuxServer ls349 and refuses
PostgreSQL mode. It does not need `/config`, media, subtitle, Docker-socket, or
host-control access and does not stop Bazarr. Production restore remains
forbidden; restore is disabled unless
`HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1` is deliberately set for a disposable
local drill.

The Profilarr plugin is locally verified and is not deployed. A later approved
production activation needs only
`/docker-apps/profilarr/config/profilarr.db` and
`/docker-apps/profilarr/config/db` exposed as two narrow read-only sources under
`/sources/profilarr`; it needs no Profilarr or Arr API key, Git credential,
network access, Docker socket, downtime, or write access. The source repository
must be clean, settled, self-contained, and on a symbolic branch. Dirty,
untracked, shallow, partial, externally backed, or in-progress Git state is an
actionable failed backup rather than a compatibility mode. The composite
artifact contains the secret-bearing SQLite control plane and private Git
history, so it requires the same protected storage as application credentials.
Production restore remains forbidden.

The Readarr and Prowlarr plugins are locally verified and are not deployed.
Production activation must first replace their mutable image selectors with the
exact manifests recorded above, approve deletion of each newly attributed native
manual backup only after durable central publication, and mount only each
application's native backup folder read-only at `/sources/readarr/backups` and
`/sources/prowlarr/backups`. The API key is used only for exact status, native
backup creation/listing/deletion, and isolated restore; the plugin never uses a
UI session or HTTP artifact download. Prowlarr's current root-owned native files
also require an explicit readable ownership/ACL contract if the backend later
runs unprivileged. Production restore remains forbidden and the restore gates
must never be set on a normal backend.
