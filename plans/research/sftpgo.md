# SFTPGo v2.7.5 backup and restore research

Research date: 2026-08-15  
Scope: the SFTPGo deployment declared in `homelab-infra`, the exact upstream
SFTPGo v2.7.5 source, and a disposable exact-image local probe. No production
endpoint or host was contacted.

## Decision summary

Protect SFTPGo's authoritative SQLite data-provider state with an online SQLite
snapshot read through a dedicated read-only bind mount. Restore the standalone
database only into a fresh sentinel-marked directory, then boot and verify an
isolated exact-version SFTPGo container.

This boundary covers administrators, users and their public keys, groups,
virtual folders, shares, API keys, roles, IP lists, event rules/actions, quotas,
and provider-managed configuration. The snapshot is transactionally consistent,
requires no SFTPGo credential or downtime, and never writes to the source.

The native `dumpdata` API was considered and rejected for this deployment. In
v2.7.5 it requires the wildcard `*` super-administrator permission, reads object
collections sequentially without an outer transaction, and would require both a
new backend network attachment and a changed SFTPGo allowed-host declaration.
Those privileges and weaker consistency are unnecessary when both services run
on the same Docker VM.

Client file bytes, NAS media, `/srv/sftpgo`, generated SSH host keys, declarative
container configuration, logs, sessions, active transfers, task locks, and
Defender history are outside the first plugin. SFTP, FTP, and WebDAV are disabled
in the deployed profile, so host-key continuity is not currently operative. If
SFTP or writable client homes are enabled later, reassess this boundary rather
than silently claiming those bytes are protected.

