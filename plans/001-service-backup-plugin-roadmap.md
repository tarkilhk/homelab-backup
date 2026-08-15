# Plan 001: Establish the risk-ranked service backup plugin program

> **Executor instructions**: Complete the foundation milestone first. Before implementing any
> plugin, create a focused plan following `ADDING_PLUGINS.md`, research the
> exact deployed version and supported backup/restore boundary, and work
> test-first. Complete and commit one independently verified milestone before
> starting the next.
>
> **Drift checks**:
>
> ```bash
> git diff --stat 2d3ee9d..HEAD -- ADDING_PLUGINS.md backend/app/plugins \
>   backend/app/core/plugins docs/PLUGIN_COMPATIBILITY.md
> git -C /home/dev/projects/homelab-infra diff --stat cd3e2d6..HEAD -- \
>   .gitea/workflows ansible docker.compose files doc
> ```
>
> Refresh affected inventory rows if plugin contracts, active deployment
> entrypoints, persistent paths, or service status have drifted.

## Status

- **Priority**: P0
- **Effort**: XL program; individual plugins S–L
- **Risk**: HIGH
- **Depends on**: none
- **Category**: direction
- **Started at**: `homelab-backup` v0.2.1 (`2d3ee9d`), 2026-08-15

## Outcome and boundaries

Protect company operations first, unique personal data second, and convenience
state last. For this application, "protected" means a reachable target,
scheduled validated artifact, declared restore capability, and isolated restore
drill—not merely that a plugin directory exists. Replication and failure-domain
placement remain external infrastructure responsibilities.

No Proxmox VM-image plugin belongs in this program. Existing Proxmox jobs remain
the machine layer. Application plugins inside guests are justified only when a
portable, application-consistent export materially improves recovery.

## Key findings

- Cal.com, Invoice Ninja, Pi-hole, Vaultwarden, PostgreSQL, MySQL,
  Jellyfin, Lidarr, Radarr, and Sonarr plugins already exist.
- Both production backends were redeployed on v0.2.1 after local drills. The
  Docker-host backend now has the Jellyfin backup-directory mount.
- WordPress is no longer used and is excluded from this program even though its
  historical plugin remains in the repository.
- The old whole-`/docker-apps` rsync service is disabled after startup-copy and
  NAS permission problems (`homelab-infra/docker.compose/system.yaml:5`).
- Primary Gitea lacks a complete app-consistent backup. The NAS immutable mirror
  of one repository branch does not cover all repos, issues, PRs, LFS, releases,
  Actions state, and package metadata.
- Hermes Workspace is documented/routed but its referenced Ansible task is
  absent; confirm runtime status before planning it.
- `postgres2` is stateful but has no identified owner in active manifests.
- The HAOS sync script's quoted `LOCAL_PATH` contains a trailing space
  (`homelab-infra/files/tarkilnas/volume1/automation-scripts/sync-backup-haos.sh:8`),
  so existing HAOS backup coverage must be verified before relying on it.

## Criticality model

| Tier | Meaning | Posture |
| --- | --- | --- |
| A0 | Required to run the company, bill/schedule customers, serve public endpoints, access credentials, or deploy/recover code | Daily or better RPO, independent copy, tested restore |
| A1 | Shared control plane/storage/datastore whose loss disables A0 services | Same as A0; protect before leaf apps |
| B | Irreplaceable documents, photos, finance, automation, or accumulated knowledge | Daily/weekly by change rate; tested native/coordinated restore |
| C | Valuable but recreatable configuration/history | Weekly/monthly or documented rebuild |
| D | Stateless, cached, generated, mirrored, or telemetry data | Rebuild; normally no plugin |

## Risk-ranked service inventory

This is repository desired state, not a live runtime probe. GenBI is manual-only
and normally stopped. Commented services (`backup-docker-apps`, NetAlertX,
Meridian, GenBI ingestion), `obsolete/`, and proposed-only files are excluded.

### Company and recovery services

