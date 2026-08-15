# pfSense native configuration export and isolated restore proof

## Decision and feasibility

Use pfSense's native, password-encrypted **full configuration XML download** from Diagnostics → Backup & Restore. Do not use pfREST as the export mechanism: the deployed pfREST version exposes configuration-history metadata, not the XML contents. The native export is semantically read-only, requires no firewall/service downtime, and includes pfSense configuration plus package configuration when “Skip packages” is false.

The operation is technically feasible through an authenticated webConfigurator session and its CSRF-protected `POST /diag_backup.php` form. There is, however, an authorization caveat that must be accepted before implementation: pfSense's narrowest page privilege is `page-diagnostics-backup-restore`, which grants the same page's destructive restore function. Adding `User - Config: Deny Config Write` is good defense in depth, but current source does not conclusively apply it to the full-restore `config_install()` path. If that residual restore authority is unacceptable, stop; pfREST 2.8.3 provides no safer raw-config GET endpoint.

The target should expose no production restore action and declare `restore_capability = "manual"`. Its restore proof is a create-only, manual drill against a newly provisioned, isolated pfSense VM built entirely from synthetic configuration. No production backup is restored into the lab.

This research performed no request, write, backup, or restore against the deployed firewall.

## Exact deployed evidence and version gap

The infrastructure repository declares:

