# Synology DSM configuration export and restore research

## Decision

**Implementation is blocked.** Synology DSM 7 has a native configuration
backup: an administrator can manually export a `.dss` file and later select
which configuration groups to restore. That is the correct product boundary
for Homelab Backup; NAS datasets, package data, Docker application data,
Synology Photos content, and Hyper Backup tasks are separate concerns.

However, Synology publishes neither a supported Web API contract for creating
or restoring this export nor a specification for the `.dss` file. The official
DSM API material documents authentication and runtime API discovery, while the
configuration-backup documentation specifies only an interactive Control
Panel workflow. Automating a private DSM endpoint, an undocumented NAS command,
or browser DOM behavior would make a privileged backup depend on an interface
whose methods, response contract, and compatibility Synology does not support.

A restore cannot be proved safely on the current development host either. The
supported virtual appliance is **Virtual DSM running in Synology Virtual
Machine Manager (VMM)** on compatible Synology hardware; the development host
is an ordinary Ubuntu 24.04 x86-64 machine, and the infrastructure repository
declares no non-production Synology/VMM target. A restore against TarkilNAS is
forbidden, and creating a Virtual DSM on TarkilNAS would itself write to a
production server and would not be a restore local to the dev instance.

Under the program rule that every backup needs a locally proven restore,
do not implement or advertise a Synology DSM plugin yet. It can proceed only
after both a supported automation seam and a disposable, non-production DSM
restore appliance are available.

This research contacted no NAS or production endpoint, performed no production
read or write, and contains no credential values.

## Exact deployment evidence

The infrastructure repository declares one native NAS host, `tarkilnas`, which
is explicitly described as Synology DSM. Its exact model, DSM release/build,
package architecture, and VMM capability are **not** recorded:
[inventory](../../../homelab-infra/ansible/inventory/hosts.yaml),
[host variables](../../../homelab-infra/ansible/inventory/host_vars/tarkilnas.yaml),
[NAS playbook](../../../homelab-infra/ansible/playbooks/tarkilnas.yaml).

Homelab Backup itself runs on that NAS as two containers. The backend is pinned
to `backend-v0.2.1`, runs as root, publishes port 18080, and mounts only its own
backup directory, database directory, and the NAS Docker socket read-only. It
has no DSM configuration-export mount and no repository-declared DSM account:
[compose declaration](../../../homelab-infra/docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml).
Root inside this container and access to the Docker daemon do not confer a
supported DSM configuration-backup interface.

The broader NAS declaration includes Docker services, NFS exports, automation
scripts, monitoring, and Synology Photos indexing. Those facts establish that
DSM configuration is valuable, but they do not make all NAS state part of a
`.dss` artifact. The existing program ledger already places bulk datasets and
photos outside this application; preserve that separation.

## Supported native configuration boundary

DSM 7's Configuration Backup page says that manual export downloads a file
named from the Synology product and creation date with a `.dss` extension. The
same page defines the restore workflow: select a backup from Synology Account
or upload the saved file, select configuration groups, optionally overwrite
conflicts, and apply. DSM stops all services during application and resumes
them afterward: [DSM 7 Configuration Backup](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_configbackup?version=7).

The documented configuration groups are:

| Group | Included configuration |
| --- | --- |
| File sharing | Shared folders, file services, users and groups, Domain/LDAP |
| Connectivity | External access, network, security, terminal and SNMP |
| System | Login Portal, regional options, notifications, Update & Restore |
| Services | Application privileges, Index Service, Task Scheduler |

Important documented exclusions and restore qualifications are:

- shared-folder **definitions** are configuration, but shared-folder contents
  are not; the `homes` data must be protected separately;
- basic backup-service settings are included, but backup tasks are not;
- user-created Task Scheduler tasks are included, while application-generated
  tasks such as Hyper Backup and Storage Analyzer tasks are excluded;
- notification email recipients must be verified again after restore;
- the restoring user's password is deliberately not restored; and
- conflicting users, groups, folders, and services are overwritten only when
  that destructive option is selected.