## Exact deployment and source mapping

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/misc/sftpgo/sftpgo.yaml`. It declares
`drakkan/sftpgo:v2.7.5-alpine`, runs as UID/GID `1000:1000`, and mounts:

| Container path | Host source | Declared role |
| --- | --- | --- |
| `/srv/sftpgo` | `/docker-apps/sftpgo/data` | User homes and provider-generated exports |
| `/var/lib/sftpgo` | `/docker-apps/sftpgo/config` | SQLite provider DB, working directory, generated host keys |
| five `/nas/*` paths | NAS media trees | Read-only virtual-folder payloads |

No data-provider override is declared. Exact v2.7.5 defaults to SQLite database
`sftpgo.db`; a relative database name is resolved under the configuration
directory ([configuration defaults](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/config/config.go#L338-L343),
[SQLite path resolution](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/sqlite.go#L230-L249)).
The deployed authoritative database is therefore
`/var/lib/sftpgo/sftpgo.db`.

The upstream v2.7.5 release resolves to commit
[`9888a3d169aed9011ae6e4f7a97ae735c1643068`](https://github.com/drakkan/sftpgo/commit/9888a3d169aed9011ae6e4f7a97ae735c1643068).
On 2026-08-15, the public Alpine tag resolved to OCI index digest
`sha256:d1e2877600aba270ac395bf76fc7c8a2a0bb4ac83c3e6c180a0540f5d4c3efb2`
and Linux/amd64 image digest
`sha256:9738588bea6d46a33448b4428fbae9221cd720f8bfccddecc3271855e3aec617`.
The infrastructure declaration is tag-only, so those registry values prove the
drill input, not which historical bytes an existing production host pulled.
Pin the multi-architecture digest in the later infrastructure rollout.

A disposable exact-image probe, isolated from production, confirmed:

- SFTPGo reports `2.7.5-9888a3d1-2026-07-17T17:02:02Z`;
- the SQLite `schema_version` value is exactly `33`;
- the initialized database passes `PRAGMA quick_check` and has an administrator;
- its tables include all v2.7.5 control-plane and transient provider tables.

## Authoritative state boundary

### Included

The complete provider database is snapshotted first so all references and
secrets come from one SQLite transaction. The published copy retains:

- `admins`, `users`, `groups`, `folders`, and their mapping tables;
- password hashes, user public keys, MFA/account filters, and additional info;
- `shares`, `api_keys`, `roles`, and `ip_lists`;
- `events_actions`, `events_rules`, and their mapping table;
- `configurations`, quota/accounting fields, and `schema_version`.

The exact v2.7.5 SQLite schema and current version are defined in
[`sqlite.go`](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/sqlite.go#L37-L181)
and
[`sqlcommon.go`](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/sqlcommon.go#L35-L42).

After the point-in-time copy is complete, the plugin removes rows from
`active_transfers`, `shared_sessions`, `tasks`, `defender_events`, and
`defender_hosts` inside the copy only. These rows coordinate live work or
record transient security/session state; they must start empty after recovery.
The source database is never modified.

### Excluded

- The five `/nas/*` media paths are read-only external payload and remain under
  the NAS data-protection policy.
- `/srv/sftpgo/data` may contain local user homes. The current runbook describes
  a read-only download user, but Git cannot prove the directory is empty. It
  must be confirmed read-only later and then classified as externally protected
  or deliberately added as a separate, quiesced payload artifact.
- Generated SSH host private keys under `/var/lib/sftpgo` are not included.
  Current SFTP is disabled. Enabling SFTP later creates a new recovery
  requirement because clients would otherwise observe a changed host identity.
- Compose and environment configuration are infrastructure-as-code.
- Logs, JWTs, current connections, rate-limit buckets, caches, and in-memory
  Defender state are reproducible/transient.

No KMS master key override appears in the inspected infrastructure. If one is
introduced later, it becomes an external restore prerequisite and must not be
embedded in the artifact.

## Selected backup mechanism

Add one production backend mount during the later infrastructure rollout:

```text
/docker-apps/sftpgo/config:/sources/sftpgo/config:ro
```

The plugin accepts only the absolute `database_path`; the deployment value is
`/sources/sftpgo/config/sftpgo.db`. It opens the source with SQLite URI
`mode=ro` and uses Python's `sqlite3.Connection.backup()` API to a newly created
private temporary file. SQLite documents its online backup API as producing a
consistent snapshot even while the source is being used
([SQLite Online Backup API](https://www.sqlite.org/backup.html)).

The plugin must then:

1. enforce a bounded, cancellation-aware snapshot operation;
2. clear only the declared transient tables in the copy;
3. normalize the copy to a standalone database without WAL/SHM dependencies;
4. require schema version 33, all required tables/columns, at least one admin,
   `PRAGMA quick_check = ok`, and an empty `PRAGMA foreign_key_check`;
5. publish one non-empty mode-`0600` `.db` artifact transactionally through
   `create_backup_artifact()`; and
6. let the normal sidecar record path, producer, target, timestamp, size, and
   hash evidence.

The artifact is secret-bearing. Never log database rows, credentials, hashes,
public keys, or error values derived from stored content.

## Rejected native API alternative

`GET /api/v2/dumpdata?output-data=1` produces provider-independent JSON and
`loaddata` can import it. The exact format covers the expected control-plane
objects ([`BackupData`](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/dataprovider.go#L712-L727)).
It remains a useful migration/export tool, but not the selected backup path:

- the route requires `PermAdminAny` / `*`, not a backup-only permission
  ([route guard](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/httpd/server.go#L1397-L1432),
  [permissions](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/admin.go#L37-L71));
- `DumpData` reads collections sequentially without an outer snapshot
  transaction
  ([implementation](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/dataprovider/dataprovider.go#L2403-L2565));
- `loaddata` restores objects one by one and can leave a partial destination;
- the current Homelab Backup backend is not attached to SFTPGo's network and
  SFTPGo's private binding does not allow the proposed container hostname; and
- an unrestricted administrator credential would become another secret stored
  by Homelab Backup.

Do not add API fallback or legacy modes without explicit approval.

## Restore contract

Declare `restore_capability = "partial"`. The plugin can prove exact,
integrity-checked, create-only database restoration, but it deliberately cannot
control another service lifecycle.

Restore must:

1. validate the staged artifact as an exact v2.7.5 SFTPGo database before any
   destination mutation;
2. require a new absolute `sftpgo.db` path in an otherwise-empty directory with
   the fixed `.sftpgo-restore-destination` v1 sentinel;
3. refuse symlinks, existing DB/WAL/SHM files, source-mount roots, `/backups`,
   `/var/lib/sftpgo`, and any artifact/destination overlap;
4. copy to a private file on the destination filesystem, fsync, revalidate,
   and publish create-only atomically;
5. revalidate the published file and remove it if post-publication validation
   fails; and
6. return `partial` with an explicit requirement to boot and verify the exact
   SFTPGo v2.7.5 image in isolation.

Production restores remain absolutely forbidden.

## Readiness and functional verification

The ordinary and telemetry `/healthz` handlers return `ok` but do not prove
provider contents
([HTTP handler](https://github.com/drakkan/sftpgo/blob/9888a3d169aed9011ae6e4f7a97ae735c1643068/internal/httpd/server.go#L1299-L1310)).
Each isolated destination must therefore prove:

- process readiness and exact v2.7.5/commit identity;
- successful authentication using the restored synthetic administrator;
- SQLite provider availability and schema version 33;
- expected administrators, users, groups, virtual folders, shares, API keys,
  roles, rules, and keys without printing secret values;
- the declared transient tables start empty; and
- disabled SFTP/FTP/WebDAV listeners remain disabled under the production-like
  drill profile.

## Required isolated local drill

Run the real plugin path twice against the exact digest, using only disposable
local directories and a Docker internal network:

1. Start a fresh source with synthetic bootstrap credentials and production-like
   disabled file-transfer listeners.
2. Create synthetic users, public keys, groups, virtual folders, shares, API
   keys, roles, and event metadata through first-party APIs.
3. Put the live source into WAL mode for the drill and keep SFTPGo running while
   the plugin snapshots through a read-only view of its directory.
4. Validate private mode, sidecar, independent size/SHA-256, schema version,
   integrity, expected content, and empty transient tables.
5. Mutate control-plane state and create a second distinct artifact.
6. Restore artifact 1 and artifact 2 to two separately created sentinel-marked
   empty directories. Never reuse a destination.
7. Boot a fresh exact-image container from each restored database and prove the
   readiness and semantic checks above. The first destination must lack the
   phase-two state; the second must contain it.
8. Prove rejection of a wrong schema version, incomplete/corrupt database,
   symlinked source/destination, existing destination, and publication failure.

No production path, endpoint, credential, NAS share, Docker socket, or service
is mounted or contacted by the drill.

## Production gates

After the local milestone is complete and the user confirms deployment:

1. pin the SFTPGo image digest in `homelab-infra`;
2. add only the read-only config-directory mount to the Docker-host backend;
3. create the SFTPGo target/job through an explicitly approved production
   configuration change;
4. run non-destructive connectivity and backup-only validation; and
5. read-only confirm whether `/srv/sftpgo/data` contains unique payload, then
   classify it without expanding this plugin implicitly.

Stop and ask before including client payload, preserving SSH host identity,
adding service downtime, broadening privileges, or changing the provider
backend. Those are new recovery contracts.