| Item | Repository evidence | Conclusion |
| --- | --- | --- |
| Host | [`pfsense.tarkilnetwork`](../../../homelab-infra/ansible/inventory/hosts.yaml) | Canonical production identity. The restore lab must block this name and all resolved addresses. |
| pfREST URL | [`https://pfsense:10443`](../../../homelab-infra/ansible/inventory/host_vars/pfsense.yaml) | pfREST/webConfigurator is served on HTTPS port 10443. The short hostname is not sufficient evidence of certificate identity. |
| pfREST release | [`pfsense_pfrest_release_tag: "v2.8.3"`](../../../homelab-infra/ansible/inventory/host_vars/pfsense.yaml) | Exact desired pfREST package is v2.8.3 (`pkg` version `2.8_3`). The installer enforces that pin: [installer script](../../../homelab-infra/files/pfsense/scripts/ensure_pfrest_installed.sh). The upstream release resolves to commit `6b9375c839b856e51f0fcf0a8bd138849a1ef10b`: [pfREST v2.8.3 release](https://github.com/pfrest/pfSense-pkg-RESTAPI/releases/tag/v2.8.3). |
| Existing auth | HAProxy automation and monitoring use `X-API-Key`; both currently disable certificate validation: [HAProxy defaults](../../../homelab-infra/ansible/roles/pfsense_haproxy/defaults/main.yaml), [exporter config](../../../homelab-infra/ansible/playbooks/templates/pfsense-exporter-config.yaml.j2). | Do not reuse the shared write-capable key or inherit `validate_certs: false`. |
| Existing host authority | An `ansible` user has shell/root escalation and deploys scripts, modifies `rc.newwanip`, and installs pfREST: [setup](../../../homelab-infra/doc/networking/pfsense-ansible-setup.md), [playbook](../../../homelab-infra/ansible/playbooks/pfsense.yaml). | Host scripts/tweaks are outside native `config.xml`; root SSH is not an acceptable backup boundary. |
| Captured Swagger | [`files/pfsense/pfrest-swagger.json`](../../../homelab-infra/files/pfsense/pfrest-swagger.json) says v2.7.1. | This file is stale relative to the v2.8.3 deployment pin and must not drive implementation. |

The repository does **not** record the installed pfSense edition (CE or Plus), release, architecture, build time, or current `config.xml` schema revision. The pfREST v2.8.3 source supports CE 2.8.1 and specific Plus releases, but that does not prove which one is deployed: [v2.8.3 support matrix](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/docs/INSTALL_AND_CONFIG.md). Exact runtime version is therefore an implementation-time read-only preflight and a restore-drill gate.

For a dedicated least-privilege pfREST key, `GET /api/v2/system/version` reads `/etc/version`, patch, and build-time files without mutation: [v2.8.3 endpoint](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Endpoints/SystemVersionEndpoint.inc), [model](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Models/SystemVersion.inc). `GET /api/v2/system/packages` reads installed package metadata including `installed_version`: [packages endpoint](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Endpoints/SystemPackagesEndpoint.inc), [package model](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Models/Package.inc). Grant only `api-v2-system-version-get` and `api-v2-system-packages-get` to that key's dedicated user, filter the returned packages in memory for RESTAPI, and require `installed_version == 2.8_3`. Do not grant either endpoint's separate mutation privileges. Do not call `/api/v2/system/restapi/version`: the deployment documents a release-cache failure on that endpoint, while the desired pfREST version is already pinned in source control: [deployed failure record](../../../homelab-infra/doc/monitoring/pfsense-pfrest-exporter.md).

## Why pfREST is not the export boundary

pfREST v2.8.3 offers `GET /api/v2/diagnostics/config_history/revision` and the plural equivalent. Its exact model returns only `time`, `description`, configuration `version`, and `filesize`; its internal callable wraps pfSense `get_backups()` and never reads or returns the XML file: [v2.8.3 model](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Models/ConfigHistoryRevision.inc), [singular endpoint](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Endpoints/DiagnosticsConfigHistoryRevisionEndpoint.inc). The same endpoints also support `DELETE`, so granting their delete privilege would be actively harmful.

The pfREST command-prompt endpoint could execute a shell command to read `/conf/config.xml`, but it is a `POST` arbitrary-command capability rather than a read-only export contract. It is strictly broader than the native page and is rejected.

## Native export contract

Netgate documents Diagnostics → Backup & Restore → “Download Configuration as XML” as the canonical local backup. A full backup is the default; package settings are included unless “Skip Packages” is selected, while RRD, volatile extra data, SSH keys, and encryption are explicit options: [Netgate backup documentation](https://docs.netgate.com/pfsense/en/latest/backup/configuration.html). pfSense stores authoritative settings in `/conf/config.xml`: [XML configuration documentation](https://docs.netgate.com/pfsense/en/latest/config/xml-configuration-file.html).

Current Netgate source defines the exact page and handler:

- `diag_backup.php` requires `page-diagnostics-backup-restore`, presents the native options, and dispatches POST data to `execPost()`: [page source](https://github.com/pfsense/pfsense/blob/master/src/usr/local/www/diag_backup.php).
- A full export with packages included reads `/conf/config.xml` directly. It optionally appends volatile data, RRD data, and SSH host keys, then optionally encrypts and returns the download: [backup handler](https://github.com/pfsense/pfsense/blob/master/src/usr/local/pfSense/include/www/backup.inc).
- Modern CE 2.7.0+/Plus 22.05+ encryption uses salted AES-256-CBC, PBKDF2-HMAC-SHA256, and 500,000 iterations, wrapped between `---- BEGIN config.xml ----` and `---- END config.xml ----`: [Netgate restore/decryption documentation](https://docs.netgate.com/pfsense/en/latest/backup/restore.html), [encryption source](https://github.com/pfsense/pfsense/blob/master/src/etc/inc/crypt.inc).

Use a cookie-preserving HTTPS session and the CSRF token emitted by the exact deployed page. Submit only the exact-version form fields for:

- `download` set, with no restore/reinstall/clear/apply field;
- empty `backuparea` (all configuration areas);
- no `nopackages` field (retain installed-package metadata/settings);
- `donotbackuprrd` set (omit monitoring history);
- no `backupdata` field (omit volatile Captive Portal/DHCP lease databases);
- `backupssh` set (preserve SSH host identity used by the deployed automation);
- `encrypt` set, with a required non-empty `encrypt_password` and matching `encrypt_password_confirm`.

The implementation must first fetch and parse the deployed form rather than assume old third-party examples. If its required fields or CSRF flow differ, stop and revalidate against the matching Netgate source tag; do not add legacy `Submit=download` aliases or an unencrypted fallback.

### State boundary

| State | Included? | Exact boundary |
| --- | --- | --- |
| pfSense configuration | Yes | Full `<pfsense>` XML: interfaces/VLANs, firewall/NAT/aliases, routing, DHCP static configuration, Unbound, users/privileges, certificates/private keys, VPN, ACME, HAProxy and other configured services. |
| Package configuration | Yes | `installedpackages` metadata and settings are retained because `nopackages` is absent. Package binaries and repository payloads are not embedded and must be reinstalled. |
| pfREST configuration | Yes, as package config | Settings, access lists, and stored API-key hashes live under the RESTAPI entry in `installedpackages`; raw API keys are not recoverable because pfREST stores only hashes: [v2.8.3 key model](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Models/RESTAPIKey.inc), [settings model](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/pfSense-pkg-RESTAPI/files/usr/local/pkg/RESTAPI/Models/RESTAPISettings.inc). The optional “Keep Backup” file at `/usr/local/share/pfSense-pkg-RESTAPI/backup.json` is a separate package-upgrade persistence copy outside `config.xml`; the v2.8.3 binary and that file are not embedded. |
| SSH host identity | Yes | `backupssh` embeds compressed/base64 SSH public and private host keys inside the XML before the whole artifact is encrypted. |
| RRD graphs | No | Deliberately omitted; Netgate notes that RRD data significantly enlarges backups. Central monitoring is authoritative for history. |
| Volatile extra data | No | Captive Portal databases, used-voucher databases, and DHCP lease databases are excluded. Static DHCP mappings remain in config. |
| Host/package runtime state | No | FreeBSD/pfSense OS image, package executables, live firewall state table, logs, DHCP live leases, process state, `/usr/local/sbin/rah_scripts`, the modified `/etc/rc.newwanip`, `/etc/rah`, and other Ansible-created host files are outside `config.xml`. |
| External state | No | ACME authority state outside config, external DNS/cloud accounts, VPN peers, clients, certificates copied to NAS/HAOS, external package repositories, and network data are not backed up. |

This artifact is a high-value secret container. Even without raw pfREST keys it contains password hashes, VPN/ACME material, TLS and SSH private keys, shared secrets, network topology, firewall policy, user data, and package credentials. Keep only the password-encrypted server response; never publish plaintext XML.

## Authentication and privilege contract

Create a dedicated local pfSense user that is not `admin`, is not in `admins`, has no shell privilege, and has only:

- `Diagnostics: Backup & Restore` (`page-diagnostics-backup-restore`);
- `User - Config: Deny Config Write` (`user-config-readonly`) as defense in depth;
- no package, command-prompt, SSH, all-pages, restore-history, or configuration-history delete privilege.

Use a long random webConfigurator password over verified HTTPS. Restrict the webConfigurator firewall rule to the Homelab Backup source address and management interface; do not expose it on WAN. pfREST's own interface/access-list controls apply to pfREST endpoints, not as a substitute for a webConfigurator firewall rule.

The limitation is explicit: `page-diagnostics-backup-restore` is a combined page privilege marked with pfSense's root-level warning. Current source makes `write_config()` honor `user-config-readonly`, but the full restore path calls `config_install()`, which directly validates and replaces `config.xml` and does not itself check that privilege: [write guard](https://github.com/pfsense/pfsense/blob/master/src/etc/inc/config.lib.inc), [restore handler](https://github.com/pfsense/pfsense/blob/master/src/usr/local/pfSense/include/www/backup.inc). The plugin can be behaviorally read-only, but the credential cannot be proven cryptographically or by privilege separation to be download-only.

For exact pfSense and installed-pfREST version metadata, use a **separate** dedicated pfREST API key whose user has only `api-v2-system-version-get` and `api-v2-system-packages-get`. pfREST recommends API keys for automation, states that keys inherit their issuing user's privileges, and recommends dedicated least-privilege users and restricted interfaces/access lists: [v2.8.3 authentication](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/docs/AUTHENTICATION_AND_AUTHORIZATION.md), [security guidance](https://github.com/pfrest/pfSense-pkg-RESTAPI/blob/v2.8.3/docs/SECURING_API_ACCESS.md). Do not reuse the deployed HAProxy/exporter key because its effective privileges are not recorded and HAProxy automation performs writes.

## Read-only `test()` and backup state machine

`test()` must perform only:

1. TLS connection with normal chain and hostname validation, no redirects off the configured origin.
2. `GET /diag_backup.php`, login with the dedicated local user, retain only an in-memory cookie jar, and confirm the authenticated page contains the expected privilege-bound backup form and CSRF token.
3. With the separate least-privilege pfREST key, `GET /api/v2/system/version` and `GET /api/v2/system/packages`; require the returned edition/release/build to be in pfREST v2.8.3's support matrix and the installed RESTAPI package to be exactly `2.8_3`. These endpoints advertise separate method-specific privileges; grant/call only GET, never their POST/DELETE capabilities.

`backup()` then creates a fresh authenticated/CSRF session, sends exactly the encrypted full-export POST described above, and streams the response to the repository's atomic artifact helper. It returns `artifact_path` only after all validation and sidecar publication succeed. It does not create, modify, or delete configuration history on pfSense.

Map DNS, TCP, TLS, login, CSRF, privilege, unsupported version, wrong content type, HTML response, size limit, timeout, encryption, and XML validation failures to specific redacted exceptions. Never include a username, password, API key, cookie, CSRF value, configuration fragment, hostname/domain from the artifact, or encryption password in logs/errors.

## Artifact validation and secret handling

Before publication, require:

1. HTTP success from the same origin, an attachment filename matching the native `config-<hostname>.<domain>-<timestamp>.xml` shape, a bounded nonzero length, and no HTML/login/error body.
2. Exactly one native begin marker and end marker with bounded base64 between them. Reject trailing/leading unexpected content.
3. In-memory decryption with the configured password and the algorithm for the exact runtime release. Do not put the password in process arguments/environment or write decrypted XML to disk; if using OpenSSL, pass the password via a private file descriptor and consume plaintext through a pipe.
4. Hardened XML parsing with DTD/entities forbidden; root exactly `<pfsense>`; expected hostname/domain; numeric configuration schema version and revision timestamp; full-system sections; expected `installedpackages` entries including RESTAPI; `sshdata` present; no `rrddata` or embedded volatile `*data/xmldatafile` payloads.
5. Configuration schema revision compatible with the runtime release. Netgate lists CE 2.8.1 as config revision 24.0 and explains that this number—not merely product version—governs restore compatibility: [version/config-revision table](https://docs.netgate.com/pfsense/en/latest/releases/versions.html), [restore compatibility](https://docs.netgate.com/pfsense/en/latest/backup/restore.html).
6. SHA-256 and exact byte count of the still-encrypted artifact. The sidecar may contain the non-secret runtime release/build, pfREST desired version, config schema revision, revision timestamp, export option booleans, size, and hash—never decrypted values or configuration excerpts.

Zero/deallocate plaintext and session material on a best-effort basis, close/logout the session, and delete any failed staging artifact. The backup encryption password must be distinct from webConfigurator/API credentials and escrowed separately; losing it makes restore impossible.

## Consistency and downtime

No downtime is needed. The native handler reads the active `/conf/config.xml`, optionally reads SSH key files, encrypts the result, and sends it to the client; it does not call a reload, restart, reboot, or configuration write: [backup handler](https://github.com/pfsense/pfsense/blob/master/src/usr/local/pfSense/include/www/backup.inc). pfSense writes `config.xml` through an fsynced temporary file and atomic rename in current source, so the reader should observe a complete old or new XML file rather than a partially overwritten file: [atomic write source](https://github.com/pfsense/pfsense/blob/master/src/etc/inc/config.lib.inc).

This is configuration consistency, not a runtime-state snapshot. Firewall states, leases, logs, RRD, running package memory, and external peers are deliberately out of scope. SSH keys are separate static files and are read after the config; a simultaneous SSH host-key rotation could cross versions. Run in a low-change window, validate one XML revision, and stop/retry if the firewall's revision changes during any optional before/after read-only check. Do not stop routing, DNS, DHCP, VPN, HAProxy, pfREST, or the firewall for backup.

## Create-only isolated restore drill

The drill is manual and destructive only to a newly created disposable target VM. It never uploads to, restores, reboots, or writes the deployed firewall.

1. Complete the production **read-only version preflight only**. Select an official pfSense installer for exactly the same edition/release when available. If the deployed system is CE 2.8.1, use CE 2.8.1/config revision 24.0 and pfREST v2.8.3. If it is Plus, use an authorized matching Plus lab image/entitlement; do not assume CE↔Plus equivalence. Netgate allows older full configs to upgrade forward but rejects newer configs on older revisions and warns that CE/Plus and hardware differences can matter: [different-version restore](https://docs.netgate.com/pfsense/en/latest/backup/restore-different-version.html), [restore compatibility](https://docs.netgate.com/pfsense/en/latest/backup/restore.html).
2. Create a synthetic source VM on a lab-only virtual switch with the same NIC count/order/model planned for the target. WAN must be disconnected or restricted to allowlisted public package sources; LAN management is reachable only from the test runner. Deny production RFC1918/ULA ranges, production DNS, VPN peers, CARP, syslog, ACME, SMTP, SSH/SCP destinations, and `pfsense.tarkilnetwork` at the hypervisor boundary.
3. Install exact pfREST v2.8.3 from the pinned release asset after independently recording and verifying its SHA-256, plus representative packages. Seed only synthetic markers across interfaces/VLANs, aliases/firewall/NAT, DHCP static mapping, Unbound override, HAProxy, a disabled/synthetic VPN, ACME/certificate objects, local users, package settings, a dedicated pfREST key, and SSH host keys. Use reserved documentation IPs/domains and disposable secrets.
4. Export through the proposed plugin and pass all encryption/XML/manifest/hash checks. Power off the source VM before bringing up the restored target, preventing duplicate LAN IPs or DHCP services even inside the lab.
5. Create a second, pristine target VM—never an existing appliance—with identical virtual NIC order on a separate deny-production lab switch. Install the same pfSense edition/release, then use the official GUI full-restore flow with the synthetic encrypted artifact/password. Netgate documents that full GUI restore applies the configuration and reboots: [restore procedure](https://docs.netgate.com/pfsense/en/latest/backup/restore.html). Maintain console access because restored interface names/IPs can make the GUI unreachable.
6. Reinstall exact package binaries from controlled sources after reboot. pfREST is unofficial and the deployed Ansible explicitly reinstalls it after OS upgrades; verify v2.8.3 before testing its restored settings/key. Confirm host-only Ansible scripts and `rc.newwanip` patch are absent, as the boundary predicts.
7. Prove the exact config schema revision, hostname, interface assignment, firewall/NAT/aliases, DHCP/Unbound, HAProxy, users/privileges, certificates, VPN/ACME synthetic objects, SSH host-key fingerprint, package settings, and working least-privilege pfREST key. Prove RRD/leases/runtime state were not restored. Capture packet logs showing no production destination was reachable.
8. Preserve only a redacted report, artifact hash, exact release/config revision, and expected-vs-observed checklist. Destroy the synthetic VMs/artifacts under the lab cleanup policy.

The drill passes only if a fresh appliance boots after the native full restore, every in-scope synthetic marker is correct, every intentional exclusion remains excluded, package reconciliation is understood, and no production traffic/credential/artifact was involved.

## Explicit STOP conditions

Stop without fallback if any condition below occurs:

- Any research/drill request would reach `pfsense.tarkilnetwork`, its resolved address, a production interface, credential, certificate, backup, VPN peer, ACME account, syslog target, or other production service. No production restore/write is permitted.
- The installed pfSense edition/release/build, architecture, config schema revision, or pfREST package version cannot be established read-only; pfREST is not exactly the pinned v2.8.3; or the running version is outside v2.8.3's support matrix.
- The native backup form/CSRF fields differ from the exact source contract. Do not scrape optimistically, use legacy form aliases, fall back to SSH/command prompt, or emit an unencrypted backup.
- The user is `admin`, belongs to `admins`, has shell/all-pages/command privileges, or the version-read key has more than `api-v2-system-version-get` and `api-v2-system-packages-get`. Do not reuse the broad deployed HAProxy/exporter key.
- The combined `page-diagnostics-backup-restore` credential risk has not been explicitly accepted, source-IP access cannot be restricted, or the platform cannot guarantee that the plugin sends only the backup fields. The read-only privilege does not prove full restore is impossible.
- HTTPS certificate/hostname validation fails, the endpoint redirects off-origin, or completion would require the deployment's current `validate_certs: false` behavior.
- The POST includes any restore, apply, reinstall, clear, history delete, command, or mutation field; any code path invokes `config_install`, config history restore/delete, pfREST mutation, reboot, or service control.
- The artifact is empty, HTML, unencrypted, malformed, oversized, not a full `<pfsense>` config, wrong hostname/revision, missing package/SSH data, unexpectedly contains RRD/volatile data, cannot decrypt, contains a DTD/entity, or cannot be atomically published with a matching hash/sidecar.
- Plaintext XML, passwords, keys, cookies, CSRF values, configuration fragments, or decrypted metadata would be written to disk/logs/errors; the encryption password is unavailable or identical to an access credential.
- Backup is proposed to require routing/DNS/DHCP/VPN/HAProxy downtime. Native export needs none; investigate rather than quiesce the production firewall.
- The restore target is not newly created, disposable, console-accessible, and deny-production isolated; source and target would share a live segment; real WAN/CARP/VPN/ACME/DHCP/syslog/SCP traffic is possible; or any production artifact/secret is proposed for the lab.
- The target is older than the artifact config revision, changes CE/Plus without an approved supported plan, lacks matching NIC assignments, or exact required package binaries (including pfREST v2.8.3) cannot be obtained and verified.
- Post-restore validation differs from the manifest, an expected in-scope marker/package is missing, an excluded state reappears, or packet capture shows any production reachability. Do not claim restore proof.

## Version-coupling rule

This note was researched on 2026-08-15 against the deployed pfREST pin v2.8.3 and current official Netgate documentation/source. The deployment's exact pfSense release remains intentionally unknown because no production call was allowed. Before implementation, capture it through the read-only version endpoint, pin the matching official source/behavior in tests, and update this note. Implement one clean exact-version form contract; do not add legacy fields, insecure TLS behavior, SSH/root fallbacks, or compatibility shims without explicit approval.
