# Home Assistant OS full-backup boundary and restore proof

## Decision

**Implementation is blocked** under this program's strict cross-component
consistency requirement until a bounded quiescence/downtime design is
explicitly approved. After that decision, the supported data-plane boundary is
a Home Assistant OS target using a dedicated Home Assistant **administrator**
long-lived access token over verified TLS. It proxies Supervisor operations
through Home Assistant's WebSocket API and downloads the finished archive
through Home Assistant's authenticated HTTP proxy. Never copy
`SUPERVISOR_TOKEN` out of Home Assistant, and do not use root SSH or the HAOS
Docker socket for backup creation.

Create a password-protected, compressed **full** Supervisor backup with `homeassistant_exclude_database: false`. Run it in the background, poll the returned Supervisor job to completion, require an empty job error list, obtain the slug from the job `reference`, and validate the full backup manifest before downloading its raw `.tar`. Publish that byte stream with the repository's atomic artifact helper. This is feasible without mounting HAOS storage into Homelab Backup.

Restore capability is operationally **isolated-only/manual**: prove the artifact with the two-run disposable-VM drill below, but expose no normal production restore action. A full restore is destructive and is forbidden against the deployed HAOS instance.

This research made no calls to, and performed no writes or restores on, the production Home Assistant system.

## Exact deployed topology (repository evidence)

