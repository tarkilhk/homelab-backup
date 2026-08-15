# Plan 007: Service coverage completion ledger

## Status

- **Priority**: P0
- **Effort**: XL program ledger
- **Risk**: HIGH
- **Depends on**: Plan 001 inventory
- **State**: IN PROGRESS
- **Audited against**: `homelab-infra`, 2026-08-15

## Purpose

This ledger is the completion proof for Plan 001. Every named inventory row must
finish in exactly one of these categories:

- `verified-plugin`: scheduled validated artifacts and an isolated restore drill;
- `plugin-local`: implementation and drills complete, production rollout pending;
- `planned-plugin`: authoritative state and supported restore boundary known;
- `blocked`: a named user decision, privilege, downtime, or vendor boundary is required;
- `external`: accepted machine/storage/vendor protection with current evidence;
- `git-rebuild`: authoritative declaration/source is in version control;
- `stateless`: runtime state is disposable or regenerated;
- `retired`: no longer active or intentionally excluded;
- `unprotected`: explicitly accepted risk; or
- `unclassified`: insufficient current evidence. This is never a final state.

Replication and failure-domain placement remain outside Homelab Backup, but a
row cannot be called protected merely because its data sits on RAID, a VM is
included in a broad image job, or a plugin directory exists.

## Company and recovery services

| Service/scope | Current category | Evidence or remaining gap |
| --- | --- | --- |
| Invoice Ninja | `planned-plugin` | Existing native plugin and local version drill; production target/run evidence still needs final audit. |
| Cal.com | `planned-plugin` | Existing native PostgreSQL export plugin; DMZ reachability and production target/run evidence remain. |
| WordPress | `retired` | User explicitly removed it from this program; historical plugin remains only. |
| Astro company site | `git-rebuild` | Authoritative source is the primary-Gitea `hollinger.asia-site` repository; CI builds and deploys reproducible `dist`. Unpublished local work remains outside that protection. |
| Primary Gitea | `plugin-local` | Exact Gitea 1.27.1 backup/restore milestone is committed; production rollout evidence remains. |
| NAS Gitea | `plugin-local` | Same exact plugin contract; distinct production target and mirror/unique-state evidence remain. |
| Gitea Actions runners | `stateless` | Runners are disposable executors; configuration is declared in Git. |
| Gitea OCI/package registry | `plugin-local` | Included in the Gitea native dump contract; production artifact proof remains. |
| Bitwarden Lite | `blocked` | Active pilot declaration still floats on `beta`, while the canonical vault route remains on NAS Vaultwarden. Confirm authoritative use and pin an exact version before designing PostgreSQL/files coverage. |
| Vaultwarden | `planned-plugin` | Existing component plugin and exact local drill; production target/run evidence remains. |
| Standard Notes | `blocked` | Exact composite boundary requires a short scheduled source downtime; explicit approval is pending. |
| Monica | `blocked` | Declared Monica 4.1.2/MariaDB 12.3 state shows near-zero recent use. Confirm valuable company CRM data before researching a composite plugin. |
| Metabase | `planned-plugin` | Named shared-PostgreSQL database is authoritative; explicit production target/run evidence remains. |
| SFTPGo control plane | `plugin-local` | Exact 2.7.5 live SQLite milestone and two restores are committed. |
| SFTPGo client payload | `blocked` | `/srv/sftpgo` and NAS mappings are intentionally outside the control-plane plugin; an explicit value/ownership and bulk-data-policy decision is required before adding any payload coverage. |
| Hermes gateway/UI/client-work/reminders | `blocked` | Live state is under `/home/tarkil/.hermes`; a constrained host-state plugin needs exact-version/consistency research and explicit host-access approval. |
| Hermes Workspace | `blocked` | Runbook references stale/absent deployment tasks; one runtime check or user declaration is needed to classify it active or retired. |
| OneCLI PostgreSQL | `planned-plugin` | Existing PostgreSQL mechanism is suitable; named target/run evidence remains. |
| OneCLI `/app/data` | `blocked` | Active 1.45.0 state warrants coordinated PostgreSQL/files coverage, but constrained Claw access and the documented do-not-copy CA-key policy require explicit decisions. |
| Airbyte | `blocked` | Declared abctl/Kind deployment lacks an exact version and verified active connections. Confirm current use and checkpoint value before selecting native/PV coverage. |
| GenBI | `blocked` | Manual-only stack contains several stores. Confirm which object/catalog/notebook/database data is authoritative before splitting coverage by mechanism. |
| Cloudflare | `planned-plugin` | Read-only account export is a selected Wave 1 boundary; exact scopes and isolated replay validation remain. |
| pfSense | `planned-plugin` | Native config export selected; exact API/export and isolated restore appliance drill remain. |
| Synology DSM configuration | `planned-plugin` | Native DSM configuration export selected; API/export and isolated restore evidence remain. |
| Synology datasets/photos | `external` | Bulk dataset protection belongs to storage replication/backup, not this application; current external evidence still needs final audit. |
| Homelab Backup Docker host | `plugin-local` | Exact 0.2.1 self-backup and two isolated boots are committed. |
| Homelab Backup NAS | `plugin-local` | Same self-backup contract; distinct production target/run evidence remains. |
| Portainer | `blocked` | EE 2.41.1 has UI-only endpoint/registry/credential metadata in `portainer_data`; choose acceptable runbook rebuild or approve native/volume backup privilege. |
| Portainer NAS agent | `stateless` | Agent is a rebuildable endpoint bridge; declaration is in Git. |
| Shared PostgreSQL databases | `planned-plugin` | Generic per-database PostgreSQL plugin is drilled; all authoritative DB targets/jobs need final enumeration. |
| `postgres2` | `blocked` | No current manifest consumer is declared. Identify its owner/databases before adding coverage or separately retiring it. |

