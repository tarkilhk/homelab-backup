# Firefly III 6.6.3 backup and restore research

Research date: 2026-08-15
Scope: the Firefly III deployment declared in `homelab-infra`, exact upstream
Firefly III v6.6.3 source, and official Firefly III and MariaDB documentation.
No production endpoint or host was contacted. No production restore or other
production mutation was performed.

## Decision summary

Firefly III is a composite workload. Its authoritative recoverable state is the
complete MariaDB `firefly` database plus every attachment in
`/var/www/html/storage/upload`. The exact launch configuration, especially the
secret `APP_KEY`, is an external restore prerequisite and must not be embedded
in the backup artifact.

There is **no provably consistent online DB-plus-uploads backup boundary** in
the exact v6.6.3 application. A dependable plugin must briefly quiesce all
Firefly application writers while it makes a logical MariaDB dump and copies
and verifies the upload set. The database can remain online. A raw copy of the
live MariaDB volume is not an acceptable substitute.

This is currently a **STOP condition**, not an implementation detail. The
Homelab Backup container has neither Firefly network/storage access nor a
narrow lifecycle-control mechanism. Do not implement a plugin that reports
success until the user explicitly accepts scheduled Firefly application
downtime and approves a least-privilege way to quiesce and resume only this
application. Do not mount the Docker socket or give Homelab Backup general host
or Portainer administration rights.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
`docker.compose/misc/fireflyiii/fireflyiii.yaml`:

| Component | Declaration | State and connectivity |
| --- | --- | --- |
| Application | `fireflyiii/core:version-6.6.3` | `firefly-app` on the private `firefly_iii_network`; host port 47880; named volume `firefly_iii_upload` at `/var/www/html/storage/upload` |
| Database | `mariadb:12.3.2` | `firefly-db` on the same private network; named volume `firefly_iii_db` at `/var/lib/mysql`; database and application user are both named `firefly` |
| Cron | `alpine:3.24.1` | Calls the application cron endpoint daily at 03:00; no persistent state |

Evidence: application image, network, upload mount and health check are at
`docker.compose/misc/fireflyiii/fireflyiii.yaml:8-37`; database image, network
and volume are at `:38-50`; cron is at `:51-72`. The database connection is
declared at `docker.compose/misc/fireflyiii/fireflyiii_core.env:81-105` and the
database initialization names at
`docker.compose/misc/fireflyiii/fireflyiii_db.env:1-5`. No secret value was
copied into this note.

The `misc` entrypoint includes Firefly (`docker.compose/misc.yaml:1-9`) and the
repository's stack contract says all of those fragments form one Portainer
stack named `misc` (`docker.compose/misc/AGENTS.md:5-10`). Its concrete network
is therefore expected to be `misc_firefly_iii_network`; deployment validation
must resolve this rather than assume it.

