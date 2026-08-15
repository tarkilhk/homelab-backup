# Plugin compatibility and deployment map

This matrix ties each plugin to the component declaration in `homelab-infra` and
to the isolated verification performed for v0.2.1. It contains no service
credentials. Re-run the drill contract after changing any component image. A
new release must update this file from current manifests and fresh local drill
evidence rather than carrying these versions forward by assumption.

| Plugin | Homelab component declaration | Verification | Restore result |
| --- | --- | --- | --- |
| Cal.com | `calcom/cal.com:v6.2.0`, PostgreSQL `16` | PostgreSQL 16 connectivity and two validated Cal.com custom-format database archives | Isolated transactional restore passed |
| Invoice Ninja | `invoiceninja/invoiceninja:5` | The current image resolved to 5.13.31; connectivity and two validated company exports passed | Isolated import delivered the synthetic marker; plugin remains `partial` because the API only reports queue acceptance |
| Jellyfin | `jellyfin/jellyfin:10.11.11` | Connectivity and two validated native archives passed | Isolated official restore and restart/readiness transition passed |
| Lidarr | `ghcr.io/linuxserver/lidarr:3.1.0.4875-ls29` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| MySQL | `mysql:8.4.0` and `mysql:8.4.0-oraclelinux8` | MySQL 8.4 connectivity and two validated logical dumps passed | Empty isolated database import and `mysqlcheck` passed; capability remains `partial` because MySQL DDL is non-transactional |
| Pi-hole | `pihole/pihole:2026.07.2` | v6 SID authentication and two validated Teleporter exports passed | Isolated Teleporter import and post-import export proof passed |
| PostgreSQL | `postgres:16` | PostgreSQL 16 connectivity and two validated custom-format archives passed | Isolated transactional clean restore passed |
| Radarr | `ghcr.io/linuxserver/radarr:6.3.0.10514-ls313` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| Sonarr | `ghcr.io/linuxserver/sonarr:4.0.19.2979-ls320` | Connectivity and two validated native archives passed | Isolated upload, restart, and readiness passed |
| Vaultwarden | `vaultwarden/server:1.37.1` | Connectivity, two validated component archives, exact-image command checks, and synthetic component replacement passed | Isolated restore, SQLite validation, rollback-safe restart, and application readiness passed |

The repository still contains the previously verified WordPress plugin.
Current program scope excludes WordPress because the service is no longer used.
No new deployment or coverage claim should be inferred for it.

The authoritative declarations are currently under these `homelab-infra` paths:

- `docker.compose/dmz/calcom/calcom.yaml`
- `docker.compose/work/invoiceninja/invoiceninja.yaml`
- `docker.compose/media/jelly_misc/jelly_misc.yaml`
- `docker.compose/media/radarr_sonarr_lidarr/radarr_sonarr_lidarr.yaml`
- `docker.compose/tarkilnas-system/pihole/pihole.yaml`
- `docker.compose/system/postgres/postgres.yaml`
- `docker.compose/tarkilnas-system/vaultwarden/vaultwarden.yaml`

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
