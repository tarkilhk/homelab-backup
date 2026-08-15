# Plugin compatibility and deployment map

This matrix ties each plugin to the component declaration in `homelab-infra` and
to the isolated verification performed for the v0.2.1 baseline and subsequent
unreleased plugin milestones. It contains no service credentials. Re-run the
drill contract after changing any component image. A new release must update
this file from current manifests and fresh local drill evidence rather than
carrying these versions forward by assumption.

| Plugin | Homelab component declaration | Verification | Restore result |
| --- | --- | --- | --- |
| Cal.com | `calcom/cal.com:v6.2.0`, PostgreSQL `16` | PostgreSQL 16 connectivity and two validated Cal.com custom-format database archives | Isolated transactional restore passed |
| Gitea | `gitea/gitea:1.27.1` on primary and NAS | Exact-image native dump, two fresh labeled restore destinations, nested package volumes, repository/issue/release/package markers, streamed size/hash/sidecar checks, absolute transfer deadlines, and bounded-memory evidence passed locally | Isolated SQLite import, hook regeneration, repository `git fsck`, exact file equality, health, and a third post-mutation rollback destination passed; production backup remains gated on downtime and Docker access |
| Homelab Backup | `tarkilhk/homelab-backup:backend-v0.2.1` on primary and NAS | Two online SQLite snapshots from a running exact-image backend, private artifacts, strict manifests, independent size/hash/sidecar evidence, and two fresh restored databases passed locally | Create-only offline restore and two isolated `--network none` exact-image boots passed; capability remains `partial` because plugin code does not control or prove a destination backend lifecycle |
| Invoice Ninja | `invoiceninja/invoiceninja:5` | The current image resolved to 5.13.31; connectivity and two validated company exports passed | Isolated import delivered the synthetic marker; plugin remains `partial` because the API only reports queue acceptance |
| Jellyfin | `jellyfin/jellyfin:10.11.11` | Connectivity and two validated native archives passed | Isolated official restore and restart/readiness transition passed |
| Lidarr | `ghcr.io/linuxserver/lidarr:3.1.0.4875-ls29` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| MySQL | `mysql:8.4.0` and `mysql:8.4.0-oraclelinux8` | MySQL 8.4 connectivity and two validated logical dumps passed | Empty isolated database import and `mysqlcheck` passed; capability remains `partial` because MySQL DDL is non-transactional |
| Pi-hole | `pihole/pihole:2026.07.2` | v6 SID authentication and two validated Teleporter exports passed | Isolated Teleporter import and post-import export proof passed |
| PostgreSQL | `postgres:16` | PostgreSQL 16 connectivity and two validated custom-format archives passed | Isolated transactional clean restore passed |
| Radarr | `ghcr.io/linuxserver/radarr:6.3.0.10514-ls313` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
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
- `docker.compose/system/homelab-backup/homelab-backup.yaml`
- `docker.compose/work/invoiceninja/invoiceninja.yaml`
- `docker.compose/media/jelly_misc/jelly_misc.yaml`
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