| Service | State at risk | Tier | Backup disposition |
| --- | --- | --- | --- |
| Invoice Ninja | Invoices, clients, payments, tax/audit history, documents | A0 | Existing `invoiceninja`; activate/drill. Optional MySQL dump is defense in depth. |
| Cal.com | Appointments, customers, scheduling/integration config | A0 | Existing `calcom`; solve DMZ DB reachability and drill. No duplicate plugin. |
| WordPress website | Retired service | D | Excluded from this program; retain historical plugin without new work. |
| Astro company site | Generated `dist`; authoritative source not identified here | A0 availability | Protect source/build repo via Gitea; archive `dist` only if authoritative. |
| Primary Gitea, Actions, OCI registry | Repos, issues/PRs, LFS, releases, hooks, CI, packages | A0/A1 | New Gitea plugin, Wave 1; runners are disposable. |
| NAS Gitea | DR mirror/control path and possible unique local state | A1 | Same Gitea plugin as a distinct target; verify mirror completeness. |
| Bitwarden Lite | Business vault PostgreSQL, files, certificates | A0 if active | New coordinated plugin only after confirming live use. |
| Vaultwarden | Credentials, orgs, attachments, sends, keys | A0 | Existing `vaultwarden` on NAS backend; activate/drill. |
| Standard Notes | Company knowledge, MySQL, uploads/object state | A0 if used for work | Wave 2 composite; omit regenerable cache. |
| Monica | CRM contacts/notes/reminders, MariaDB, files | A0 if company CRM | Wave 2 MySQL/MariaDB plus files; wrapper only if restore improves. |
| Metabase | Dashboards/questions/permissions in shared PostgreSQL | A0/A1 if company BI | Existing PostgreSQL plugin for its named DB. |
| SFTPGo | Users, keys, shares, service DB/config, client transfers | A0 if client-facing | New SFTPGo plugin, Wave 2; scope NAS payload separately. |
| Hermes gateway/UI/client-work/reminders | `~/.hermes`, sessions, memory, workspace, automation | A0 if company-critical | Wave 1 Hermes profile over constrained host-state transport; VM backup stays baseline. |
| Hermes Workspace | Unknown workspace data | Unclassified | Confirm it is actually deployed before creating a target. |
| OneCLI | Credential-broker workflows, PostgreSQL, `/app/data` | A0/A1 if agent dependency | Existing PostgreSQL plus Wave 2 file profile. |
| Airbyte | Connections, schedules, checkpoints, internal DB/PVs | A0/A1 if company pipeline | Wave 2 native/API export after use classification; VM backup remains. |
| GenBI: Prefect/JupyterHub/MinIO/Nessie/Trino/OpenMetadata | Notebooks, orchestration, lake objects/catalog, governance | A0–B by use | Classify first; use PostgreSQL now, add native stores only if authoritative. |
| Cloudflare | DNS, tunnels/routes, account rules/access metadata | A0 | New read-only export plugin, Wave 1; cloudflared clients are stateless. |
| pfSense/HAProxy/DHCP/Unbound/VPN/ACME | Network/ingress config, users, certificates | A0/A1 | Native config export, Wave 1; artifacts are sensitive and no VM-image plugin is needed. |
| Synology DSM/NFS/datasets | Company/shared data, photos, packages, app/VM backups, ACLs/tasks | A1 | DSM configuration export; bulk dataset replication remains external. |
| Homelab Backup x2 | Jobs/targets/settings SQLite, history/catalog | A1 | Wave 0 consistent self-backup to another failure domain. |
| Portainer + NAS agent | Endpoint/stack/registry settings | A1 availability, C data | Mostly Git; plugin only if rebuild exercise finds material manual state. |
| Shared PostgreSQL + `postgres2` | Multiple app DBs; unknown second-instance owner | A1 | Existing PostgreSQL per authoritative DB; inventory `postgres2`. |

### Irreplaceable and personal services

