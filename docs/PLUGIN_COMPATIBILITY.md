# Plugin compatibility and deployment map

This matrix ties each plugin to the component declaration in `homelab-infra` and
to the isolated verification performed for this release. It contains no service
credentials. Re-run the drill contract after changing any component image.

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
| WordPress | `wordpress:7.0.2` with MySQL `8.4.0` | Connectivity and two full files-plus-database archives passed | Isolated replacement, validation, and rollback-capable restore passed |

The authoritative declarations are currently under these `homelab-infra` paths:

- `docker.compose/dmz/calcom/calcom.yaml`
- `docker.compose/work/invoiceninja/invoiceninja.yaml`
- `docker.compose/media/jelly_misc/jelly_misc.yaml`
- `docker.compose/media/radarr_sonarr_lidarr/radarr_sonarr_lidarr.yaml`
- `docker.compose/tarkilnas-system/pihole/pihole.yaml`
- `docker.compose/system/postgres/postgres.yaml`
- `docker.compose/tarkilnas-system/vaultwarden/vaultwarden.yaml`
- `docker.compose/work/wordpress/wordpress.yaml`

## Current deployment prerequisites and gaps

The Docker-host backup backend currently mounts only `/backups` and `/app/db`.
Therefore Jellyfin and WordPress cannot be configured there yet: their plugins
also require the Jellyfin server backup directory or WordPress document root to
be mounted into the backend. This is a deployment prerequisite, not a plugin
fallback; the plugin intentionally refuses inaccessible paths.

The TarkilNAS backend has the Docker socket required by Vaultwarden. Docker-socket
access is host-equivalent privilege even when the socket is mounted read-only, so
the backend must remain on a trusted network and target only the declared local
Vaultwarden container.

The production observations made before this release showed successful
PostgreSQL and Vaultwarden backups, an Invoice Ninja export, and a Pi-hole v6 401
from the legacy authentication flow. The Pi-hole implementation now uses the v6
SID contract and passes against 2026.07.2 locally. The repaired image has not yet
been deployed for the final production backup-only validation, so those earlier
runs are not evidence for the new image.

No production restore is permitted. Production validation is limited to
non-destructive connectivity checks and native backup/export triggers. Every
restore drill uses an isolated local destination.