## Personal and high-value services

| Service/scope | Current category | Evidence or remaining gap |
| --- | --- | --- |
| Paperless-ngx | `blocked` | Exact 2.20.15 native export/restore plan is complete; production container-execution privilege decision is pending. |
| Immich PostgreSQL/assets | `blocked` | Exact v3.1.0 research requires read-only verification of the effective managed-media root, explicit bounded downtime approval, and a narrow all-writer quiescence path; the raw Docker socket is rejected. |
| Immich machine-learning cache | `stateless` | Model cache is reproducible. |
| Immich Folder Album Creator | `stateless` | Automation declaration is in Git; NAS photos are external payload. |
| Immich Power Tools | `unclassified` | Determine whether `/app/data` contains unique state beyond Immich and Git configuration. |
| Home Assistant | `blocked` | Full Supervisor backup/download is supported, but exact research found the multi-component archive is sequential and deployed add-ons use hot backup; strict consistency needs explicit quiescence/downtime approval before implementation. |
| Mosquitto/ESPHome/HACS/AdGuard on HAOS | `blocked` | Full Supervisor backup includes these components, but Mosquitto, ESPHome, and AdGuard currently declare hot backup semantics; they inherit the Home Assistant quiescence/downtime gate. |
| Firefly III | `blocked` | Exact 6.6.3 source ordering permits attachment DB/file races that no online before/after fence can rule out; a consistent MariaDB/uploads artifact needs explicit brief app-downtime approval and a narrow lifecycle path. |
| Sure | `unclassified` | Active digest-pinned Rails/PostgreSQL/storage deployment; consistency/restore boundary needs primary research. |
| Hindsight | `unclassified` | Active 0.8.6 PostgreSQL plus `.codex` files; determine authoritative/encrypted file boundary. |
| Quartz | `blocked` | Ansible keeps user-authored content outside the framework checkout at `/opt/claw/quartz/content` and only seeds a Git-declared placeholder when empty. Protecting that authoritative host state requires an explicitly approved constrained Claw read-only path. |
| Termix | `plugin-local` | Exact 2.3.2 read-only encrypted-state plugin, two distinct backups, two create-only restores, and two authenticated exact-image boots pass locally; production mount/target/schedule evidence remains. |
| Mealie | `blocked` | Native v3.22.0 export is not a transactional PostgreSQL/files snapshot and native restore requires unsafe cluster privilege. A dependable logical dump plus `/app/data` capture needs explicit brief Mealie downtime and narrow lifecycle/read-only mount approval. |
| Wallabag | `unclassified` | Active 2.6.14 data/images deployment; exact database and restore boundary need research. |
| YouTube-DL Material configuration | `unclassified` | Active 4.3.2 MongoDB/appdata deployment; determine which subscriptions/config are valuable. |
| YouTube-DL media payload | `external` | Audio/video payload follows NAS/media policy and is excluded from application artifacts. |
| Speedtest Tracker | `unprotected` | Measurement history is telemetry and is deliberately excluded by the active goal; its current declaration also has no persistent volume. |

