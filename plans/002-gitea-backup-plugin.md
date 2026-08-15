# Plan 002: Gitea 1.27.1 backup and isolated restore

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation contract
- **State**: TODO
- **Production status**: BLOCKED pending explicit downtime and constrained
  primary-host execution approval; isolated local implementation is authorized
- **Researched at**: `homelab-backup` v0.2.1 and `homelab-infra`
  `eeed77a`, 2026-08-15

## Outcome

Add one `gitea` plugin for the primary and NAS deployments. It must create a
consistent native dump containing the database, repositories, LFS,
attachments, releases, configuration, and package data, then restore and prove
that dump against a labeled isolated local Gitea destination.

This milestone ends after local implementation, two local drills, repository
checks, review, and a focused commit. Production deployment is a separate gate:
no production restore is permitted, and a production backup cannot stop Gitea
until the user explicitly approves the downtime policy.

## Verified deployment facts

- Primary: `gitea/gitea:1.27.1`, container `gitea`, `/docker-apps/gitea/data`
  mounted at `/data`; package blobs are a second mount at
  `/data/gitea/packages`.
- NAS: `gitea/gitea:1.27.1`, container `gitea-nas`,
  `/volume1/docker/gitea/data` mounted at `/data`.
- Both declarations use the image's SQLite layout. Runners and MCP servers are
  reproducible and are not part of the artifact.
- The v0.2.1 NAS backup backend has read-only Docker-socket access. The primary
  backend has no Docker socket and is not attached to the Gitea network.
- The exact Gitea image includes `/usr/local/bin/gitea`, `/usr/bin/sqlite3`, and
  `/usr/bin/unzip`, and runs application data as UID/GID 1000 (`git`).

Authoritative declarations:

- `homelab-infra/docker.compose/gitea/gitea/gitea.yaml`
- `homelab-infra/docker.compose/tarkilnas-system/gitea/gitea.yaml`
- both Homelab Backup deployment manifests under their respective
  `homelab-backup/` directories

## Vendor contract

Gitea 1.27 documents `gitea dump` as the complete ZIP export and states that the
instance must be shut down during backup to keep its database, repositories,
and files consistent. Package data is included unless `--skip-package-data` is
set. Gitea has no automatic recovery command; the documented restore extracts
data/repositories, imports `gitea-db.sql`, fixes ownership, regenerates hooks,
and starts the service.

Primary sources:

- <https://docs.gitea.com/1.27/administration/backup-and-restore>
- <https://docs.gitea.com/1.27/administration/command-line#dump>

## Public contract

### Schema

Keep the target schema flat:

- `container_name`: required exact local Docker container name
- `allow_service_stop`: required boolean acknowledgement; backup refuses unless
  true
- `timeout_seconds`: bounded integer with a conservative default

Do not expose arbitrary image names, commands, paths, shell fragments, Docker
URLs, or restore destinations. Derive the exact image and `/data` mount from the
inspected target container.

### Connectivity

`test()` is non-destructive and must:

1. inspect the exact container through the local Docker socket;
2. require a running, healthy `gitea/gitea:1.27.1` container;
3. require an exact `/data` mount and SQLite configuration;
4. prove `/api/healthz` from inside the declared container without logging
   configuration or environment secrets;
5. raise a specific user-facing exception for every failure.

### Backup

The backup transaction must:

1. refuse unless `allow_service_stop` is true;
2. stop the target with a bounded timeout and confirm it stopped;
3. create a short-lived helper from the target's exact image with the target
   volumes mounted read-only;
4. run as `git` in a writable helper temp directory:
   `gitea dump -c /data/gitea/conf/app.ini --skip-log`;
5. stream the resulting ZIP through `create_backup_artifact()` without loading
   it into memory;
6. validate ZIP integrity and required database, repository, configuration,
   attachment/LFS, and package layout before publication;
7. remove the helper and restart the original container in `finally`;
8. wait for the original health check and `/api/healthz` before reporting
   success.