| Service | Tier | Disposition |
| --- | --- | --- |
| Paperless-ngx | B | Wave 3 native document export plus independent artifact copy. |
| Immich | B | Wave 3 coordinated PostgreSQL + assets; RAID/same-NAS copies are not backup. |
| Home Assistant, Mosquitto, ESPHome, HACS, AdGuard | B | Wave 3 Supervisor-native backup; verify current HAOS-to-NAS sync. |
| Firefly III | B | Wave 3 MariaDB + uploads. |
| Sure | B | Wave 3 PostgreSQL + Rails storage. |
| Speakr | B if work, else C | Wave 2 if commercial; otherwise Wave 3. |
| Hindsight | B/C | Wave 3 PostgreSQL + encrypted restricted files. |
| Quartz | B if company knowledge, else C | Host-state profile for authoritative content. |
| Termix | B/C | Native/file export with strict secret handling. |
| Mealie, Wallabag | C | PostgreSQL/files or native/SQLite-safe export. |
| YouTube-DL Material | C | MongoDB/config after higher waves; media follows NAS policy. |
| Speedtest Tracker | C/low | Fix missing persistence before any plugin. |

### Media and observability

| Service group | Tier | Disposition |
| --- | --- | --- |
| Jellyfin; Radarr/Sonarr/Lidarr | C | Existing plugins; make paths reachable and drill. |
| Readarr/Prowlarr | C | Wave 4 thin, researched Servarr subclasses. |
| Plex, Audiobookshelf, Calibre, Bazarr, Jellyseerr/Jellystat, Tautulli, Wrapperr, Profilarr, Tracearr, Maloja/Multi-Scrobbler | C | Wave 4 native or consistency-aware config backup; media is separate. |
| Transmission, Flood, CleanupArr, Houndarr | C/D | Low-priority config; queues/caches generally rebuildable. |
| Grafana | C | API export only for UI state not already provisioned in Git. |
| Prometheus/Mimir/Loki | D by default | Skip unless history has explicit business/compliance value. |
| Exporters, Alloy, Telegraf, rsyslog, MCPs, Shield forwarder | D | No plugin; desired config is in Git. |

## Exact container inventory

All helper/init/sidecar service keys remain listed for completeness. There are
160 declared service keys across these active entrypoints.

| Entrypoint | Service keys |
| --- | --- |
| `system` | `adminer`, `backend`, `frontend`, `cloudflared`, `fail2ban`, `fail2ban-http`, `logcli`, `postgres`, `postgres2`, `renovate-scheduler`, `watchtower` |
| `work` | Invoice Ninja: `invoiceninja`, `invoiceninja-mysql`, `invoiceninja-nginx`; Monica: `monica`, `monica-cron`, `monica-mariadb`; WordPress: `wordpress_db`, `wordpress_www`, `phpmyadmin`; `metabase`; Standard Notes: `server`, `web`, `db`, `cache`, `localstack` |
| `misc` | `mealie`, `sftpgo`, `ytdl_material`, `ytdl-mongo-db`; Paperless: `webserver`, `broker`, `gotenberg`, `tika`; `speedtest-tracker`; Immich: `database`, `immich-server`, `immich-machine-learning`, `immich-folder-album-creator`, `immich-power-tools`; Firefly: `firefly-db`, `firefly-app`, `firefly-cron`; `wallabag`, `iperf3`, `termix`; Hindsight: `hindsight-db`, `hindsight-migrate`, `hindsight`; Sure: `sure-db`, `sure-redis`, `sure-web`, `sure-worker` |
| `media` | `audiobookshelf`, `calibre`, `readarr`, `jellyseerr`, `jellystat`, `jellyfin`, `jellyfin-wrapped`, `plex`, `tautulli`, `wrapperr`, `prowlarr`, `transmission`, `cleanuparr`, `flood`, `houndarr`, `lidarr`, `radarr`, `sonarr`, `bazarr`, `profilarr`, `tracearr-db`, `tracearr-redis`, `tracearr`, `maloja`, `multi-scrobbler` |
| `monitoring` | `grafana`, `prometheus`, `mimir`, `loki`, `alloy`, `telegraf`, `rsyslog`, `node-exporter`, `pfsense-exporter`, `pve-exporter`, `adguard-exporter`, `blackbox-exporter`, `tasmota-exporter`, `mcp-grafana`, `shield-log-forwarder` |
| `gitea` | `gitea`, `runner_1` through `runner_5`, `mcp-gitea` |
| `bitwarden` | `bitwarden`, `db`, `adminer` |
| `genbi` (manual) | `minio`, `minio-init`, `nessie`, `trino`, `prefect-db-setup`, `prefect-server`, `prefect-worker`, `elasticsearch`, `postgresql`, `execute-migrate-all`, `openmetadata-server`, `openmetadata-ingestion`, `genbi-jupyterhub-postgres`, `genbi-jupyterhub-singleuser`, `jupyterhub` |
| `dmz` | `astro-site`, `postgres`, `calcom-db-setup`, `calcom`, `cloudflared`, `dmz-alloy`, `dmz-telegraf`, `dozzle`, `watchtower`, `dmz-nginx`, `dmz-fail2ban`, `dmz-fail2ban-nginx`, `speakr`, `speakr-runpod-whisperx`, `whisperx-adapter` |
| `claw` | `claw-alloy`, `dozzle`, `telegraf`, `onecli-postgres`, `onecli`, `searxng-valkey`, `searxng`, `firecrawl-redis`, `firecrawl-rabbitmq`, `firecrawl-postgres`, `firecrawl-playwright`, `firecrawl-api` |
| `tarkilnas-system` | `openssh-server`, `watchtower`, `vaultwarden`, `gitea-nas`, `runner`, `mcp-gitea-nas`, `snmp-exporter`, `tarkilnas-alloy`, `telegraf`, `pihole`, `pihole6-logs-exporter`, `pihole6-metrics-exporter`, Homelab Backup `backend`, `frontend` |

