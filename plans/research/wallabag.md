# Wallabag 2.6.14 backup and restore research

## Decision

The deployed Wallabag database is **SQLite**, not PostgreSQL or MySQL. The
recoverable service boundary is:

- `/var/www/wallabag/data/db/wallabag.sqlite` and any SQLite journal/WAL needed
  to obtain a valid snapshot;
- `/var/www/wallabag/data/site-credentials-secret-key.txt`, which decrypts
  site credentials stored in the database; and
- `/var/www/wallabag/web/assets/images`, which holds downloaded article images.

For this deployment those paths are backed by the two host binds
`/docker-apps/wallabag/data` and `/docker-apps/wallabag/images`. The application
configuration is a separate recovery prerequisite: the Docker entrypoint
regenerates `app/config/parameters.yml` from its image template and environment
on every start, so the local infrastructure declaration and secret system—not
that generated container-layer file—are authoritative.

The strongest composite backup needs short Wallabag-only downtime. Stop the
exact `wallabag` container, copy the SQLite directory and the other state
read-only to private staging, normalize and verify the SQLite copy in staging,
capture the images, restart and prove Wallabag 2.6.14 readiness, then publish.
SQLite's Online Backup API can make a consistent live **database** snapshot,
but it cannot provide one point in time across the database, encryption key,
and image tree. A raw live copy is unsafe during a transaction. Sources:
[SQLite backup safety](https://www.sqlite.org/howtocorrupt.html#_backup_or_restore_while_a_transaction_is_active)
and [SQLite Online Backup API](https://www.sqlite.org/backup.html).

**Implementation is blocked.** Homelab Backup has neither Wallabag bind
mounted read-only nor narrowly scoped control to quiesce it. The deployed image
is pinned by tag, not digest, so exact live-image identity also needs an
approved read-only inventory. Raw Docker-socket access is not the default.

This research made no call to a production host or endpoint, changed no
production state, contains no secret values, and authorizes no production
backup, stop, write, or restore. Every restore below is create-only and
disposable; production restores are forbidden.

## Exact deployment and actual database backend

At infrastructure commit `eeed77a76fbc23db3da8470011535ad64cf0bc75`,
`/home/dev/projects/homelab-infra/docker.compose/misc/wallabag/wallabag.yaml`
declares one service:

- image `wallabag/wallabag:2.6.14`;
- container name `wallabag`, memory limit 256 MiB, port `47580:80`;
- `/docker-apps/wallabag/data:/var/www/wallabag/data`;
- `/docker-apps/wallabag/images:/var/www/wallabag/web/assets/images`; and
- only `SYMFONY__ENV__DOMAIN_NAME` and `SYMFONY__ENV__SERVER_NAME` as
  Wallabag-specific environment settings.

The exact official Docker tag is source commit
`480d3833bc44c555b6ee76491635fef84ec7dd86`; it embeds the Wallabag 2.6.14
release at application commit
`74cbfd945bc316b5cf0308ba56e32fd2ed3443c0`. See the
[tagged Dockerfile](https://github.com/wallabag/docker/blob/2.6.14/Dockerfile)
and [Wallabag 2.6.14 release](https://github.com/wallabag/wallabag/releases/tag/2.6.14).

The tagged entrypoint defaults an absent database driver to `pdo_sqlite`, sets
the file to `/var/www/wallabag/data/db/wallabag.sqlite`, and initializes it only
when it is missing or empty. The tagged parameters template has the same
defaults. The infrastructure supplies no driver/host/database override and no
separate database service or network, so the repository-declared effective
backend is SQLite. Sources:
[2.6.14 entrypoint](https://github.com/wallabag/docker/blob/2.6.14/root/entrypoint.sh),
[parameters template](https://github.com/wallabag/docker/blob/2.6.14/root/etc/wallabag/parameters.template.yml),
and [official image README](https://github.com/wallabag/docker/blob/2.6.14/README.md#sqlite).

At research time Docker Hub resolved `wallabag/wallabag:2.6.14` to OCI index
digest
`sha256:4a527e027e0d59e87c14225ef11e005af3d4890374202ad319ce5e63dfc66709`;
its amd64 manifest was
`sha256:82ade66b403d67c05732f9f08003034509f6499e9f487b858c0b77524b28545d`.
The registry-owned image is on the
[official Wallabag Docker Hub page](https://hub.docker.com/r/wallabag/wallabag/).
The Compose declaration pins only the tag, so these registry results do not
prove the image currently running. Before implementation or an exact-deployment
drill, an explicitly approved **read-only** inventory must record the running
image ID, platform, mounts, and effective environment variable **names** without
printing values or invoking an application backup.

### Declared Symfony-secret issue

The 2.6.14 image template has a public fallback for `SYMFONY__ENV__SECRET`, and
the repository-visible Wallabag/common env files do not override it. That
secret is used by Symfony security, including remember-me signing; see the
[tagged template](https://github.com/wallabag/docker/blob/2.6.14/root/etc/wallabag/parameters.template.yml)
and [security configuration](https://github.com/wallabag/wallabag/blob/2.6.14/app/config/security.yml).

Do not copy the public fallback into a backup artifact, silently preserve it as
a new plugin default, or silently rotate it. Replacing it is a breaking security
and session-continuity decision outside this research. Confirm the effective
live setting by name-only/redacted inspection and obtain an explicit operator
decision before any recovery procedure is approved. The disposable drill uses
its own strong synthetic secret from its first boot onward.

## Authoritative state

| State | Backup disposition |
| --- | --- |
| `data/db/wallabag.sqlite` | Required. It holds users/password hashes, OAuth tokens/clients, saved article content and metadata, tags, annotations, reading state, internal settings, tagging/ignore rules, 2FA state, and schema/migration history. Copying only per-user exports is not full recovery. |
| SQLite `-journal`, `-wal`, and `-shm` sidecars | Treat as part of the source database state during capture. Copy the whole `data/db` directory while stopped, then recover/normalize only the staged copy. Never separate a database from a hot journal/WAL. |
| `data/site-credentials-secret-key.txt` | Required if present, and always secret. v2.6.14 generates this 0600 key and uses it to encrypt/decrypt site usernames/passwords held in SQLite. If credential rows exist but the key is missing, fail rather than publish an unrecoverable backup. |
| `web/assets/images` | Required. When “Download images locally” is enabled, Wallabag saves article images here and rewrites stored article HTML to these local paths. Empty is valid when the feature has never been used. |
| `app/config/parameters.yml` | Required recovery configuration in a traditional install, but **derived, not persisted state here**. The 2.6.14 Docker entrypoint overwrites it from environment on each start. Recreate it from versioned infrastructure plus separately managed secrets; store only a redacted config fingerprint/reference in the manifest. |
| `web/uploads/import` | Exclude. In this deployment synchronous import uploads are temporary and are deleted after processing; this path is not mounted. No Redis/RabbitMQ worker is declared. Stop if an actual pending async-import topology is later discovered. |
| `var/cache`, `var/logs`, `var/sessions`, application code/vendor/assets | Exclude. They are unmounted container-layer runtime/build state recreated by the exact image. Browser sessions are intentionally not recovered. |
| `data/assets` or any other unexpected data-root member | The image creates a build-time `data/assets` directory, but the host bind obscures it and v2.6.14 application source does not identify it as managed content. Allow it only if empty; stop and classify any unexpected non-empty member rather than silently omit it. |

Wallabag's official backup guide identifies `app/config/parameters.yml`, the
SQLite `data/db` directory, and `web/assets/images` as the backup inputs:
[Backup](https://doc.wallabag.org/admin/backup/). The image's Docker-specific
derivation of `parameters.yml` is established by the tagged entrypoint above.
Downloaded-image placement is also documented in
[Internal Settings](https://doc.wallabag.org/admin/internal_settings/#misc)
and implemented by the
[2.6.14 image helper](https://github.com/wallabag/wallabag/blob/2.6.14/src/Wallabag/CoreBundle/Helper/DownloadImages.php).

The additional encryption key is defined at
`data/site-credentials-secret-key.txt`; `CryptoProxy` creates it, restricts its
mode, and uses it to encrypt/decrypt DB-held site credentials. Sources:
[v2.6.14 configuration](https://github.com/wallabag/wallabag/blob/2.6.14/app/config/wallabag.yml),
[CryptoProxy](https://github.com/wallabag/wallabag/blob/2.6.14/src/Wallabag/CoreBundle/Helper/CryptoProxy.php),
and [credential repository](https://github.com/wallabag/wallabag/blob/2.6.14/src/Wallabag/CoreBundle/Repository/SiteCredentialRepository.php).

## Supported backup and restore boundary

Wallabag documents a filesystem-oriented service backup: save parameters,
database, and pictures. For SQLite it says to copy `data/db`. The exact Docker
tag provides no full-service backup/restore endpoint or command. Its
`wallabag:export` command is per-user and exports entries only, so it omits
users, OAuth/2FA, annotations/settings and the credential key; see the
[tagged export command](https://github.com/wallabag/wallabag/blob/2.6.14/src/Wallabag/CoreBundle/Command/ExportCommand.php).
It is a portability feature, not the disaster-recovery contract.

There is no first-party all-state restore workflow beyond placing the saved
state and configuration back into a compatible installation. For this exact
Docker image, a non-empty restored SQLite file causes the entrypoint to skip
fresh database installation, regenerate parameters/cache from environment,
and start the application. That behavior is useful in the disposable drill,
but it is not permission to overwrite a running installation. The plugin's
restore must remain fresh-destination-only.

## Consistency boundary and downtime

SQLite explicitly warns that an external copy made during a transaction can be
corrupt, and that a hot rollback journal or WAL must remain paired with its
database. Its Online Backup API or `VACUUM INTO` can safely snapshot a live
database, but Wallabag's database updates and downloaded-image writes do not
share a transaction or snapshot. Therefore an online service-wide artifact
cannot prove database/image coherence even if the SQLite member alone is
valid.

The approved backup transaction is:

1. acquire normal target serialization and run only read-only preflight;
2. use an allowlisted helper to request a bounded graceful stop of the exact
   `wallabag` container and prove it is stopped; no other writer is declared;
3. copy the complete source `data/db` directory, site-credential key, and image
   tree from read-only mounts into private staging while the service remains
   stopped;
4. on **staging only**, open the copied database with its copied sidecars so
   SQLite can recover it, use the SQLite Backup API to produce one normalized
   `wallabag.sqlite`, then require `PRAGMA integrity_check` to return only `ok`,
   `PRAGMA foreign_key_check` to return zero rows, and the expected 2.6.14
   tables/migration state;
5. validate all members, counts, hashes, versions, and config prerequisites;
6. restart exactly the stopped container and require its built-in health/API
   readiness at version 2.6.14; and
7. publish atomically only after restart/readiness succeeds. On every failure
   or cancellation, reap child work, remove staging, make a bounded restart
   attempt, and surface a redacted error. A restart failure publishes nothing.

Docker stop sends the container's main process `SIGTERM` before a bounded
grace period and then `SIGKILL`; use an explicit bounded grace period and treat
forced termination as a reason for careful staged journal recovery, not a
reason to write the source. See [Docker stop](https://docs.docker.com/reference/cli/docker/container/stop/).
SQLite defines the validation roles of
[`integrity_check` and `foreign_key_check`](https://www.sqlite.org/pragma.html#pragma_integrity_check).

This is an availability mutation and requires explicit approval. If a writer
outside the declared container is discovered, quiesce it through an approved
narrow seam too or stop; do not label an online approximation consistent.

## Minimum production access

The current Homelab Backup backend at
`/home/dev/projects/homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml`
has only backup/catalog/Jellyfin binds and unrelated networks. It has neither
Wallabag bind and no Docker socket.

Minimum access after approval is:

- read-only mounts of `/docker-apps/wallabag/data` and
  `/docker-apps/wallabag/images` at dedicated plugin paths;
- bounded HTTP reachability to the declared Wallabag URL (or an allowlisted
  local readiness proxy) for public `GET /api/info`; and
- one narrow lifecycle helper or operator-owned pre/post hook able to inspect,
  stop, and start only the exact `wallabag` container and report state.

No database network, database username/password, Wallabag login/API token,
generated `parameters.yml`, host filesystem root, or raw Docker socket is
needed. The source binds must never be mounted read-write. A host ACL/read-only
bind should expose only the two roots, including the 0600 credential key,
rather than granting broad host traversal.

`test()` remains non-destructive: require bounded public `GET /api/info` to
report exactly 2.6.14, verify the two approved roots/database and any present
key are readable regular paths without opening the live DB for recovery and
without creating a file, and compare configured identity/mount metadata. The
official image uses
that same endpoint for its healthcheck, and the route reads internal settings
through the database; see the [tagged Dockerfile](https://github.com/wallabag/docker/blob/2.6.14/Dockerfile)
and [2.6.14 info route](https://github.com/wallabag/wallabag/blob/2.6.14/src/Wallabag/ApiBundle/Controller/WallabagRestController.php).
Do not log URLs containing credentials, environment values, key contents,
article titles/URLs, image paths, or database rows.

## Artifact contract

Publish one private archive through `create_backup_artifact()` only after the
whole transaction succeeds:

```text
manifest.json
data/db/wallabag.sqlite
data/site-credentials-secret-key.txt
images/<downloaded image tree>
```

The manifest records format version; Wallabag/Docker source tags and commits;
observed immutable image/platform digest; SQLite library/header/schema and
migration versions; the verified numeric UID/GID expected by the exact image;
source mount identities without absolute host paths in the
sidecar; redacted config fingerprint plus infrastructure commit; whether the
credential key and downloaded images exist; UTC quiescence/copy/recovery/
restart times; deliberate exclusions; counts/byte totals/modes; and SHA-256 for
the normalized DB, key, and every regular image.

Require a non-empty SQLite header and expected schema, successful integrity and
foreign-key checks, no source journals in the final normalized member, and a
key whenever credential rows exist. Reject symlinks, hard links, devices,
sockets, FIFOs, absolute/traversing or duplicate members, changing files,
unsupported modes, excessive member/expanded-byte limits, unexpected
data-root members, version mismatches, corrupt/truncated images, and hash/count
discrepancies. Artifact and sidecar permissions are private; neither may expose
the credential/Symfony secret, personal URLs/titles, or host paths.

## Secret-safe create-only restore contract

Declare `restore_capability = "partial"`. The plugin can safely materialize and
validate state in fresh local directories without gaining application
orchestrator authority. The disposable harness separately boots the exact
image and proves application behavior.

A restore must:

1. refuse unless the caller supplies the exact non-production restore sentinel
   in a local parent and the `data` and `images` destination children do not
   exist; reject symlinked paths and anything resembling a configured
   production root;
2. accept only an authenticated artifact produced by this system, require exact
   Wallabag 2.6.14 and compatible SQLite metadata, validate the sidecar,
   manifest, member set, sizes and hashes before destination mutation;
3. require an explicit external bootstrap configuration containing SQLite as
   the backend, the intended disposable domain/server name, and a strong
   synthetic Symfony secret supplied through a private env/secret file; never
   restore or synthesize production secret values from artifact data;
4. stage the normalized DB, credential key, and images privately; restore the
   exact image's verified numeric ownership, require mode 0600 for the key, and
   fail if credential rows exist without it;
5. open the staged database only, require full integrity/foreign-key checks,
   exact expected schema/migration state and representative row/image-reference
   invariants, then atomically rename the staged `data` and `images` children
   into their still-absent disposable destinations; and
6. return `partial` with exact digest-pinned boot/validation steps. On error,
   remove only staging and newly created sentinel-scoped output. Never contact,
   stop, overwrite, or restore production.

Do not run `wallabag:install` or migrations against the restored DB. The exact
2.6.14 entrypoint should detect the non-empty SQLite database and skip install;
any install/reset prompt, schema write, or migration means the contract has
diverged and must stop. Browser sessions are container-layer state and are
intentionally revoked; preserving application data does not promise session
continuity.

## Disposable exact-version two-run Docker drill

Use temporary host directories, an internal-only Docker network, synthetic
credentials/content/images, and immutable image digests. Publish no LAN port,
join no production network, mount no production/NAS path, reuse no production
secret, and expose no Docker socket inside Homelab Backup. A host-side harness
may control only its disposable Compose project. Seed/verify through a sibling
client container on the private network.

Before claiming exact-deployment equivalence, perform the approved read-only
inventory and pin the observed live platform manifest. If confirmed, use the
current official v2.6.14 index/platform digests recorded above; otherwise stop
and resolve the discrepancy. Use the one digest-pinned Wallabag image with
fresh disposable `data` and `images` bind directories, SQLite defaults, an
internal-only domain alias, and a strong synthetic `SYMFONY__ENV__SECRET`.
Also run an internal synthetic article server with deterministic HTML, PNG/SVG
assets, and one credential-protected page; no internet fetch is needed.

For each of two consecutive runs:

1. start a fresh source stack; replace the default account password; seed a
   second user, OAuth client/token, tagged/favorited/archived articles, reading
   progress, an annotation, internal settings, a site credential for the
   protected synthetic page, and an article whose images are downloaded
   locally; record IDs/counts, selected content hashes, image byte hashes,
   migration state, and successful credential-assisted fetch;
2. invoke the real plugin backup path through the proposed narrow lifecycle
   seam; prove Wallabag was stopped for the composite capture and restarted;
3. require a distinct non-empty artifact and sidecar, independent SHA-256,
   exact version/digest, successful DB/member/hash validation, correct 0600 key
   handling, and no secrets, article URLs/titles, or host paths in logs/metadata;
4. restore into different fresh sentinel-marked `data`/`images` destinations,
   boot the same digest-pinned 2.6.14 image with the same **synthetic** bootstrap
   config, and require `/api/info` version 2.6.14 plus authenticated login;
5. prove exact users/entries/tags/annotations/OAuth/config counts and IDs,
   archive/favorite/progress state, rendered/retrieved image byte equality, and
   successful decryption/use of the restored site credential; also require DB
   integrity, zero foreign-key errors and unchanged migration state; and
6. destroy every disposable container, network, bind directory, credential,
   artifact copy and temp path, including after injected failure.

Between run 1 and run 2, mutate only the disposable source with a second marker
article/image/tag/annotation. Restore artifact A and artifact B into separate
fresh destinations and prove A contains only state A while B contains A+B.
Inject at least stop timeout/forced-stop handling, DB copy/recovery/integrity
failure, image change/read failure, missing credential key with credential
rows, archive/hash failure, timeout/cancellation, restart/readiness failure,
unsafe member, wrong image/version, untrusted sidecar, and non-empty restore
destination. No failure may publish an artifact, leak a secret, touch
production, or leave the disposable source stopped.

## STOP conditions

Stop before implementation, drill, or production work if any of these holds:

- The user has not explicitly approved bounded Wallabag downtime and the exact
  narrowly scoped lifecycle design.
- The only lifecycle option is a raw Docker socket, unrestricted remote shell,
  or another host-wide control grant.
- Both declared roots cannot be mounted read-only, the 0600 key cannot be read
  through a narrow ACL, or any proposal requires a read-write production mount.
- The actual live database backend/path, image/platform digest, mounts,
  effective environment variable names, schema/migration state, or presence of
  another writer has not been established by approved read-only inspection.
- The running service is not exactly Wallabag 2.6.14, the observed image differs
  from the expected release without explanation, or the DB schema is
  incompatible.
- Another process can write SQLite or images while Wallabag is stopped and
  cannot be identified and quiesced.
- Source SQLite/journal state cannot be copied together and normalized in
  staging, or any integrity/foreign-key/schema/member/hash check fails.
- Credential rows exist but `site-credentials-secret-key.txt` is absent,
  unreadable, exposed in logs, or not included with private permissions.
- Unexpected non-empty state exists in `/docker-apps/wallabag/data` and has not
  been classified; do not silently omit it.
- The implementation would use per-user export as full backup, open/copy the
  live DB unsafely, write/checkpoint/recover the production DB, use an app login
  token, or trigger any native install/migration/export command as a probe.
- The effective Symfony secret remains unresolved, or recovery would silently
  preserve the public image fallback or rotate a production secret without an
  explicit breaking/security decision.
- Restart/readiness cannot be bounded and guaranteed on every backup failure or
  cancellation, or publication can happen before readiness returns.
- A restore path is not fresh, absent, local, disposable, and sentinel-marked;
  any host/path/network/credential resembles production; or restore would
  overwrite existing state.
- The drill cannot pin the observed exact digest, isolate all resources, prove
  DB/key/image/config behavior twice, or clean up after failure.
- Any step would perform a production restore. Production restore is forbidden,
  not an approval prompt.