The Homelab Backup backend currently joins only its default network and the
external Standard Notes network, and has no Firefly upload mount
(`docker.compose/system/homelab-backup/homelab-backup.yaml:1-26`). The old
whole-host backup service is disabled
(`docker.compose/system.yaml:5-8`). Even if enabled, rsync of a running MariaDB
data directory would not create a safe database backup: MariaDB documents that
an unprepared raw backup is not point-in-time consistent and may make InnoDB
refuse restoration ([backup overview](https://mariadb.com/docs/server/server-usage/backup-and-restore/backup-and-restore-overview)).

The infrastructure pins tags but not digests. On 2026-08-15 the public registry
resolved:

- `fireflyiii/core:version-6.6.3` to OCI index
  `sha256:4d63328dbc7c60ef5a8269bb2ee89f120b28f88eb0395e4211e23f93fd79337f`
  and Linux/amd64 manifest
  `sha256:a7cc158e43ee4856ef0a37017a9d497494ed5764b46505720e296c9b3fa79d30`;
- `mariadb:12.3.2` to OCI index
  `sha256:759869cb6f003234a95c6384cdee245b4bce7de26913fe607a8110362c0c007d`
  and Linux/amd64 manifest
  `sha256:1aed3452135970efc2fb44d423c070c604f1a47b66ce9000364c5c8bd318a212`.

Those registry values identify reproducible drill inputs; they do not prove
which historical bytes a production host pulled from a mutable tag. Pin the
multi-architecture digests in the later infrastructure change.

## Authoritative and reproducible state

### Include in every artifact

1. **The entire `firefly` MariaDB database.** It contains the financial domain,
   users and authentication state, configuration, attachment metadata, and
   the encrypted stored copy of the OAuth signing keypair. Exact v6.6.3 stores
   and reconstructs that keypair from the database
   ([`OAuthKeys.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Support/System/OAuthKeys.php#L119-L202)).
2. **Every live upload byte.** Exact v6.6.3's upload disk is
   `storage/upload`
   ([`filesystems.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/config/filesystems.php#L61-L69)),
   and a physical attachment is deterministically named `at-<id>.data`
   ([`Attachment.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Models/Attachment.php#L94-L100)).

Firefly's own backup guide requires the entire database, all
`/storage/upload` contents, and the exact Docker launch variables—especially
`APP_KEY`—and explicitly requires a restore test
([official backup guide](https://github.com/firefly-iii/docs/blob/9118cd72691ff8dbfd20d46d2440308839892f64/docs/docs/how-to/firefly-iii/advanced/backup.md#L10-L32)).

### External restore prerequisites

- The original `APP_KEY`, database connection values and other deployment
  secrets remain in the secret-management/configuration recovery process.
  They are never logged or stored in the artifact. The OAuth keys in the DB are
  encrypted with Laravel cryptography and cannot be recovered with a different
  application key
  ([`OAuthKeys.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Support/System/OAuthKeys.php#L119-L171)).
- Compose/env declarations, container image identities, reverse-proxy config,
  and scheduling are infrastructure-as-code. The backup sidecar should record
  app/server/client versions and image digests, but not secret values.

### Reproducible or excluded

Application caches, logs, sessions, exports, temporary files, the cron
container and generated key files are not separate authoritative payloads.
The key files are reconstructed from the DB when absent. Attachment bytes are
not encrypted at rest and must be treated as sensitive
([official security documentation](https://github.com/firefly-iii/docs/blob/9118cd72691ff8dbfd20d46d2440308839892f64/docs/docs/explanation/more-information/security.md#L46-L54)).

## Consistency proof and impossibility result

MariaDB's `--single-transaction` starts a consistent snapshot for transactional
InnoDB tables without blocking application traffic; `--quick` streams rows
rather than buffering them. Concurrent DDL is forbidden during the dump
([`mariadb-dump` reference](https://mariadb.com/docs/server/clients-and-utilities/backup-restore-and-import-clients/mariadb-dump)).
Firefly's exact MySQL configuration specifies InnoDB
([`database.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/config/database.php#L83-L96)).
That proves consistency **inside the database only**.

The filesystem and database do not share a transaction:

- Creating an attachment first publishes a DB row with `uploaded=false`
  ([`AttachmentFactory.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Factory/AttachmentFactory.php#L62-L80)),
  then writes the file, then updates MD5, size and `uploaded=true`
  ([`AttachmentHelper.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Helpers/Attachments/AttachmentHelper.php#L174-L185)).
- Deleting does the reverse: it removes the file before soft-deleting the DB
  row
  ([`AttachmentRepository.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Repositories/Attachment/AttachmentRepository.php#L52-L66)).
- The upload endpoint accepts an existing attachment, writes its physical file,
  then updates the row's hash and size
  ([`StoreController.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Api/V1/Controllers/Models/Attachment/StoreController.php#L94-L120),
  [`AttachmentHelper.php`](https://github.com/firefly-iii/firefly-iii/blob/1678c15905fa6186cb81a2438b23f692820849fa/app/Helpers/Attachments/AttachmentHelper.php#L174-L185)).

Therefore:

- DB-first can capture a row whose file is deleted before the filesystem copy.
- Files-first can miss a file created after the copy but visible to the DB
  snapshot.
- Before/after manifest equality plus retry is not a proof. An attachment can
  change A -> B -> A around the boundary while the DB snapshot sees B (the ABA
  problem), or can exist only during the dump. Hashing the final files cannot
  recover an intermediate version.
- A database read lock is insufficient because file deletion/replacement occurs
  before the corresponding DB mutation and does not honor that lock.

A guaranteed composite artifact consequently requires either an atomic
cross-volume storage snapshot (not declared here) or quiescing all Firefly
writers. Bounded retries can be a useful diagnostic, but must never turn this
impossibility into a reported-success backup.

## Selected secure backup contract

Subject to explicit approval of the STOP condition, the later plugin should:

1. Obtain a narrow, auditable quiescence lease that stops only `firefly-app`.
   The `firefly-cron` caller may also be paused to avoid noise. The database and
   Homelab Backup remain running. Always resume in a `finally` path and alarm if
   resume fails.
2. Confirm the app is quiesced and reject any concurrent schema migration/DDL.
3. Query a sorted attachment manifest containing at least `id`, `uploaded`,
   `deleted_at`, `md5`, `size` and `updated_at`.
4. Run the MariaDB-provided client using a private temporary defaults file,
   never credentials in command arguments or logs. The logical dump contract is
   `--single-transaction --quick --skip-lock-tables --routines --events
   --triggers --hex-blob --default-character-set=utf8mb4 --databases firefly`.
5. Copy only manifest rows with `uploaded=true` and `deleted_at IS NULL` from a
   dedicated read-only upload mount. Require every `at-<id>.data` file to have
   exactly the DB-declared size and MD5. Record and reject unexpected missing
   files; record orphan names as diagnostics without logging contents.
6. Re-query the manifest and require byte-for-byte equality. Any mutation means
   quiescence failed and the run fails.
7. Package `database.sql`, `uploads/`, and a versioned non-secret internal
   manifest into one secret-bearing archive. Publish it mode 0600 through
   `create_backup_artifact()` only after all validation succeeds; the normal
   sidecar supplies independent artifact size/hash evidence.
8. Resume the application, require its normal health endpoint to recover, and
   report a failed/partial attempt—not success—if resume or health recovery
   fails even when an artifact was produced.

Firefly has no native backup routine, and its CSV export cannot restore the full
application
([backup guide](https://github.com/firefly-iii/docs/blob/9118cd72691ff8dbfd20d46d2440308839892f64/docs/docs/how-to/firefly-iii/advanced/backup.md#L1-L20),
[export documentation](https://github.com/firefly-iii/docs/blob/9118cd72691ff8dbfd20d46d2440308839892f64/docs/docs/tutorials/firefly-iii/exporting-data.md#L8-L48)).
Do not add that export as a fallback.

### Least privilege

Use a dedicated network-restricted MariaDB backup identity, not the writable
Firefly application user or database root. MariaDB documents `SELECT` as the
minimum data-read privilege and additional privileges according to dump options
(`SHOW VIEW`, `TRIGGER`, `EVENT`, and in some versions/options `PROCESS` or
`RELOAD`)
([official dump guide](https://mariadb.com/docs/server/mariadb-quickstart-guides/mariadb-backup-guide)).
The exact grants must be proven against the exact MariaDB 12.3.2 server and the
client shipped in the Homelab Backup image, then reduced to the observed set.
Creating that identity and its secret is a one-time production mutation for the
operator, never an action for the plugin.

Minimum later infrastructure changes are:

- join the backend to the resolved external Firefly network;
- mount only the Firefly upload named volume at a dedicated path read-only;
- inject only the dedicated backup credential through the existing secret
  mechanism; and
- provide the approved Firefly-only quiesce/resume mechanism.

No Docker socket, database root credential, Firefly administrator token,
writable upload mount, host root filesystem mount, or production restore
permission is justified.

## Create-only isolated restore contract

Declare `restore_capability = "partial"`. The plugin can restore and validate
the composite state, but must not manage or overwrite a running Firefly
deployment. Production restores remain forbidden.

Restore must:

1. Stage and validate the whole archive before destination mutation: sidecar
   hash/size, safe member names, no links/devices, bounded total/member sizes,
   exact internal manifest contract, SQL presence, and attachment size/MD5.
2. Require a newly created database name and a new empty upload directory with
   a fixed versioned restore sentinel. Refuse existing tables/files, symlinks,
   source paths, `/backups`, `/var/lib/mysql`, and artifact/destination overlap.
3. Import with the `mariadb` client into that new isolated database only. Never
   use `--force`; abort on the first SQL error. MariaDB documents that restore
   is execution of the dump through the `mariadb` client and warns that a dump
   can drop/recreate existing data
   ([restore guide](https://mariadb.com/docs/server/mariadb-quickstart-guides/mariadb-restore-guide)).
4. Stage uploads privately on the destination filesystem, fsync, validate
   against the restored attachment rows, then publish the new directory
   create-only.
5. Run `mariadb-check`, verify all expected tables use InnoDB, and require every
   live attachment row to resolve to exactly one size/hash-matching file.
6. On any failure, drop only the database created by this invocation and remove
   only the sentinel-marked directory created by this invocation. Never clean
   or overwrite a pre-existing destination.
7. Return `partial` with the external requirements: exact original `APP_KEY`,
   production-like environment, and an isolated exact-version app boot and
   functional verification.

The restore credential may have `CREATE`, `CREATE VIEW`, `INSERT`, `ALTER`,
`INDEX`, `TRIGGER`, `EVENT` and related rights only inside the disposable
destination. It is never a production credential and never reaches a
production database host.

## Exact-version two-run disposable drill

Before any production target is created, run the real plugin path twice on the
development VM, using only an internal Docker network, synthetic secrets, new
named volumes/directories, and the exact OCI manifests listed above:

1. Start MariaDB 12.3.2 and Firefly III 6.6.3 by digest. Assert
   `SELECT VERSION()` and Firefly's version/health response before seeding.
2. Through Firefly's first-party local UI/API, create a synthetic user,
   accounts, categories, budgets and transactions plus attachment A with known
   bytes. Never import production data.
3. Quiesce only the disposable app through the proposed lifecycle mechanism,
   create artifact A through the real plugin, resume it, and verify health.
4. Mutate financial state, add attachment B, replace one attachment payload,
   and delete another. Create distinct artifact B through the same path.
5. Independently validate both artifacts and sidecars, including mode 0600,
   archive safety, SQL structure, attachment manifest, byte sizes and hashes.
6. Restore A and B into two different newly created databases and two different
   sentinel-marked empty upload directories. Never reuse a destination.
7. Boot a fresh exact Firefly digest against each restored destination with the
   same synthetic `APP_KEY`. Require health, login, expected version, expected
   financial objects, and successful download/hash validation of every expected
   attachment. Prove A lacks B-only changes and B contains them.
8. Restart each restored app and database and repeat the functional checks to
   prove persistence rather than one-process cache behavior.
9. Exercise failures: corrupt SQL, corrupt/truncated attachment, unsafe archive
   path, non-empty destination, missing/wrong sentinel, wrong DB version/client,
   quiescence loss, dump failure and resume failure. None may publish a valid
   success or modify a pre-existing destination.
10. Repeat the entire backup/restore sequence a second time from clean
    disposable state to prove deterministic cleanup and absence of leaked
    networks, volumes, containers or credentials.

Record exact client/server/app versions, commands, counts and hashes without
recording credentials or attachment content. Do not contact production during
this drill.

## STOP conditions

Stop rather than weaken the contract if any of these remains true:

- scheduled Firefly application downtime is not explicitly accepted;
- no narrow, fail-safe Firefly-only quiesce/resume mechanism is approved;
- a dedicated least-privilege DB identity cannot be provisioned and exact
  grants cannot be proven against MariaDB 12.3.2;
- the app and DB tags cannot be resolved/pinned for the drill;
- any table is non-InnoDB, concurrent DDL is possible, or the shipped client
  cannot produce and restore the declared exact-server dump;
- the upload volume cannot be mounted read-only without broad host access;
- the attachment manifest changes while quiesced, any referenced file is
  missing or mismatched, or the app cannot be resumed healthy;
- the original `APP_KEY` and required deployment secrets are not independently
  recoverable; or
- either exact-version restore run fails functional or persistence validation.

If downtime is rejected, classify Firefly III as blocked. Do not silently fall
back to DB-only, CSV export, a raw live volume copy, or an online best-effort
composite artifact.
