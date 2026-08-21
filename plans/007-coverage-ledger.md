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
| Invoice Ninja | `plugin-local` | Exact 5.13.31 native export and honestly partial import passed two clean local rounds with four fresh RestoreService destinations, restart persistence, final repository gates, and clean Standards/Spec reviews. Company/client/invoice state is proven; source/export document bytes are proven, while upstream cannot reliably restore them into a fresh private destination. Production rollout evidence remains. |
| Cal.com | `plugin-local` | Exact 6.2.0/PG16.14 A/B scheduler backups and four independent fresh RestoreService destinations passed across two clean local rounds, including strict migration/catalog/profile evidence, exact-image app boot/restart, public event/booking content, typed control-plane markers, repository gates, and clean implementation/drill reviews. Capability remains honestly `partial`; actual runtime digests, DMZ database-only reachability, dedicated denied-write grants, production target/job, and an approved backup-only run remain rollout gates. |
| WordPress | `retired` | User explicitly removed it from this program; historical plugin remains only. |
| Astro company site | `git-rebuild` | Authoritative source is the primary-Gitea `hollinger.asia-site` repository; CI builds and deploys reproducible `dist`. Unpublished local work remains outside that protection. |
| Primary Gitea | `plugin-local` | Exact Gitea 1.27.1 backup/restore milestone is committed; production rollout evidence remains. |
| NAS Gitea | `plugin-local` | Same exact plugin contract; distinct production target and mirror/unique-state evidence remain. |
| Gitea Actions runners | `stateless` | Runners are disposable executors; configuration is declared in Git. |
| Gitea OCI/package registry | `plugin-local` | Included in the Gitea native dump contract; production artifact proof remains. |
| Bitwarden Lite | `blocked` | Active pilot declaration still floats on `beta`, while the canonical vault route remains on NAS Vaultwarden. Confirm authoritative use and pin an exact version before designing PostgreSQL/files coverage. |
| Vaultwarden | `plugin-local` | Exact 1.37.1 stop-based A/B backups and four independent fresh restores passed across two clean rounds, including Web Vault secure-note and attachment recovery before and after restart. File Sends are unused and remain outside the client-level recovery claim. Production still requires the exact image declaration, opted-in stop schedule, target/run evidence, and a backup-only validation. |
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
| Cloudflare | `blocked` | Primary-source research selected normalized zone DNS, remotely managed tunnel configurations, and active private routes, excluding connector tokens and dynamic fail2ban bans. Exact scope still needs a read-only live inventory, and current-contract restore proof needs a separate disposable Cloudflare account/zone plus an independently allowlisted write token; local mocks cannot prove the vendor restore. |
| pfSense | `blocked` | Native encrypted full-config export and an isolated appliance drill are specified, but the narrowest WebGUI privilege combines backup and destructive restore while the plugin has no safe automatic restore seam. Implementation awaits an explicit privilege decision and an approved restore contract. |
| Synology DSM configuration | `blocked` | DSM 7 supports administrator-only manual `.dss` export/restore but publishes no automation contract or artifact-format specification. Supported restore proof requires Virtual DSM on compatible Synology VMM hardware; the Ubuntu dev VM cannot provide it and production NAS restore is forbidden. |
| Synology datasets/photos | `external` | Bulk dataset protection belongs to storage replication/backup, not this application; current external evidence still needs final audit. |
| Homelab Backup Docker host | `plugin-local` | Exact 0.2.1 self-backup and two isolated boots are committed. |
| Homelab Backup NAS | `plugin-local` | Same self-backup contract; distinct production target/run evidence remains. |
| Portainer | `blocked` | EE 2.41.1 has UI-only endpoint/registry/credential metadata in `portainer_data`; choose acceptable runbook rebuild or approve native/volume backup privilege. |
| Portainer NAS agent | `stateless` | Agent is a rebuildable endpoint bridge; declaration is in Git. |
| Shared PostgreSQL databases | `plugin-local` | Plan 017 completed the generic named-database PostgreSQL 16 foundation with private authentication, denied-write source evidence, exact archive/catalog provenance, and two clean rounds of A/B backups plus independent fresh transactional restores. Every authoritative production database, runtime pin, role/default grant, target/job, and backup-only run still needs explicit enumeration and rollout evidence. |
| Generic Oracle MySQL databases | `blocked` | The generic plugin has only the legacy two-backup/one-restore baseline. Exact MySQL Shell 8.4.0 testing disproved the proposed schema-only online contract: Shell wrote bytes but warned that consistency could not be guaranteed without role/system metadata plus broader global authority. Plan 019 is stopped pending explicit approval of broader privileges or quiescence, or a decision to retain application-specific native/composite boundaries. MariaDB is a separate vendor contract and is not covered by this row. |
| Pi-hole | `blocked` | The existing Teleporter plugin has only the legacy two-backup/one-restore baseline. Exact 2026.07.2 source proves export is non-atomic across TOML, optional leases, and SQLite tables, while the narrowest application password still authorizes unrelated write endpoints. Plan 020 is stopped pending explicit consistency and source-authority policies; no current-contract artifact or two-round restore proof exists. |
| `postgres2` | `blocked` | No current manifest consumer is declared. Identify its owner/databases before adding coverage or separately retiring it. |