Synology's DSM 7.2 user guide separately describes Hyper Backup as protection
for system configurations, permissions, applications, folders, files, and
LUNs. That larger boundary is not interchangeable with a configuration-only
`.dss` export: [DSM 7.2 user guide](https://global.download.synology.com/download/Document/Software/UserGuide/Os/DSM/7.2/enu/Syno_UsersGuide_NAServer_7_2_enu.pdf).

Therefore a future `synology_dsm` plugin must claim only **DSM configuration
recovery**. It must not claim to recover volumes, files, Docker binds,
containers, packages or their data, Hyper Backup/Snapshot Replication tasks,
licenses, Synology Account state, or hardware/storage-pool configuration.

## Authentication and privilege

Configuration Backup lives in Control Panel, and Synology states that only
members of the `administrators` group can access Control Panel:
[Control Panel](https://kb.synology.com/en-me/DSM/help/DSM/AdminCenter/ControlPanel_desc?version=7).
No narrower configuration-export role is documented. A future automated target
would therefore hold a broad DSM administrator credential, not a scoped backup
token.

The official login API supports account/password sessions, OTP, a device token
for later OTP omission, a session ID, and a CSRF `SynoToken`. It also says to
query `SYNO.API.Info` for the APIs and versions exposed by the target and to
log out when finished:
[DSM Login API workflow](https://kb.synology.com/en-us/DG/DSM_Login_Web_API_Guide/2),
[base APIs](https://kb.synology.com/en-id/DG/DSM_Login_Web_API_Guide/3).
Those authentication primitives do not define a configuration-backup API.
Runtime discovery can prove that a named API exists and report its path and
version range; it does not supply the undocumented export/restore method
semantics needed for dependable backup software.

If Synology publishes a supported configuration-backup API later, require:

- a dedicated DSM administrator account used only by Homelab Backup;
- verified HTTPS with an exact configured origin and no HTTP/insecure-TLS
  fallback;
- password/OTP/device/session/CSRF values stored as secrets and redacted from
  logs, API responses, sidecars, and errors;
- login and logout around every bounded operation; and
- a non-destructive `test()` that performs only supported discovery/status
  reads and proves the configured account is an administrator without
  exporting, importing, stopping services, or changing DSM.

This broad privilege is a material security cost even if a supported API
appears. The current raw Docker socket is not an acceptable substitute, and
neither root SSH nor an undocumented `syno*` executable should be added as a
fallback.

## Automation assessment

The supported manual export is a real backup workflow, but it does not meet
Homelab Backup's requirement to schedule and execute a backup unattended.

| Candidate seam | Assessment |
| --- | --- |
| Documented DSM Web API | **Unavailable for this capability.** Synology's published DSM API guide defines login/logout and discovery, not configuration export/restore. |
| Private WebAPI discovered from DSM UI | **Reject.** Discovery alone provides no documented method/parameter/result semantics, and behavior is tied to an unknown DSM build. |
| Headless browser driving Control Panel | **Reject for now.** It automates an official human workflow through an unsupported DOM/session contract, still requires a full administrator credential, and cannot be tested against an exact local DSM. |
| SSH or `syno*` command | **Reject.** No primary Synology specification was found for a supported configuration-export command; root SSH would expand privilege substantially. |
| Copy DSM internal configuration files | **Reject.** It bypasses the native coherent export, has no supported restore contract, and would be version/layout dependent. |
| DSM automatic backup to Synology Account | Useful vendor protection but not a Homelab Backup artifact. Synology documents one current file per NAS, overwrite-on-success, and deletion after 180 days without upload; Homelab Backup cannot validate or retain it independently. |
| Hyper Backup | Separate product and broader archive. It can protect configuration, packages, and data but does not supply the desired `.dss` scheduling API contract to this application. |

The negative API conclusion is intentionally narrow: it means no supported
contract was present in Synology's official configuration-backup and DSM API
documentation researched on 2026-08-16. It does not claim that the DSM web UI
uses no internal endpoint.

## Artifact and validation contract

Synology documents the filename suffix and restore workflow, but not the `.dss`
container layout, magic bytes, MIME type, checksum, schema, or offline
validation algorithm. The artifact must therefore remain opaque. Do not infer
ZIP/tar structure, extract it, edit it, or accept an unofficial parser as proof
of recoverability.

If a supported export API becomes available, a safe first implementation must:

1. stream the one response to a private temporary file with strict timeout and
   byte limits;
2. reject redirects away from the configured DSM origin, HTML/login/error
   responses, empty output, and any filename not ending in `.dss`;
3. calculate SHA-256 and byte count while streaming;
4. publish the non-empty opaque file atomically with
   `create_backup_artifact()` or `write_backup_bytes()`;
5. record source model, exact DSM version/build, API name/version, export time,
   original safe filename, byte count, SHA-256, and the documented scope and
   exclusions in the sidecar; and
6. return `artifact_path` only after artifact and sidecar publication.

These checks prove transfer integrity and provenance, **not semantic
restorability**. A `.dss` suffix, nonzero bytes, and stable hash cannot prove
that DSM will accept the file. Only a successful isolated DSM import and
state-level verification can do that. Until such a drill exists, the plugin
must not report a validated usable artifact.

Because the configuration groups include users, directory integration,
external access, security, terminal, SNMP, notifications, and application
privileges, treat every `.dss` as credential-bearing/private even though
Synology does not publish its serialization details. Do not expose its name,
contents, account names, hostnames, network configuration, or member metadata
in normal logs.

## Version pinning

The infrastructure declaration does not identify the NAS model or exact DSM
build. Synology documents checking the installed version through Update &
Restore, Info Center, or Web Assistant:
[check DSM version](https://kb.synology.com/en-id/DSM/tutorial/How_to_check_DSM_version_on_Synology_NAS).
No production check was authorized or performed for this research.

Before implementation, an explicitly approved read-only inventory step must
record:

- exact product model and package architecture;
- full DSM major/minor/patch/update/build string;
- whether VMM and Virtual DSM are supported by that model;
- the configuration-backup UI behavior for that exact build; and
- any configuration-backup API present in `SYNO.API.Info`, without invoking
  an undocumented export or restore method.

Pin the plugin to that exact observed DSM contract initially. Do not add older
endpoint names, parameter variants, private-command fallbacks, or cross-version
acceptance without primary evidence and explicit compatibility approval.

For restore, use an exact-model and exact-DSM-build disposable target until
Synology documents a wider `.dss` compatibility rule. Synology's current
migration guidance says destination DSM must be the same or newer for supported
migration methods and warns that model capabilities differ, but it does not
provide a direct `.dss` schema compatibility guarantee:
[DSM migration guidance](https://kb.synology.com/en-au/DSM/tutorial/How_to_migrate_between_Synology_NAS_DSM_6_0_and_later).

## Restore contract

The only supported restore procedure established here is manual DSM Control
Panel import:

1. provision a fresh, disposable, non-production DSM instance matching the
   recorded model capability and exact DSM build;
2. isolate its network from production and use only synthetic users, groups,
   shares, services, routes, certificates, notifications, directory endpoints,
   tasks, and credentials;
3. verify the artifact/sidecar SHA-256 and exact source-version contract before
   presenting the `.dss` file to DSM;
4. in Configuration Backup, choose restore from a computer file;
5. select only the explicitly tested configuration groups; use overwrite only
   on the fresh disposable target;
6. acknowledge that DSM stops all services, wait for them to resume, and fail
   on any import/service error; and
7. verify state through DSM itself, including users/groups, share definitions
   and permissions, file services, application privileges, safe network and
   security markers, SNMP/terminal settings, regional/login settings,
   user-created scheduler tasks, and the documented exclusions.

Never expose this as a production restore button. The restoring administrator's
password is intentionally retained, services stop during restore, and the
overwrite option changes security- and network-critical state. A manual
runbook can describe recovery, but it does not satisfy this program's build and
two-run local restore proof by itself.

## Why the local restore drill is unavailable

Synology's supported virtualization model is Virtual DSM inside VMM. The VMM
documentation says DSM images are Virtual DSM `.pat` files managed by VMM on a
Synology NAS; VMM technical specifications require a compatible Synology NAS,
more than 2 GB RAM, and Btrfs storage. Each VMM host has one free Virtual DSM
entitlement, with additional instances licensed:
[VMM image management](https://kb.synology.com/en-global/DSM/help/Virtualization/image?version=7),
[VMM technical specifications](https://www.synology.com/en-us/dsm/7.3/software_spec/vmm),
[Virtual DSM licensing](https://kb.synology.com/en-eu/DSM/help/Virtualization/license?version=7).

The current dev machine is Ubuntu 24.04, not a Synology VMM host. Synology does
not publish a supported DSM container, generic QEMU/KVM image, or emulator for
running Virtual DSM on this host. Community boot loaders or repackaged DSM
images are outside Synology's supported contract and would not prove that a
real target accepts the artifact.

A future drill is possible only if the user supplies a separate
non-production, compatible Synology NAS/VMM host (or another Synology-supported
disposable DSM environment) that is isolated from production and explicitly
authorized for destructive restores. The two-run proof must then use two
independently fresh Virtual DSM destinations and two distinct synthetic source
states/artifacts, verify exact markers and exclusions after each restore, prove
run A state does not leak into run B, and destroy the disposable instances.

Using TarkilNAS/VMM is not an acceptable workaround under the current rules:
creating images, storage, networks, licenses, or Virtual DSM instances changes
the production NAS, and the restore would execute on that production server
rather than on the local dev instance.

## Explicit STOP conditions

Stop without fallback if any of the following is true:

- any research, test, export discovery, import, restore, Virtual DSM creation,
  or cleanup would contact or modify TarkilNAS or another production component;
- the exact Synology model, DSM version/build, and supported interface cannot
  be established from approved read-only evidence;
- export or restore requires a private WebAPI method, reverse-engineered DOM,
  undocumented executable, root SSH, raw internal-file copy, disabled TLS
  verification, or an HTTP fallback;
- no dedicated administrator credential can be isolated and protected, or a
  supposedly narrower role cannot access the documented workflow;
- the response is empty, oversized, HTML/JSON error content, redirected off
  origin, ambiguously named, or cannot be published atomically with a sidecar;
- anyone proposes parsing or rewriting `.dss` internals without a Synology
  format specification, or calls transfer checks proof of restorability;
- the artifact lacks its sidecar, SHA-256, byte count, exact source model/DSM
  build, scope, or provenance;
- the restore destination is not fresh, disposable, version-matched,
  non-production, and network-isolated, or it can reach production networks,
  directory services, notification systems, certificates, shares, or users;
- a restore is proposed on TarkilNAS or any other existing system, or the
  overwrite option could affect non-synthetic state;
- VMM/Virtual DSM provisioning would occur on the production NAS under the
  current no-production-write and local-restore-only rules;
- any expected configuration marker is missing, any excluded state appears,
  any service fails to resume, or cross-run isolation fails; or
- a manual runbook, `.dss` extension, stable hash, mocked API, or unofficial DSM
  emulator is offered in place of two real isolated restore drills.

## What unblocks implementation

Both gates are required:

1. **Supported scheduled export seam:** a Synology-published configuration
   export API/CLI contract for the pinned DSM build, or an explicit user
   decision to accept and maintain a version-pinned unsupported automation
   seam. The latter is a product/reliability exception, not the default plan.
2. **Supported disposable restore target:** a separate non-production
   Synology/VMM environment reachable from the dev test harness, approved for
   destructive tests, with exact-version Virtual DSM images and no route or
   credentials to production.

Until both exist, keep the ledger row `blocked`, use DSM's manual `.dss` export
or automatic Synology Account backup as an external operational safeguard, and
do not claim Synology DSM configuration coverage in Homelab Backup.