Cancellation, timeout, dump failure, archive validation failure, helper cleanup
failure, and restart failure must be independently tested. A failed backup must
leave no final artifact or sidecar and must never leave the original service
silently stopped.

### Restore

Declare `restore_capability = "automatic"` only if the full isolated workflow
passes. Restore must refuse any destination lacking the Docker label
`asia.hollinger.homelab-backup.restore-destination=true`; production manifests
must not carry this label.

For a labeled destination, the restore transaction must:

1. require the same exact Gitea image, SQLite layout, and `/data` mount;
2. validate the artifact before any destination mutation;
3. stop the destination and capture a streaming rollback archive of `/data`;
4. stage and safely extract the Gitea dump, rejecting links, traversal, special
   files, excessive members, and incompatible layout;
5. replace the documented data/config/repository paths and import
   `gitea-db.sql` into a fresh SQLite database;
6. set UID/GID 1000 ownership and regenerate repository hooks;
7. start the destination and prove health plus restored marker content;
8. on any failure, stop the destination, restore the rollback archive, restart,
   and prove its prior health before returning failure.

The helper lifetime must cover the whole operation; every Docker call and child
command needs a deadline and confirmed termination before rollback or lock
release.

## Test-first slices

1. Discovery, schema, `restore_capability`, and invalid config.
2. Docker inspection and exact-version/SQLite/mount/health validation.
3. Consistent stop → helper dump → streamed artifact → restart success.
4. Vendor-specific archive rejection and atomic cleanup.
5. Restart guarantee for every backup failure boundary.
6. Restore-label, archive-safety, and empty/fresh destination guards.
7. Successful SQLite/data/repository restore and hook regeneration.
8. Restore rollback for each destructive boundary.
9. Timeout, cancellation, secret-redaction, and same-container concurrency.
10. API discovery/schema/test coverage.

Use Docker protocol fakes for deterministic unit tests. Use the real
`gitea/gitea:1.27.1` image only for isolated integration drills.

## Local drill evidence

Run two consecutive drills. Each creates source marker data spanning at least a
repository commit, issue/comment, release attachment or LFS object, and package
blob where the local API supports it. Each drill must prove:

- unique non-empty ZIP and sidecar;
- independently calculated size and SHA-256;
- expected dump members and valid SQL;
- source marker equality after restore into a fresh labeled destination;
- repository clone/fsck and API visibility;
- application health after restore;
- source service health after every backup attempt;
- bounded peak memory while streaming the artifact.

## Commands

```bash
cd backend
.venv/bin/pytest -q tests/plugins/test_gitea_plugin.py
.venv/bin/pytest -q
.venv/bin/mypy app tests
.venv/bin/black --check app tests
.venv/bin/isort --check-only app tests
```

Also run the frontend suite, lint, and build because the new schema appears in
the Targets UI. Inspect the built backend image for the required Docker client
path before the drills.

## Production gate

Local completion does not authorize deployment changes. Before production
backup validation, ask for decisions on:

1. the allowed maintenance window and maximum stop duration;
2. the constrained execution path for primary Gitea, because its backup backend
   currently lacks Docker access;
Package blobs are authoritative primary Gitea state and are included in every
artifact. Large artifact size does not weaken that policy; streaming and bounded
memory are required instead.

After explicit deployment, production validation is limited to connectivity and
user-approved backup triggers. Production restore remains forbidden.

## Done criteria

- [ ] All ten test-first slices pass.
- [ ] Both local drills satisfy every evidence item.
- [ ] Full backend and frontend checks pass.
- [ ] Code review has no unresolved P0/P1 findings.
- [ ] Compatibility documentation records the exact version and evidence.
- [ ] The milestone is committed independently.

## STOP conditions

Stop rather than weaken consistency if the exact dump layout differs from the
documented restore boundary; the package mount cannot be captured read-only;
rollback cannot be proven; the service cannot be restarted after a failure;
Docker access would need to expand in production; or production downtime has
not been explicitly approved.
