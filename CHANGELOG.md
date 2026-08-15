# Changelog

All notable changes to Homelab Backup are recorded here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A version-pinned Gitea 1.27.1 plugin with consistent native dumps, bounded
  Docker transfers, isolated labeled-destination restores, and verified rollback.
- A Homelab Backup self-backup plugin with consistent online SQLite snapshots,
  private versioned artifacts, and strict create-only offline restores verified
  by two isolated exact-image boots.

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

[Unreleased]: https://github.com/tarkilhk/homelab-backup/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/tarkilhk/homelab-backup/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tarkilhk/homelab-backup/releases/tag/v0.2.0
