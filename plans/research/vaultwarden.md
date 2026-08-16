# Vaultwarden 1.37.1 current-contract revalidation research

Research date: 2026-08-16

Scope: the existing `vaultwarden` plugin, Vaultwarden 1.37.1 at its exact
official source revision and linux/amd64 image manifest, and the declarations
currently checked into `homelab-infra`. No production host, endpoint,
container, configuration, credential, or data was contacted or changed. The
only network operations were read-only lookups against the official Git and
OCI registries. Environment files were inspected by key name only; no values
are reproduced here.

## Decision summary

**The existing plugin is valuable but does not yet satisfy the repository's
current completion contract. It can be repaired and fully proven on the dev VM,
but dependable production backup requires one new operator decision: every full
backup must briefly stop Vaultwarden.**

Vaultwarden's built-in `backup` command creates a coherent SQLite snapshot, but
it does not capture attachments, file Sends, configuration, or the JWT signing
key. The existing plugin runs that command and then archives those other
components while the application remains writable. Vaultwarden has no
transaction spanning its database and those files, so this is not one coherent
recovery point. The exact source demonstrates real race windows: attachment and
file-Send database rows can be committed before their payload upload completes
([attachment creation](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/ciphers.rs#L1130-L1158),
[attachment file write](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/ciphers.rs#L1319-L1324),
[file-Send creation](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/sends.rs#L309-L369),
[file-Send write](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/sends.rs#L381-L448)).

The selected exact contract is therefore:

1. require Vaultwarden 1.37.1 at the immutable linux/amd64 digest below;
2. prove the running application, database, image, version, default SQLite
   storage layout, and exact writable `/data` mount non-destructively;
3. require an explicit `allow_service_stop=true` target setting;
4. stop the source, run `/vaultwarden backup` in an exact-image networkless
   helper sharing only that stopped source's `/data` mount, package the SQLite
   snapshot plus the authoritative file components, validate, publish, clean
   up, restart the source in `finally`, and prove readiness;
5. make restore fail closed unless a hard local-drill enable flag, an exact
   destination allowlist, and an explicit restore-destination label all agree;
6. restore only into a fresh, disposable exact-version destination, with
   rollback and cancellation behavior tested; and
7. pass two clean exact-image backup-to-fresh-restore rounds with decrypted
   secure-note, attachment, and file-Send evidence through the supported Web
   Vault or official Bitwarden CLI.

The repaired plugin can remain `restore_capability = "automatic"` for this
strictly isolated contract. No production restore is ever permitted. The
production image pin, target update, and stop-based backup activation are
separate rollout gates after the local milestone.

## Exact image and source identity

Vaultwarden 1.37.1 is an immutable signed release whose tag resolves to source
commit
[`2629bcbe1380c894e3a7f52cafcac3988edb8fbb`](https://github.com/dani-garcia/vaultwarden/commit/2629bcbe1380c894e3a7f52cafcac3988edb8fbb)
([release](https://github.com/dani-garcia/vaultwarden/releases/tag/1.37.1)).
The annotated tag object is `74361a98bff527c1f028b1557341d119002d9a91`.

Read-only Docker Hub OCI resolution on 2026-08-16 produced:

| Property | Exact value |
| --- | --- |
| Registry image | `docker.io/vaultwarden/server:1.37.1` |
| OCI index | `sha256:ebdfe70701c60ac0c28c697e787cea767d7972940b786037b29fe0d507f821e8` |
| linux/amd64 manifest | `sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744` |
| Exact drill reference | `vaultwarden/server@sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744` |
| OCI version label | `1.37.1` |
| OCI revision label | `2629bcbe1380c894e3a7f52cafcac3988edb8fbb` |

The official
[Docker Hub tag resource](https://hub.docker.com/v2/repositories/vaultwarden/server/tags/1.37.1)
reports the index and platform manifests. Published SLSA provenance ties the
amd64 manifest to the source revision above; the upstream build defines the
same version, source, and revision OCI labels in
[`docker/docker-bake.hcl`](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/docker/docker-bake.hcl#L60-L70).

The exact plugin and drill must pin the linux/amd64 manifest, inspect the
container's image identity and OCI labels, and require `/api/version` to return
exactly `1.37.1`. A tag string alone is not identity. A future Vaultwarden
version or different platform manifest is a new research and drill contract,
not an implicit compatibility promise.

## Declared homelab topology

The inspected `homelab-infra` revision is
`eeed77a76fbc23db3da8470011535ad64cf0bc75`.

The Vaultwarden declaration is
[`docker.compose/tarkilnas-system/vaultwarden/vaultwarden.yaml`](../../../homelab-infra/docker.compose/tarkilnas-system/vaultwarden/vaultwarden.yaml):

| Property | Declared value |
| --- | --- |
| Image | `vaultwarden/server:1.37.1` (mutable tag; not yet digest-pinned) |
| Container / hostname | `vaultwarden` / `tarkilnas` |
| Persistent state | `/volume1/docker/bitwarden:/data` |
| Published port | host `55123` to container `10080` |
| Network | stack-local bridge |
| Restart | `unless-stopped` |
| Explicit user | none; the commented non-root declaration is inactive |

The service-specific environment file declares only the key names
`ROCKET_ENV`, `ROCKET_PORT`, `ROCKET_WORKERS`, and `SIGNUPS_ALLOWED`. Neither it
nor the common environment declares `DATA_FOLDER`, `DATABASE_URL`,
`ATTACHMENTS_FOLDER`, `SENDS_FOLDER`, or `RSA_KEY_FILENAME`. Static declarations
therefore select the default SQLite layout under `/data`; this is a declaration
fact, not proof of the live container.

The NAS Homelab Backup backend is declared in
[`docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml`](../../../homelab-infra/docker.compose/tarkilnas-system/homelab-backup/homelab-backup.yaml).
It runs as `0:0`, mounts `/var/run/docker.sock` read-only, stores central
artifacts under `/backups`, and currently uses the v0.2.1 backend image. A
read-only bind of the Docker socket still grants access to Docker's mutating API;
the Vaultwarden plugin uses that API for exec, stop/start, helper creation,
archive upload, and deletion. Exact target and image validation plus the
restore guard are therefore security boundaries, not optional hardening.

Production rollout later must replace the mutable Vaultwarden tag with the
immutable amd64 reference and deploy a Homelab Backup release containing the
repaired contract. No production state was inspected to infer that those
changes have already happened.

## Authoritative recovery state

For this declared default SQLite deployment, the authoritative application
state is:

- `db.sqlite3`: required; it contains nearly all account, encrypted vault,
  organization, device, policy, and control-plane state;
- `attachments/`: required whenever attachment rows exist;
- `sends/`: required for complete restoration of file Sends; text Sends are in
  SQLite, but file payloads are not;
- `config.json`: recommended and authoritative if admin-page overrides have
  ever been saved; it can contain plaintext secrets;
- `rsa_key.pem`: recommended and authoritative for preserving existing JWT and
  invitation validity; and
- the deployment environment and compose declaration: separately protected in
  `homelab-infra`, not copied into an artifact.

The official Vaultwarden backup guide describes the data directory, marks the
database and attachments as required, explains the optional-but-functional
importance of file Sends, and documents the sensitivity of configuration and
RSA material
([official backup inventory](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault#backing-up-data)).
The exact source derives the default database, attachment, Send, temporary,
template, and RSA paths from `DATA_FOLDER`
([configuration source](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/config.rs#L502-L521)).

Exact-version correction to the current plugin: 1.37.1 reads or generates only
`<RSA_KEY_FILENAME>.pem` and derives the public key in memory
([path helper](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/config.rs#L1589-L1591),
[key initialization](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/auth.rs#L69-L102)).
The current artifact's `rsa_key.pub.pem` member is not an authoritative 1.37.1
file and must be removed from the exact contract. Historical DER/public files
described by the general wiki are likewise outside this exact-version unit.

Vaultwarden supports moving `DATABASE_URL`, `ATTACHMENTS_FOLDER`,
`SENDS_FOLDER`, and `RSA_KEY_FILENAME` away from `DATA_FOLDER`, including some
external storage backends
([official environment template](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/.env.template#L13-L74)).
This milestone must not pretend to support those layouts. It should require the
declared exact defaults and reject any effective override. Supporting split or
remote state later would be a separate composite contract.

### Explicitly excluded

- `icon_cache/`: disposable and refetched on demand.
- `tmp/`, templates shipped by the image, logs, process memory, and transient
  upload files.
- `db.sqlite3-wal` and `db.sqlite3-shm`: the selected database member is a
  completed `VACUUM INTO` snapshot, not a raw live-file pair.
- pre-existing `db_*.sqlite3` snapshots and native backup residue.
- Docker/container layers, networks, runtime metadata, reverse proxy, DNS, TLS,
  NAS configuration, and replication.
- environment-managed credentials. The artifact naturally remains highly
  secret-bearing because it contains encrypted vault state, configuration, and
  the private JWT key; it must be private at rest and never summarized into
  logs, sidecars, or metrics.

## Strongest non-destructive health and version check

The official 1.37.1 image ships `/healthcheck.sh` and declares it as the Docker
healthcheck. The script reads the configured domain/base path and TLS mode, then
requests the correctly based `/alive` endpoint
([healthcheck source](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/docker/healthcheck.sh#L47-L65),
[image declaration](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/docker/Dockerfile.debian#L168-L173)).

`/alive` is materially stronger than an arbitrary HTTP 2xx: its route requires
a live database connection
([web route](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/web.rs#L209-L219)).
`/api/alive` has the same database-backed behavior, and `/api/version` returns
the compiled application version
([API routes](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/mod.rs#L182-L196)).

The repaired `test()` should be entirely non-destructive and require all of:

1. a syntactically bounded container name and `allow_service_stop` boolean;
2. Docker socket connectivity and exactly one matching container;
3. the immutable linux/amd64 image digest plus exact version/revision labels;
4. a running container with the exact writable `/data` mount;
5. no effective database/data/attachment/Send/RSA path overrides;
6. Docker health `healthy` and a successful bounded execution of the image's
   own `/healthcheck.sh`;
7. `/api/version == "1.37.1"`; and
8. the expected SQLite file at the fixed default path. Schema and row/file
   validation belongs to the coherent backup artifact, not a raw copy of the
   live WAL database during a non-destructive connection test.

`get_status()` should execute the same observable checks and return an honest
redacted status/version. The current unconditional `{"status": "ok"}` must be
removed. The current optional `health_url` fallback should also be removed from
this clean exact contract: it can ignore Vaultwarden's configured base path and
TLS, and it permits an arbitrary configured HTTP origin even though the exact
image already owns a correct local healthcheck.

## Consistency and backup semantics

### What the native command guarantees

`/vaultwarden backup` is available in 1.37.1 and supports SQLite only. It opens
the configured database read-only and executes `VACUUM INTO`, producing
`db_YYYYMMDD_HHMMSS.sqlite3`
([exact implementation](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/db/mod.rs#L401-L430),
[CLI dispatch](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/main.rs#L189-L199)).
That output is a coherent standalone SQLite database. It is not a complete
Vaultwarden backup. The admin UI explicitly warns that its equivalent operation
does not include configuration or attachments
([exact warning](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/static/templates/admin/settings.hbs#L121-L133)).

The official restore instructions require Vaultwarden to be stopped while
replacing data. When restoring a `.backup`/`VACUUM INTO` database, a stale live
`db.sqlite3-wal` must be deleted before startup
([official restore guidance](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault#restoring-backup-data)).

### Selected consistent backup state machine

The plugin should use a lock keyed by the resolved Docker container identity,
not only Homelab Backup's target ID, so two targets cannot stop or mutate the
same source concurrently.

Under that lock:

1. Repeat the exact `test()` checks and require
   `allow_service_stop=true`. Record the container ID, image identity, mount
   identity, running state, and the baseline set of generated `db_*.sqlite3`
   files without logging paths or values.
2. Stop the source with a fixed deadline and prove it is stopped. From this
   point, every exit path owns a restart/readiness obligation.
3. Create one networkless helper from the exact already-resolved image, share
   only the stopped source's `/data` mount read-write, set only the fixed
   non-secret default storage environment required by this exact contract, and
   run `/vaultwarden backup` under a fixed deadline. Do not clone the source's
   complete secret-bearing environment into the helper.
4. Require a successful exit and exactly one new generated snapshot relative to
   the baseline. Prefer the command's exact reported path plus the before/after
   identity. Reject no change, ambiguity, stale-newest selection, links, unsafe
   names, and timestamps outside the run boundary.
5. Stream only that generated database, `attachments/`, `sends/`,
   `config.json` when present, and `rsa_key.pem` into private temporary
   storage. Do not fetch the whole `/data` tree, old native snapshots, icon
   cache, or temp files.
6. Build and strictly validate the versioned component artifact described
   below. Update only non-secret sidecar evidence.
7. Delete exactly the newly generated native database through the same helper
   and prove its absence before publishing the central artifact. A cleanup
   failure fails the run and removes the unpublished temporary artifact.
8. Remove the helper, start the source, and require exact image identity,
   Docker health, `/healthcheck.sh`, database-backed readiness, and version
   `1.37.1` before the transactional artifact helper publishes the result.
9. On timeout, failure, or cancellation, terminate/remove uncertain helper work,
   restart and verify the source through cancellation-shielded cleanup, release
   the container lock, and propagate the original failure. If source restart
   cannot be proven, raise a specific critical error rather than reporting a
   backup result.

This is a deliberate short service outage. The outage lasts for the native
snapshot and component transfer, so attachment/Send volume determines its
duration. Production activation needs explicit approval for that downtime; the
current instruction allowing ordinary backup triggers does not implicitly
authorize stop/start behavior.

## Exact artifact contract

Publish one private `.tar.gz` through `create_backup_artifact()`. It contains:

- exactly one root regular `db.sqlite3`, sourced from the newly generated
  native snapshot;
- `attachments/<cipher_uuid>/<attachment_id>` regular files when attachment
  rows exist;
- `sends/<send_uuid>/<file_id>` regular files when file-Send rows exist;
- optional root regular `config.json` only when it exists on the source;
- required root regular `rsa_key.pem`; and
- one root regular `backup-manifest.json` with a new exact format version.

The private manifest may contain only recovery structure: application/version,
image manifest and source revision, component names, database migration head,
and a file inventory of relative identifier paths, sizes, and SHA-256 digests.
It must not contain plaintext configuration, vault rows, email addresses,
decrypted item/Send names, tokens, credentials, host paths, or Docker
environment values.

Validation before publication and again before restore must:

- require one gzip stream with no trailing data and a bounded compressed size;
- bound member count, per-member and total expanded bytes, path depth, and
  expansion ratio;
- reject absolute/traversal paths, duplicate or case-colliding names, links,
  sparse/device/FIFO/socket members, unsupported types, unexpected modes, and
  every member outside the exact allowlist;
- require the manifest to declare exactly every included component and file;
- parse `config.json` as one bounded JSON object without exposing values;
- parse `rsa_key.pem` as a valid RSA private key and record only a safe public
  fingerprint in the private manifest;
- open `db.sqlite3` read-only, require `PRAGMA quick_check == ok`, an empty
  `PRAGMA foreign_key_check`, the exact Diesel migration head for 1.37.1, and a
  conservative required-table set including `users`, `ciphers`, `attachments`,
  `sends`, `organizations`, `devices`, and `__diesel_schema_migrations`;
- map every attachment row to exactly one artifact file and require its stored
  byte count to match `file_size`;
- map every file-Send row's JSON identifier and size to exactly one artifact
  file; reject malformed file-Send data, missing payloads, and undeclared
  extras; and
- bind the validated descriptor, inode, size, and SHA-256 to the bytes later
  used for publication or restore. Do not validate a pathname and reopen it
  later.

The sidecar may report Vaultwarden version, immutable image digest, source
revision, component presence, attachment/Send counts, database migration head,
artifact size/hash, and validation outcome. It must not expose individual
identifiers, private paths, configuration keys/values, user counts tied to
identities, or secret material.

## Isolated restore contract

Restore is a disaster-recovery drill, never a production operation. Before any
Docker stop or data mutation, require the hard authorization and identity gates
below. Establish freshness from the stopped rollback snapshot before deleting
or replacing any data:

1. `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
2. the destination's canonical container identity in a comma-capable exact
   local allowlist;
3. label
   `asia.hollinger.homelab-backup.restore-destination=true` on the container;
4. different source and destination target/container identities;
5. the exact 1.37.1 linux/amd64 image and exact writable `/data` mount;
6. a running, healthy destination using only the default local SQLite layout;
7. a fresh database with zero `users`, `ciphers`, `attachments`, and `sends`;
   and
8. a strictly validated artifact whose descriptor remains held through upload.

Then:

1. Capture and strictly validate a complete stopped-destination rollback
   artifact before replacement.
2. Stop the destination and prove it is stopped.
3. Use a networkless exact-image helper sharing only its `/data` mount.
4. Remove managed components plus `db.sqlite3-wal` and `db.sqlite3-shm`, extract
   only the allowlisted verified members, preserve safe ownership/modes, and
   remove the private artifact manifest from the live data directory.
5. Fetch the restored database and component inventory back through Docker and
   repeat SQLite, schema, migration, file-map, size, and hash verification.
6. Start the destination and require a new healthy process, the image's own
   healthcheck, database-backed `/alive`, `/api/version == "1.37.1"`, and the
   application evidence below.
7. On every error or cancellation after mutation, stop the destination, restore
   the complete preimage, validate it, restart it, and prove readiness before
   propagating failure. If rollback or readiness cannot be proven, leave the
   destination stopped and return a specific failure requiring the disposable
   destination to be discarded.
8. Remove every helper and staging object and release the container lock under
   success, timeout, exception, and cancellation.

The current behavior that treats an initially stopped destination as a
successful unverified restore must be removed. This exact workflow accepts only
a running fresh destination and always proves application readiness.

## Exact local Docker drill

No database injection is necessary. Vaultwarden's own exact-tag Playwright
suite demonstrates Web Vault account creation and Send creation/viewing
([account helper](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/playwright/tests/setups/user.ts),
[Send test](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/playwright/tests/send.spec.ts)).
The official Bitwarden CLI supports self-hosted server configuration, creation
and retrieval of vault items and attachments, and creation/receipt of file
Sends
([official CLI documentation](https://bitwarden.com/help/cli/)).

Use a unique internal Docker network with no published host ports and synthetic
local-only credentials. Start:

- one source container from the exact digest with a unique named `/data` volume;
- two independently initialized destination containers from the exact digest,
  each with a distinct named `/data` volume, the restore-destination label, and
  a unique exact allowlist identity; and
- only the pinned Web Vault/Bitwarden client test tooling needed to exercise
  the supported user flow. Secrets enter through stdin or private files, never
  command arguments, logs, assertions, artifact names, or sidecars.

For each of two clean rounds:

1. Confirm image digest, OCI revision, version, mount separation, no published
   ports, and source/destination health.
2. Through the Web Vault, create a fresh synthetic account. Through the Web
   Vault or official CLI, create a uniquely named encrypted secure note, attach
   a small unique file, and create a file Send with different unique bytes.
3. Run the plugin backup. Independently validate private permissions, sidecar
   size/hash, exact members, database integrity/schema/migration, component
   inventory, and distinct artifact path/hash. Confirm the source was restarted
   healthy and the one native temporary snapshot was removed.
4. Restore to that round's separate fresh destination. Log in through the
   supported client, read/decrypt the secure-note marker, download/decrypt the
   attachment, and compare its plaintext SHA-256. Open the restored Send through
   its public flow (preserving the client-side fragment key while using the
   destination origin), download/decrypt it, and compare its different plaintext
   SHA-256. The server's attachment and Send download handlers read the restored
   filesystem payloads
   ([attachment route](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/web.rs#L197-L206),
   [Send route](https://github.com/dani-garcia/vaultwarden/blob/2629bcbe1380c894e3a7f52cafcac3988edb8fbb/src/api/core/sends.rs#L602-L622)).
5. Prove exact image/version, new process readiness, restored configuration
   structure, restored RSA fingerprint, and absence of phase leakage. Round two
   must use different note and payload markers and a different artifact hash.

The exact integration suite must also exercise, without touching production:

- wrong tag/digest/version/revision and non-SQLite or split-storage rejection;
- missing/unhealthy/stopped source, wrong/missing/writable mount, helper timeout,
  failed command, ambiguous native snapshot, cleanup failure, cancellation
  while stopped, and safe restart;
- corrupt/truncated/oversized/trailing-data archives, unsafe or duplicate
  members, malformed manifest/config/key/database, missing attachment/Send
  payload, and size/hash mismatch;
- restore disabled, unauthorized/unlabeled/nonfresh/same-volume destination,
  artifact replacement after validation, readiness failure, rollback success,
  rollback failure, and cancellation after mutation; and
- same-container concurrency serialization plus lock release after cancellation.

Finally remove all containers, volumes, networks, client state, credentials,
artifacts, sidecars, and helper objects. A post-drill Docker audit must show no
resource with the unique drill prefix.

## Concrete gaps in the existing plugin

Prioritized against the current repository contract:

1. **Critical — restore is not local-only.** Any configured container with an
   exact writable mount can currently be stopped and overwritten. There is no
   enable flag, allowlist, restore-destination label, freshness check, or
   source/destination identity rejection.
2. **High — backup is not a coherent multi-component recovery point.** It
   combines one SQLite snapshot with later live attachment/Send/config/key
   reads and does not validate database-to-file referential completeness.
3. **High — no exact image/version/storage contract.** Any container name and
   `/data`-like path are accepted; image digest, source revision, application
   version, SQLite mode, and split storage overrides are not checked.
4. **High — health is manufactured.** `test()` checks only container existence
   and archive visibility, while `get_status()` always returns `ok`. Neither
   proves process health, a working DB connection, native healthcheck success,
   or exact version.
5. **High — artifact validation permits unintended extraction.** Unknown and
   undeclared members are not rejected, duplicates and resource bounds are not
   enforced, and restore extracts the original whole archive into `/data`.
6. **High — verified bytes are not bound to used bytes.** Restore validates a
   pathname and later reopens it for upload, allowing replacement between
   verification and mutation.
7. **High — cancellation can bypass rollback.** Restore catches `Exception`,
   not `asyncio.CancelledError`, after mutation; cleanup may restart a
   destination without restoring its preimage.
8. **High — the exact RSA component is wrong.** `rsa_key.pub.pem` is listed even
   though 1.37.1 persists and consumes only the private PEM. The plugin does not
   validate the required private key.
9. **Medium — stale native backup selection is possible.** The plugin chooses
   the lexically newest `db_*.sqlite3` after fetching all of `/data`, without a
   before/after identity or captured command result.
10. **Medium — source transfer is overbroad and unbounded.** Fetching the whole
    data directory also transfers icon cache, temp state, live DB/WAL files, and
    old snapshots before discarding them.
11. **Medium — cleanup failure is only a warning.** A successful central
    artifact can be reported while generated source snapshots accumulate.
12. **Medium — an initially stopped restore destination is reported as success
    without application proof.** Automatic recovery must always end in a newly
    ready exact-version disposable destination.
13. **Medium — no cross-target container lock.** Separate Homelab Backup
    targets can address the same Docker container and overlap stop/start or
    restore operations.
14. **Proof gap — the historical drill is below the current standard.** It
    recorded two artifacts but only one restore, used synthetic component
    replacement rather than decrypted application content, did not pin the
    amd64 manifest, and did not check attachment/Send recovery through a client.

## Milestone readiness and production gates

The implementation milestone is finite and locally executable now:

- exact source and image identities are known;
- authoritative state and exclusions are known;
- the native database snapshot and stop-based full consistency boundary are
  supported by first-party source;
- the non-destructive health/version contract is known;
- the local backup, restore, rollback, and application-evidence topology is
  feasible without production access; and
- every current gap has a direct acceptance test.

Local completion does **not** authorize production use. Production rollout
requires all of:

1. explicit operator approval for a scheduled Vaultwarden stop on every backup;
2. an immutable image pin in `homelab-infra`;
3. deployment of the repaired Homelab Backup release;
4. target configuration with `allow_service_stop=true` and an agreed schedule;
5. a non-destructive production `test()` after deployment; and
6. separate explicit approval before the first production backup, because the
   new consistent mechanism stops and restarts the service.

Production restore remains forbidden without exception. If routine source
downtime is not acceptable, this plugin must remain blocked or be explicitly
reclassified as a weaker best-effort online backup; the latter would not meet
the current dependable full-state contract.