## Personal and high-value services

| Service/scope | Current category | Evidence or remaining gap |
| --- | --- | --- |
| Paperless-ngx | `blocked` | Exact 2.20.15 native export/restore plan is complete; production container-execution privilege decision is pending. |
| Immich PostgreSQL/assets | `blocked` | Exact v3.1.0 research requires read-only verification of the effective managed-media root, explicit bounded downtime approval, and a narrow all-writer quiescence path; the raw Docker socket is rejected. |
| Immich machine-learning cache | `stateless` | Model cache is reproducible. |
| Immich Folder Album Creator | `stateless` | Automation declaration is in Git; NAS photos are external payload. |
| Immich Power Tools | `blocked` | Exact v0.22.0 `/app/data/app.db` contains unique settings, credentials, workflow, import, and run state. It must be captured and restored as a version-linked child of the Immich composite, so it inherits the managed-media-root, downtime, and narrow lifecycle gates. |
| Home Assistant | `blocked` | Full Supervisor backup/download is supported, but exact research found the multi-component archive is sequential and deployed add-ons use hot backup; strict consistency needs explicit quiescence/downtime approval before implementation. |
| Mosquitto/ESPHome/HACS/AdGuard on HAOS | `blocked` | Full Supervisor backup includes these components, but Mosquitto, ESPHome, and AdGuard currently declare hot backup semantics; they inherit the Home Assistant quiescence/downtime gate. |
| Firefly III | `blocked` | Exact 6.6.3 source ordering permits attachment DB/file races that no online before/after fence can rule out; a consistent MariaDB/uploads artifact needs explicit brief app-downtime approval and a narrow lifecycle path. |
| Sure | `blocked` | Exact v0.7.1-hotfix.1 state spans PostgreSQL and local Active Storage; Rails write ordering prevents a proven online composite snapshot. A dependable artifact needs explicit brief web/worker downtime and a narrow Sure-only lifecycle path. |
| Speakr | `blocked` | Active DMZ state spans SQLite/secret-bearing instance data and authoritative uploads. Exact upstream guidance requires stopped capture; production needs explicit approval for a narrow DMZ stop/stream/restart transport, preferably a forced-command helper rather than a second backend or Docker socket. |
| Hindsight | `plugin-local` | Exact 0.8.6 PostgreSQL 18 backup and transactional create-only restore plugin passed two independent artifacts, fresh restores, API/native-file checks, and exact-image boots/restarts. Production still needs an approved private-network attachment, read-only dump identity, target, schedule, and backup-only run proof. |
| Quartz | `blocked` | Ansible keeps user-authored content outside the framework checkout at `/opt/claw/quartz/content` and only seeds a Git-declared placeholder when empty. Protecting that authoritative host state requires an explicitly approved constrained Claw read-only path. |
| Termix | `plugin-local` | Exact 2.3.2 read-only encrypted-state plugin, two distinct backups, two create-only restores, and two authenticated exact-image boots pass locally; production mount/target/schedule evidence remains. |
| Mealie | `blocked` | Native v3.22.0 export is not a transactional PostgreSQL/files snapshot and native restore requires unsafe cluster privilege. A dependable logical dump plus `/app/data` capture needs explicit brief Mealie downtime and narrow lifecycle/read-only mount approval. |
| Wallabag | `blocked` | Exact 2.6.14 SQLite, site-credential key, and downloaded-image boundary is known, but coherent capture needs approved brief downtime and narrow lifecycle control. Production activation also awaits a default Symfony-secret decision and live image-digest verification. |
| YouTube-DL Material configuration | `blocked` | Exact 4.3.2 control-plane state spans standalone MongoDB and allowlisted appdata. A dependable media-excluding artifact needs approved brief app downtime, a narrow lifecycle seam, and database-scoped authenticated dump access. |
| YouTube-DL media payload | `external` | Audio/video payload follows NAS/media policy and is excluded from application artifacts. |
| Speedtest Tracker | `unprotected` | Measurement history is telemetry and is deliberately excluded by the active goal; its current declaration also has no persistent volume. |