The deployment repository identifies the target as Home Assistant OS at `haos.tarkilnetwork`: [inventory](../../../homelab-infra/ansible/inventory/hosts.yaml), [HAOS setup](../../../homelab-infra/doc/homeassistant/haos-setup.md). Its documented Supervisor-managed applications include Mosquitto Broker, Advanced SSH & Web Terminal, ESPHome, Node Exporter, and AdGuard Home; HACS is also documented, although HACS itself is a custom integration rather than a continuously running app: [setup](../../../homelab-infra/doc/homeassistant/haos-setup.md), [monitoring](../../../homelab-infra/doc/homeassistant/haos-monitoring.md), [HACS installation model](https://www.hacs.xyz/docs/use/download/download/).

The same repository also creates host-level state that is **not** managed by Supervisor: root secrets under `/root/secrets`, a direct Docker `alloy` container, and its named volume. The playbook explicitly distinguishes the Advanced SSH app's container namespace from the HAOS host namespace: [HAOS playbook](../../../homelab-infra/ansible/playbooks/homeassistant.yaml). Those facts are decisive exclusions from the full-backup boundary.

Homelab Backup runs on TarkilNAS and currently mounts only its own `/backups`, database, and a read-only NAS Docker socket; it has neither an HAOS filesystem mount nor a Supervisor credential: [compose file](../../../homelab-infra/docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml). A legacy NAS script rsyncs HAOS `/backup/` over root SSH, and an NFS export exists for a HAOS backup directory: [sync script](../../../homelab-infra/files/tarkilnas/volume1/automation-scripts/sync-backup-haos.sh), [exports](../../../homelab-infra/files/tarkilnas/etc/exports). That copy path does not create or attest a coherent backup and is not the proposed plugin contract.

The infrastructure repository does not pin or record the live HAOS, Core, Supervisor, or app versions. It also labels HACS imprecisely. Runtime versions and exact installed app slugs therefore remain an implementation-time **read-only preflight**, not assumptions in this plan.

## What a full Supervisor backup contains

Home Assistant's user documentation says a full backup contains the `config`, `share`, `addons`, `ssl`, and `media` directories and all installed apps; `addons` here is the source directory for manually installed/created apps, not a second copy of store app source. It also states that backups are encrypted compressed tar archives stored locally under `/backup` by default: [Home Assistant backup and restore](https://www.home-assistant.io/common-tasks/general/), [full-backup action](https://www.home-assistant.io/actions/hassio.backup_full/).

Current Supervisor source makes the boundary precise: a full backup selects every installed app, all standard folders, and Home Assistant Core; it records repositories and Supervisor configuration as well: [backup manager](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/manager.py), [standard folder set](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/validate.py), [backup implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py).

| State | Included? | Exact boundary |
| --- | --- | --- |
| Home Assistant Core | Yes | The complete Core configuration directory, including YAML, `.storage`, custom integrations, dashboards/themes/www, and the recorder database when `homeassistant_exclude_database` is false. Core excludes transient/corrupt logs and caches, nested backups, SQLite `-shm`, and similar generated files; excluding the database adds the recorder DB/WAL exclusions: [Core backup orchestration](https://github.com/home-assistant/core/blob/dev/homeassistant/components/hassio/backup.py), [Supervisor-to-Core backup implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/homeassistant/module.py). |
| HACS | Yes, through Core config | HACS is a custom integration and stores its data in Home Assistant `.storage`; HACS explicitly states that its data is part of a regular Home Assistant backup: [HACS data documentation](https://www.hacs.xyz/docs/use/data/). Downloaded custom components and frontend assets also live under Core config. A temporary/current “Get HACS” app is included only if runtime preflight says it remains installed. |
| Installed Supervisor apps | Yes | Every app that is installed when the backup starts is selected. Each app archive contains its recorded version and persisted user/system options, app data, AppArmor profile, and any app-specific config directory. A locally built app also carries its image export; a store app's image/source is not embedded and must be fetched on restore: [app backup implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/app.py), [backup archive implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py). |
| Mosquitto | Yes if installed | Its app data/options are included; its mapped `share` and `ssl` state are also covered by the standard folders. Its current official manifest declares no backup override, so Supervisor's default `hot` mode applies: [Mosquitto manifest](https://github.com/home-assistant/addons/blob/master/mosquitto/config.yaml), [Supervisor app schema](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/validate.py). |
| ESPHome | Yes if installed | App data/options and the Home Assistant config mapping are covered. The official app manifest deliberately excludes nested app-data build/cache directories with `backup_exclude`; the ESPHome YAML under mapped Home Assistant config remains in the Core backup: [ESPHome manifest](https://github.com/esphome/home-assistant-addon/blob/main/esphome/config.yaml). It has no backup-mode override, so the default is hot. |
| AdGuard Home | Yes if installed | App data/options are covered, but the official manifest excludes `*/adguard/data/querylog.*`; query history matching that pattern is intentionally not recoverable. The manifest has no backup-mode override, so the default is hot: [AdGuard Home manifest](https://github.com/hassio-addons/addon-adguard-home/blob/main/adguard/config.yaml). |
| Other installed apps | Yes if installed | Advanced SSH, Node Exporter, and any other runtime-installed Supervisor app are included with the same app-data/config rules. The manifest, not the display name, determines exclusions and hot/cold behavior. |
| Shared folders | Yes, with a boundary | `share`, local `addons`, `ssl`, and `media` are copied. Content below Supervisor bind-mounted network paths is deliberately skipped; the mount definitions themselves are separate Supervisor config: [folder backup implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py). |
| Supervisor config | Partly | App repository URLs plus Supervisor network-mount definitions (including their credentials) and configured Docker registries are stored in an encrypted inner archive: [Supervisor config implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py). |
| HAOS host/boot state | No | This is not a disk image. The OS image, boot slots, host packages/tweaks, root SSH keys, `/root/secrets`, unmanaged containers, Docker volumes, and host logs are outside the full-backup archive. |
| External systems and hardware | No | Network-share contents, external databases/services, MQTT clients, ESP devices/firmware already flashed to devices, DNS clients, USB/radio hardware, and other machines are not captured. Store app/Core container images are resolved again during restore. |

The resulting archive is credential-bearing even when password-protected: Core config can contain users/tokens/secrets; Mosquitto and other apps can contain credentials; and Supervisor mount/registry definitions may contain secrets. The backup password and Home Assistant token must be independently secret, never logged or returned in API errors, and the downloaded tar must retain private storage permissions.

## Safe trigger and read contract

### Authentication and transport

Use a dedicated Home Assistant user that is an administrator and create a long-lived access token from that user's profile. Home Assistant accepts it as `Authorization: Bearer ...`; long-lived tokens are broad user credentials and currently last ten years, so this is not a narrowly scoped backup credential: [Home Assistant authentication API](https://developers.home-assistant.io/docs/auth_api/). Store and redact it like a password.

The Supervisor API itself requires `SUPERVISOR_TOKEN`, which is supplied only to Home Assistant and apps. Official developer documentation warns that the token can change after restart/update. Do not extract or persist it: [Supervisor endpoints](https://developers.home-assistant.io/docs/api/supervisor/endpoints/), [Supervisor development](https://developers.home-assistant.io/docs/supervisor/development/). Home Assistant Core's `supervisor/api` WebSocket command substitutes its internal token and requires the external Home Assistant user to be an administrator for these endpoints: [Core WebSocket proxy source](https://github.com/home-assistant/core/blob/dev/homeassistant/components/hassio/websocket_api.py), [WebSocket authentication protocol](https://developers.home-assistant.io/docs/api/websocket/).

Require HTTPS with ordinary hostname and certificate verification. The deployment repository demonstrates configured `/ssl` material but does not prove the exact certificate SAN/base URL. Do not inherit its root-SSH `StrictHostKeyChecking=no` posture or silently fall back to HTTP/insecure TLS.

### Read-only `test()`

After WebSocket authentication, proxy only these reads:

1. `GET /supervisor/info`: require healthy, supported Supervisor and record Supervisor version/architecture.
2. `GET /info`: record the installed Core, Supervisor, HAOS, machine, and architecture values.
3. `GET /backups/info`: prove backup API reachability without creating anything.
4. `GET /addons`: capture the exact installed app slugs, names, versions, state, and architecture compatibility.

`test()` succeeds only after all reads complete and must map authentication, authorization, TLS, connectivity, timeout, unsupported-installation, and malformed-response failures to specific redacted errors. It must perform no backup, cleanup, restart, or restore.

### `backup()` state machine

The supported Supervisor endpoint `POST /backups/new/full` accepts `name`, `password`, `compressed`, `location`, `homeassistant_exclude_database`, and `background`; background clients must inspect the job for status and slug: [Supervisor backup endpoint](https://developers.home-assistant.io/docs/api/supervisor/endpoints/). Send it via Home Assistant WebSocket `supervisor/api` with:

- a collision-resistant name owned by this job;
- a required, non-empty backup password;
- `compressed: true`;
- `location: null` for Supervisor's local `/backup` unless a separately validated Supervisor backup mount is deliberately configured;
- `homeassistant_exclude_database: false`;
- `background: true`.

Then:

1. Save the returned `job_id`; poll `GET /jobs/<job_id>` with bounded backoff and an overall timeout.
2. Require `done: true`, `errors: []`, job name `backup_manager_full_backup`, and a non-empty `reference` (the backup slug). Supervisor documents the Job fields, including `errors`; current source assigns the created slug to the job reference: [Job model](https://developers.home-assistant.io/docs/api/supervisor/models), [backup manager source](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/manager.py).
3. Read `GET /backups/<slug>/info`. Require the exact requested name/slug, `type: full`, `protected: true`, `compressed: true`, nonzero size, Home Assistant present with database exclusion false, all four standard folders, and an app-slug set equal to the installed-set snapshot taken immediately before the trigger. Record Core, Supervisor, app versions, repositories, size, date, and hash in sidecar metadata.
4. Download the raw archive from `GET /api/hassio/backups/<slug>/download` using the same Home Assistant administrator bearer token. Core's route requires an administrator for backup info/download/restore paths and streams the Supervisor response: [Core HTTP proxy source](https://github.com/home-assistant/core/blob/dev/homeassistant/components/hassio/http.py). Stream to the repository artifact helper; enforce byte and time limits, require the downloaded byte count to match API metadata, calculate SHA-256 while streaming, and never extract the archive into the application filesystem.
5. Return `artifact_path` only after the atomic artifact and sidecar publication completes.
6. Optional source cleanup may issue `DELETE /backups/<slug>` through `supervisor/api` **only** for the exact slug created by this invocation and only after publication. If cleanup fails, retain the HAOS copy and return/report a redacted cleanup warning; never delete a pre-existing backup. Do not turn the legacy rsync path into a fallback.

This validation is mandatory because Supervisor writes app and folder components sequentially and captures some component failures on the job while still allowing the outer archive to finish. Fatal archive errors remove the incomplete file, but a merely present tar is not proof that every requested component succeeded: [backup manager](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/manager.py), [component error handling](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py).

## Consistency and availability

Supervisor serializes full-backup creation, enters its `freeze` state, calls Home Assistant's `backup/start` hook, and copies Core, apps, folders, and Supervisor config sequentially before finalizing and registering the archive. Home Assistant's hook lets Core prepare/resume, but normal archive creation does not globally stop Core: [manager sequence](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/manager.py), [Core hook implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/homeassistant/module.py).

Each app controls its own consistency. A `cold` app is stopped and later restarted; a `hot` app stays running unless it defines pre/post backup commands. Supervisor's default is hot: [app backup implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/app.py), [app backup schema](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/validate.py). The current Mosquitto, ESPHome, and AdGuard manifests do not override that default or declare pre/post backup hooks. Their files may change while copied.

Consequences:

- Expect no global backup outage for the currently identified apps, though a future cold-mode app can have brief per-app downtime and Supervisor waits for restarts.
- A full archive is **not one transactional point-in-time snapshot across Core plus every app**. It is a coordinated, sequential backup with component-specific semantics.
- Run during a low-activity window. The isolated drill must specifically exercise Mosquitto retained state, ESPHome source config, and AdGuard configuration rather than assuming tar presence is consistency proof.
- If the requirement is strict cross-component transactional consistency, stop. Achieving it would require an explicitly approved downtime/quiesce design; the backup API's `/freeze` and `/thaw` endpoints are documented for an external image/VM snapshot, not as a replacement recipe for Supervisor's archive workflow.

## Restore contract

A restore may consume only an artifact produced and validated by this target, with its sidecar, SHA-256, password, version manifest, and synthetic/provenance marker intact. Home Assistant supports uploading a backup during fresh-device onboarding and says the target may be different hardware, must have more free space than the used space on the source, and can take roughly 45 minutes or more; the UI becomes unavailable and original credentials apply afterward: [restore documentation](https://www.home-assistant.io/common-tasks/general/).

For a full restore, current Supervisor source verifies the password/archive and refuses a backup made by a newer Supervisor than the target. It requires free space, healthy/running state, and host/system internet; then it freezes Supervisor, tears down Core and apps, removes apps not in the backup, restores repositories/apps/folders/Core/Supervisor config, installs the recorded Core/app versions as needed, and starts Core again: [full restore implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/manager.py), [component restore implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/backups/backup.py).

Therefore the supported recovery runbook is:

1. Provision a fresh, disposable/approved HAOS target with compatible architecture and a Supervisor version at least as new as the recorded backup version.
2. Give it enough disk headroom and only the controlled internet access required to fetch the recorded Core/store-app images and repositories.
3. Upload and restore through the official fresh-install onboarding flow, supplying the backup password. Do not call the deployed system's restore endpoint.
4. Wait for Core and all expected apps to become healthy. Log in with the restored credentials, not the fresh target's pre-restore credentials.
5. Verify exact app slug/version/state, Core version, representative state markers, folders, HACS, MQTT, ESPHome, and AdGuard behavior. A completed Supervisor job alone is insufficient because component restore methods can return partial failure; preserve logs and mark the drill failed on any discrepancy.

## Isolated two-run restore drill

The drill uses only synthetic state. No production artifact, token, secret, DNS name, IP, MAC, database, device, radio, certificate, or MQTT message may enter it.

### Isolation fixture

- Create one disposable HAOS `amd64` source VM and two independently provisioned pristine HAOS target VMs; do not clone a target from the source data disk.
- Put them on a dedicated network that denies RFC1918/ULA/link-local access and explicitly blocks production DNS, mDNS/SSDP, MQTT, DNS service traffic, and the production HAOS host. Permit host access to the test UI/API and narrowly controlled DNS/HTTPS egress only for HAOS, Core, app, HACS, and repository downloads.
- Use unique synthetic administrator/app credentials and a unique backup password. Attach no production USB, Bluetooth, Zigbee/Z-Wave, serial, ESP, storage, or network mounts.
- Install the representative runtime set: Mosquitto, ESPHome, AdGuard Home, Advanced SSH, Node Exporter, and HACS following current official installation. Record exact slugs and versions from the source API; do not assume the optional Get HACS app remains installed after setup.

### Run A

1. Seed unique `A` markers: a Home Assistant helper/config entry and recorder state, dummy secret, `.storage`/HACS repository state, one file in each standard folder, an ESPHome YAML device stub, Mosquitto persistence plus a retained synthetic topic, and AdGuard client/filter configuration. Generate disposable SSL material only.
2. Trigger backup A through the proposed target. Require the full job/manifest/download/hash checks above and prove the password decrypts it in the isolated fixture.
3. Restore A through onboarding to pristine target A.
4. Verify Core/API health, recorded Core/Supervisor/app versions, exact expected app set and running state, original synthetic login, every A marker, HACS repository visibility, retrieval of the retained MQTT value, the ESPHome YAML, and AdGuard filter/client behavior. Verify documented exclusions: no AdGuard query-log marker and no ESPHome generated build-cache marker. Verify there is no route or traffic to production.

### Run B

1. On the source VM, change the primary marker from A to B, add a B-only marker, remove an A-only marker, update the retained MQTT payload and ESPHome/AdGuard test config, and produce a new recorder state.
2. Trigger backup B independently. Require a different slug/name/date/SHA-256 and demonstrate that no cached or overwritten A artifact was returned.
3. Restore B to independently pristine target B. Require every B value and deletion to be reflected, require the A-only marker to remain absent, and re-run all health/exclusion/isolation checks.
4. As a cross-run assertion, target A must contain no B-only marker. Preserve the redacted test report and hashes; dispose of the synthetic VMs and temporary artifacts according to the test environment's cleanup policy.

Passing means both independent backup/restore runs recover their exact expected state, every expected app is healthy, intentional exclusions stay excluded, and no production path was reachable. Anything less is a failed restore proof.

## Explicit STOP conditions

Stop without fallback or destructive action if any of the following is true:

- A configured host, resolved address, certificate identity, machine ID, MAC, token, artifact, credential, mount, or device belongs to production during research or the restore drill; any request would reach `haos.tarkilnetwork` or another private production service.
- The endpoint is not HAOS/Supervised, Supervisor is unsupported/unhealthy, exact live versions and installed slugs cannot be read, or the runtime add-on/HACS model contradicts this document. Re-research the pinned deployed versions before implementation.
- The credential is not a dedicated Home Assistant administrator token, TLS hostname/certificate verification fails, or completion would require HTTP, disabled verification, root SSH, a Docker socket, extracting `SUPERVISOR_TOKEN`, or preserving the legacy rsync route as fallback.
- Another backup/restore job is active, Supervisor is not running, free space is insufficient, the selected backup mount is absent/down, or the operation exceeds its bounded timeout.
- The backup password is empty, `homeassistant_exclude_database` is not exactly false, the job ID/slug/name is not owned by this invocation, or the job is missing, stalled, not done, wrong type, or has any error.
- Backup info is not full/protected/compressed, is zero-sized, omits Home Assistant, reports database exclusion, omits any standard folder, or its app set differs from the immediate preflight snapshot. Do not publish a partial-success tar.
- The download is truncated, oversized, redirects off the configured origin, disagrees with metadata, fails tar/metadata validation, contains unsafe member paths, or its sidecar/hash cannot be published atomically. Do not return `artifact_path`.
- Strict single-point-in-time consistency is required. The identified apps use hot backup; obtain explicit approval for a downtime/quiesce design before changing that contract.
- A restore target is not fresh, disposable, and network-isolated; can route to production/private networks; has production hardware/radios mounted; lacks disk headroom, image/repository availability, the password, provenance, or a Supervisor version at least as new as the backup.
- A full restore is proposed on an existing or production system. Full restore removes delta apps and overwrites Core and folder state. Production restores are forbidden.
- Any restore job/component fails, any expected marker/app/version is missing, an excluded marker unexpectedly returns, or A/B isolation is violated. Do not report restore capability as proven.
- Cleanup cannot prove the exact source slug was created by this invocation or local artifact publication has not completed. Leave the HAOS backup in place and report the cleanup failure; never broaden deletion.

## Version caveat

Official documentation and the linked `main`/`dev` sources describe the current contract as researched on 2026-08-15. Because the deployment repository does not pin the live Home Assistant stack, implementation must capture the actual HAOS/Core/Supervisor/app versions through read-only preflight and revalidate any version-sensitive behavior against the matching official tag. Do not add legacy endpoint aliases, snapshot terminology, insecure fallback transports, or compatibility shims without explicit approval.