## Media and observability

| Service/scope | Current category | Evidence or remaining gap |
| --- | --- | --- |
| Jellyfin | `planned-plugin` | Existing native plugin and exact local drill; production path/target evidence remains. |
| Radarr | `planned-plugin` | Existing Servarr plugin and exact local drill; production target/run evidence remains. |
| Sonarr | `planned-plugin` | Existing Servarr plugin and exact local drill; production target/run evidence remains. |
| Lidarr | `planned-plugin` | Existing Servarr plugin and exact local drill; production target/run evidence remains. |
| Readarr | `planned-plugin` | Thin exact-version Servarr subclass remains a lower-priority milestone. |
| Prowlarr | `planned-plugin` | Thin exact-version Servarr subclass remains a lower-priority milestone. |
| Plex | `unclassified` | Valuable configuration/history only; research native/config-safe boundary. |
| Audiobookshelf | `planned-plugin` | Exact 2.36.0 read-only SQLite plus item/author metadata boundary is specified in Plan 009; media remains excluded. Local implementation and two exact-image drills are in progress. |
| Calibre | `unclassified` | Determine whether library metadata is authoritative or externally protected with books. |
| Bazarr | `unclassified` | Configuration/history only; media/subtitle payload policy needs confirmation. |
| Jellyseerr | `unclassified` | Configuration/request history may merit native/config backup. |
| Jellystat | `unprotected` | Playback/statistics history is excluded telemetry; desired runtime configuration is declared in Git, so no PostgreSQL target belongs in this program. |
| Tautulli | `unprotected` | Playback/statistics history is excluded telemetry; the remaining convenience configuration does not justify a bespoke plugin in this program. |
| Wrapperr | `stateless` | Generated presentation over other service data unless contrary evidence appears. |
| Profilarr | `unclassified` | Determine whether authoritative profiles are in Git or only app storage. |
| Tracearr | `unprotected` | TimescaleDB/Redis contain excluded playback telemetry and queues; desired service configuration is declared in Git. |
| Maloja/Multi-Scrobbler | `unprotected` | Scrobble history is excluded telemetry and Multi-Scrobbler's queue/cache is disposable; desired integration configuration is declared in Git. |
| Transmission/Flood | `unprotected` | Torrent/session queues are explicitly excluded, and the remaining convenience UI settings are cheaply reconstructed from the Git-declared deployment; no bespoke plugin is warranted. |
| CleanupArr/Houndarr | `stateless` | Automation is Git-declared; runtime queues/cache are disposable. |
| Grafana provisioned state | `git-rebuild` | Provisioning and dashboards are declared in `homelab-infra`. |
| Grafana UI-only state | `blocked` | A read-only API audit on 2026-08-15 found all 20 dashboards plus alerting policy/rules in Git, but the still-used `prometheus` datasource UID is UI-only while only Mimir and Loki are provisioned. Migrate that datasource to Git or explicitly accept its rebuild before skipping an API export. |
| Prometheus/Mimir/Loki | `unprotected` | Telemetry history is deliberately excluded absent a new retention requirement. |
| Exporters/Alloy/Telegraf/rsyslog/MCPs | `stateless` | Desired configuration is in Git; runtime state is disposable. |

## Completion rules

- Every `unclassified` row must be resolved with current evidence.
- Every `planned-plugin` row must either reach `verified-plugin`, be explicitly
  rescheduled with an accepted risk, or be reclassified based on stronger
  evidence.
- Every `plugin-local` row needs production deployment, target, schedule, and
  validated run evidence before becoming `verified-plugin`.
- Every `external` row needs a named current mechanism and recovery evidence.
- Every `unprotected` row needs explicit user acceptance or remediation.
- Grouped helpers/caches may inherit `stateless` only when their authoritative
  parent row names the actual protected state.