## Media and observability

| Service/scope | Current category | Evidence or remaining gap |
| --- | --- | --- |
| Jellyfin | `blocked` | The existing native plugin has only the legacy two-backup/one-restore baseline. Exact 10.11.11 source proves the archive is non-atomic across its database and copied files, every API key is unrestricted Administrator authority, and the archive omits plugins, plugin configuration, device identity, and other `/config` state. Plan 021 is stopped pending recovery-boundary/downtime, omitted-state inventory, credential-authority, and restore-claim decisions. |
| Radarr | `plugin-local` | Exact 6.3.0.10514-ls313 native backup and automatic isolated restore passed two clean A/B rounds with four fresh destinations and two-restart content persistence. Production still needs an immutable pin, broad application API key approval, fixed read-only native-backup mount, exact cleanup approval, target, schedule, and backup-only evidence. Movies/downloads remain external. |
| Sonarr | `plugin-local` | Exact 4.0.19.2979-ls320 native backup and automatic isolated restore passed two clean A/B rounds with four fresh destinations and two-restart content persistence. Production still needs an immutable pin, broad application API key approval, fixed read-only native-backup mount, exact cleanup approval, target, schedule, and backup-only evidence. Episodes/downloads remain external. |
| Lidarr | `plugin-local` | Exact 3.1.0.4875-ls38 native backup and automatic isolated restore passed two clean A/B rounds with four fresh destinations and two-restart content persistence. Production still needs an immutable pin, broad application API key approval, fixed read-only native-backup mount, exact cleanup approval, target, schedule, and backup-only evidence. Music/downloads remain external. |
| Readarr | `plugin-local` | Exact 0.4.18.2805 native backup and isolated restore passed two clean A/B drill rounds under Plan 014. Production needs an immutable image pin, narrow read-only native-backup mount, approved native-copy cleanup, target, schedule, and backup-only proof. Books and download data remain external. |
| Prowlarr | `plugin-local` | Exact 2.4.0.5397 native backup and isolated restore passed two clean A/B drill rounds under Plan 014. Production needs an immutable image pin, narrow read-only native-backup mount, approved native-copy cleanup, target, schedule, and backup-only proof; an unprivileged backend also needs readable source ownership/ACLs. |
| Plex | `blocked` | Exact 1.43.2 control-plane state requires the full Plex data directory while Plex is stopped or after a stopped atomic snapshot. Select a narrow external stop/snapshot/read-only-export/start mechanism before implementation; media payload remains excluded. |
| Audiobookshelf | `verified-plugin` | Exact 2.36.0 read-only SQLite plus bounded item/author metadata backup and two fresh exact-image restore/boot drills pass locally. Production `v0.3.3` is deployed with only the two narrow read-only control-plane mounts; target/job `17` passed its non-mutating test and backup Run/TargetRun `665` published a sidecar-discovered 21,818-byte artifact with recorded SHA-256 and no protection gap. Media remains excluded and production restore remains forbidden. |
| Calibre | `blocked` | Exact v9.11.0 has worthwhile config/library metadata, but reliable capture requires the actual library root plus coordinated quiescence of Calibre and every writer sharing `/eBooks` (including Readarr). Ebook formats and other payload remain external. |
| Bazarr | `plugin-local` | Exact v1.5.6 native online backup plus two fresh exact-image restore/boot drills pass locally under Plan 012. The plugin uses only the narrow API and read-only backup-directory mount; media and subtitle payload remain excluded. |
| Jellyseerr | `blocked` | Exact 2.7.3 state spans WAL SQLite plus credential-bearing settings with no cross-file transaction. A dependable full-config capture needs explicit brief downtime with narrow lifecycle control or an audited atomic snapshot; media/cache/logs remain excluded. |
| Jellystat | `unprotected` | Playback/statistics history is excluded telemetry; desired runtime configuration is declared in Git, so no PostgreSQL target belongs in this program. |
| Tautulli | `unprotected` | Playback/statistics history is excluded telemetry; the remaining convenience configuration does not justify a bespoke plugin in this program. |
| Wrapperr | `stateless` | Generated presentation over other service data unless contrary evidence appears. |
| Profilarr | `plugin-local` | Exact v1.1.5 SQLite plus clean, stable all-ref Git backup and two fresh exact-image restore/boot drills pass locally under Plan 013. Dirty/in-progress repositories fail closed, and the unsafe native raw-copy backup is excluded. Production needs only the two documented narrow read-only mounts. |
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
