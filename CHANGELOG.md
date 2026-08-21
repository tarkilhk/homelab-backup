# Changelog

All notable changes to Homelab Backup are recorded here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.3] - 2026-08-21

### Fixed

- Treat absent Audiobookshelf `metadata/items` and `metadata/authors`
  directories as empty native state when the read-only metadata root itself is
  present, while retaining strict failure for a missing root or unsafe entries.

## [0.3.2] - 2026-08-21

### Fixed

- Accept Audiobookshelf 2.36.0 databases upgraded through Sequelize migrations
  only when their optional `SequelizeMeta` table has the exact native schema.

## [0.3.1] - 2026-08-21

### Fixed

- Make release tests derive plugin discovery versions from the installed
  package and preserve private permissions in the archive-tamper fixture.

## [0.3.0] - 2026-08-21

### Added

- A version-pinned Gitea 1.27.1 plugin with consistent native dumps, bounded
  Docker transfers, isolated labeled-destination restores, and verified rollback.
- A Homelab Backup self-backup plugin with consistent online SQLite snapshots,
  private versioned artifacts, and strict create-only offline restores verified
  by two isolated exact-image boots.
- A SFTPGo 2.7.5 control-plane plugin with credential-free online SQLite
  snapshots, transient-state scrubbing, and create-only restores verified by
  two fresh exact-image boots.
- A Termix 2.3.2 encrypted-state plugin with read-only stable snapshots,
  private strict artifacts, create-only local restores, and two authenticated
  exact-image recovery drills.
- An Audiobookshelf 2.36.0 control-plane plugin with read-only online SQLite
  snapshots, bounded native metadata capture, private strict artifacts, and two
  create-only exact-image recovery drills; audiobook and ebook media stay out
  of scope.
- A Hindsight 0.8.6 plugin with least-privileged PostgreSQL 18 logical dumps,
  exact full-TOC validation, and transactional create-only restores verified by
  two fresh exact-image boots and restarts. Backup attempts snapshot only their
  non-secret database identity, and restore execution is disabled outside an
  explicitly authorized isolated drill.
- A Bazarr 1.5.6/LinuxServer ls349 control-plane plugin using the native online
  SQLite backup, strict attribution and artifact validation, structural sidecar
  evidence, and RestoreService-staged create-only local restores verified by
  two exact-image boots and restarts. Media and subtitle payloads remain
  separate recovery prerequisites.
- A version-pinned Profilarr 1.1.5 plugin that combines a live SQLite snapshot
  with a stable, self-contained all-ref Git bundle, rejects unsettled repository
  state, publishes a private strict composite artifact, and reconstructs all
  authoritative application state through a create-only local restore. Radarr,
  Sonarr, Git hosting, credentials, and production restore remain outside its
  boundary.
- Exact Readarr 0.4.18.2805 and Prowlarr 2.4.0.5397 control-plane plugins that
  create native backups through the API, stream them from narrow read-only
  backup mounts, validate and publish them privately, clean up only the
  attributed native copy, and prove isolated upload/restart recovery in two
  clean exact-image drill rounds. Books, download data, and external services
  remain separate prerequisites.
- Exact Radarr 6.3.0.10514-ls313, Sonarr 4.0.19.2979-ls320, and Lidarr
  3.1.0.4875-ls38 control-plane adapters over the hardened Servarr module, with
  fixed read-only native-backup mounts, strict bounded archive validation,
  structural sidecars, exact post-publication cleanup, and twelve fresh local
  restores proving content through two restart cycles across two clean rounds.
  Media and download payloads remain separate recovery prerequisites.
- A hardened exact Invoice Ninja 5.13.31 native export/import contract with
  strict signed-download and archive validation, private transactional
  artifacts, fresh local-only RestoreService imports, application-level marker
  checks, and two clean exact-image drill rounds. Restore remains honestly
  partial because the vendor importer does not reliably recover embedded
  document bytes into a fresh private destination.
- A strict PostgreSQL 16 named-database recovery module with denied-write source
  probes, private bounded custom archives, exact catalog/TOC provenance,
  descriptor-bound transactional restores, and two clean PostgreSQL 16.14
  backup-to-fresh-restore drill rounds.
- A strict Cal.com 6.2.0 adapter over the PostgreSQL 16 recovery module with
  exact migration/catalog/control-plane profiles, stable online A/B archives,
  fresh transactional RestoreService destinations, and exact-image boot/restart
  proof in two clean rounds. Restore remains `partial` because application
  configuration, encryption keys, external providers, and lifecycle are external.

### Changed

- Sidecars now bind every newly written artifact to its byte size and SHA-256
  digest. Artifacts created by older versions without both fields are rejected
  by validation and restore; create fresh backups before upgrading.
- Generic Oracle MySQL recovery is explicitly classified as a legacy-only,
  blocked contract after exact MySQL Shell 8.4.0 testing returned zero while
  warning that consistency could not be guaranteed with the proposed
  schema-scoped identity. Broader privileges or a proven quiescence boundary
  require an explicit decision before implementation resumes.
- Pi-hole Teleporter recovery is explicitly classified as legacy-only and
  decision-gated after exact 2026.07.2 source review proved sequential,
  non-atomic export and no endpoint-scoped read-only credential. A consistency
  policy and source-authority policy require explicit approval before current-
  contract implementation resumes.
- Jellyfin recovery is explicitly classified as legacy-only and decision-gated
  after exact 10.11.11 source review proved the native database/file archive is
  not one coherent snapshot, omits plugin/device and other `/config` state,
  requires unrestricted Administrator authority, and has no terminal or
  rollback-aware restore result. Boundary/downtime, omitted-state inventory,
  credential-authority, and restore-claim decisions are required before
  current-contract implementation resumes.

## [0.2.1] - 2026-08-15

### Fixed

- Remove Jellyfin's server-side staging archive after the validated central
  artifact and sidecar are committed, preventing unbounded staging growth.

## [0.2.0] - 2026-08-15

### Added

- A canonical SemVer release version, synchronized package metadata, and
  immutable versioned container tags.
- Backup protection status, Prometheus metrics, and truthful run outcomes for
  missing, overdue, skipped, partial, and failed backups.
- Recovery sidecars and isolated restore workflows for the supported plugins.
- Repeatable compatibility drills for all eleven homelab backup plugins.

### Changed

- Hardened plugin backup validation, process timeouts, overlap handling, and
  memory usage against the component versions deployed in the homelab.
- Made the backend image run as an unprivileged user by default.

### Fixed

- Reconciled interrupted jobs and eliminated successful runs with no matching
  target-run records.
- Repaired Pi-hole v6 authentication and current database, Servarr, Jellyfin,
  Invoice Ninja, Vaultwarden, and WordPress backup behavior.

[Unreleased]: https://github.com/tarkilhk/homelab-backup/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/tarkilhk/homelab-backup/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/tarkilhk/homelab-backup/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/tarkilhk/homelab-backup/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/tarkilhk/homelab-backup/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/tarkilhk/homelab-backup/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tarkilhk/homelab-backup/releases/tag/v0.2.0