## Non-container/external inventory

| Host/scope | Services |
| --- | --- |
| `pihole-vm` | Pi-hole FTL, metrics/log exporters, Node Exporter, Alloy |
| `claw` | Hermes gateway/dashboard, client-work/reminder units, Hermes UI, Quartz, reverse tunnel/firewall, Node Exporter; Hermes Workspace unconfirmed |
| `claw-sre` | OpenClaw/Codex automation, workspace/memory, Alloy, Node Exporter |
| `airbyte` | Airbyte via `abctl`/Kind and Alloy |
| `haos` | Home Assistant OS, Mosquitto, ESPHome, HACS, Advanced SSH, documented AdGuard, monitoring add-ons |
| `pfsense` | Routing, firewall/NAT, DHCP, Unbound, HAProxy, VPN, ACME, pfREST, Node Exporter/syslog |
| `tarkilnas` native | DSM, NFS/shared folders, Synology Photos/indexing, automation, SNMP/Log Center |
| `docker` host | Docker Engine, Portainer EE, reverse-tunnel clients, NFS mounts, host exporters/timers |
| `dev` | Dev VM, Docker, Codex/Cursor tooling, AppArmor sandbox, Node Exporter |
| `nuc`, `minis` | Proxmox VE, user-managed scheduled VM backups, NFS storage, exporters/LVM metrics/syslog diagnostics |
| External | Cloudflare, optional RunPod WhisperX, Shield TV, Tasmota/ESPHome devices |

## Recommended waves

### Foundation: make plugin development repeatable

1. Repair `ADDING_PLUGINS.md`, `AGENTS.md`, and compatibility documentation so
   they describe the live artifact, test, restore, API, and venv contracts.
2. Add repository-hygiene regression checks for those contributor contracts.
3. Convert this inventory into one focused plan per plugin milestone.
4. Classify ambiguous services before selecting their backup boundary:
   Bitwarden Lite, `postgres2`, Hermes Workspace, Airbyte/GenBI, and Astro source.

Exit when the authoring contract is test-protected and the next plugin has a
version-pinned, service-specific plan.

### Wave 1: Company control planes and public edge

1. Gitea native dump with repository/LFS/attachment/release validation and an
   explicit package-registry policy; target both Gitea instances. Decide the
   documented consistency downtime policy before any production rollout.
2. Consistent Homelab Backup self-backup for both instances.
3. Constrained host-state transport plus Hermes profile: allowlisted paths,
   atomic archive, checksums, quiesce policy, secret handling; no shell field.
4. Cloudflare read-only zone/DNS/tunnel/rules/access export.
5. pfSense config/version/package export.
6. Synology DSM configuration export.
7. Bitwarden Lite only if confirmed authoritative.

### Wave 2: Complete company application bundles

1. Standard Notes: MySQL plus uploads/object state.
2. SFTPGo: users/config/keys plus explicitly scoped client data.
3. Monica if company CRM: MariaDB plus storage.
4. OneCLI: PostgreSQL plus `/app/data`.
5. Speakr if commercial: native or consistent uploads/instance bundle.
6. Airbyte/GenBI only after classification; prefer native/API and existing
   PostgreSQL coverage before new datastore plugins.

### Wave 3: Irreplaceable personal data

1. Paperless native export.
2. Immich coordinated DB/assets.
3. Home Assistant Supervisor-native backup.
4. Firefly III and Sure finance bundles.
5. Hindsight, Quartz, Termix, Mealie, Wallabag by confirmed value.
6. MongoDB/YTDL only after NAS dataset policy is clear.

### Wave 4: Convenience state

1. Thin Readarr/Prowlarr Servarr extensions.
2. Native/config-safe Plex, Audiobookshelf, Jellyseerr/Jellystat, Tautulli,
   Bazarr, Profilarr, Tracearr, Maloja, and related coverage where worthwhile.
3. Grafana UI export if Git provisioning is incomplete.
4. Skip telemetry history, queues/caches, and generated state unless a new
   business/compliance requirement says otherwise.

## Plugin contract and verification

Every extracted plugin plan must follow `ADDING_PLUGINS.md`: flat schema,
tests-first mocked external I/O, `test()` returning `True` only on success and
raising useful exceptions otherwise, atomic non-empty artifact under
`/backups/<target_slug>/<YYYY-MM-DD>/`, mandatory sidecar, declared restore
capability, isolated restore drill, secret-safe logs, and minimum deployment
access. Do not add legacy aliases/formats/fallbacks without explicit approval.

| Purpose | Command | Expected |
| --- | --- | --- |
| Focused tests | `cd backend && .venv/bin/pytest -q tests/<plugin-tests>` | Exit 0 |
| Backend tests | `cd backend && .venv/bin/pytest -q` | Exit 0 |
| Type check | `cd backend && .venv/bin/mypy app tests` | Exit 0 |
| Formatting/imports | `cd backend && .venv/bin/black --check app tests && .venv/bin/isort --check-only app tests` | Exit 0 |
| Frontend if changed | `cd frontend && npm test -- --run && npm run lint && npm run build` | Exit 0 |

## Program done criteria

- [ ] Every deployed service is protected, explicitly covered by accepted
      machine/storage recovery, reproducible/stateless, or accepted unprotected.
- [ ] Named company apps, active credential stores, Gitea, website source,
      Cloudflare, pfSense, Hermes/OneCLI company state, and shared DBs show
      current protection evidence.
- [ ] Every automatic/partial restore path passed against an isolated target at
      the deployed major version; no production restore was used.
- [ ] No Proxmox VM-image plugin was created.
- [ ] No cache/stateless service received a bespoke plugin without a documented
      new retention requirement.

## STOP conditions

Stop rather than improvise if access requires unrestricted
shell/filesystem/Docker privileges; the supported restore boundary is unknown;
DB and file/object state cannot be made consistent; a drill would touch
production; production consistency requires downtime that has not been
approved; compatibility behavior is needed without approval; or live inventory
contradicts the repository.

## Maintenance

Refresh this inventory whenever Compose includes, Ansible hosts/services,
persistent mounts, or SaaS edge components change. Treat all artifacts as
sensitive. Keep service export and bulk offsite replication separate: they
solve different failures. Reject raw live copies
of PostgreSQL/MySQL/SQLite/OpenSearch data directories unless the application
explicitly documents that method as consistent.
