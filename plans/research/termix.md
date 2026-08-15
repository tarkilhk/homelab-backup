# Termix 2.3.2 backup and restore research

Status: researched against the exact deployed release; implementation not started. This document contains no credential values and authorizes no production restore.

## Deployment verified from infrastructure-as-code

The current deployment is one `ghcr.io/lukegus/termix:release-2.3.2` container with `/docker-apps/termix/data` bind-mounted at `/app/data`, port `48080:8080`, a 256 MiB memory limit, `no-new-privileges`, and only the shared misc network. SSL is disabled. The exact image pulled for local drills resolves to OCI index digest `sha256:06a27a3dc22ae426cf0681fcdbdb58732f2aab56d8ce9e95f4deea18306e5c2f`. Evidence: `/home/dev/projects/homelab-infra/docker.compose/misc/termix/termix.yaml:5-22` and `/home/dev/projects/homelab-infra/docker.compose/misc/termix/termix.env:1-2`.

The source corresponding to that image is official tag `release-2.3.2-tag`, package version 2.3.2, commit `c3282b5dca081d52513e94329bbc71084338217d`: [release](https://github.com/Termix-SSH/Termix/releases/tag/release-2.3.2-tag), [package.json](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/package.json#L1-L6).

## What is authoritative under `/app/data`

Termix 2.3.2 does **not** operate directly on an on-disk SQLite database. It opens SQLite as `:memory:`, decrypts the persisted database into memory at startup, serializes memory periodically, and writes the encrypted result back to disk. The relevant implementation is [database initialization and persistence](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/db/index.ts#L12-L28) and [the save loop](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/db/index.ts#L1495-L1545).

The exact backup allowlist should therefore be:

- `.env` — essential instance secrets, including the database encryption key and other keys needed to interpret restored data. Termix generates and maintains this file itself; see [system key storage](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/utils/system-crypto.ts#L245-L329). It is highly sensitive and must never be logged.
- `db.sqlite.encrypted` — the default authoritative persisted database. Its contents include users, password/TOTP material, sessions and trusted devices, hosts, encrypted connection credentials and private keys, snippets, API keys, sharing/RBAC data, audit data, preferences and topology; see the [official schema](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/db/schema.ts).
- `.opk/config.yml`, only when present — user-authored OPKSSH configuration; see [OPKSSH configuration handling](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/ssh/opkssh-auth.ts#L55-L77).

The current v2 encrypted format is one file: a metadata header plus AES-256-GCM ciphertext. It is written to a unique temporary file and atomically renamed over the destination; see [encrypted-file publication](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/utils/database-file-encryption.ts#L25-L70). A reader therefore receives either the prior complete serialized database or the new complete one, not a half-written database. There is no live SQLite WAL/SHM set to coordinate because the live database is in memory.

Do not include regenerable or transient `opkssh/` downloaded binaries, `uploads/` import staging, `.temp/`, or migration debris. The current deployment does not need `ssl/`; if SSL is later enabled, `ssl/termix.crt` and `ssl/termix.key` become durable inputs and this boundary must be reviewed.

## Why native export/import is not the backup boundary

Termix's native `/database/export` endpoint exports selected data for the authenticated user into a new SQLite file. Its import endpoint merges data into an authenticated user's current database and skips duplicates. It does not preserve the complete instance, system keys, all users, authentication/session state, or an exact database image. Evidence: [export implementation](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/database.ts#L585-L1135) and [incremental import](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/database.ts#L1137-L1650).

The admin restore route accepts a server-side backup path but does not provide a safe, complete hot-replacement protocol for the already-running in-memory database. It should not be used by this plugin: [restore route](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/database.ts#L1712-L1771).

## Live snapshot safety and limitation

A read-only filesystem backup can run without Termix network access, its credentials, the Docker socket, or downtime. Add only a read-only backend mount such as `/docker-apps/termix/data:/sources/termix/data:ro` in a later infrastructure change.

The plugin should copy only the allowlisted regular files, reject symlinks, and stable-read each file twice (size, metadata and cryptographic hash) after a settle interval longer than the two-second save debounce, retrying a small bounded number of times. It should validate the encrypted header as v2/AES-256-GCM, decrypt a private temporary copy with the key from `.env` without logging it, then run SQLite `quick_check`, foreign-key checks, and minimum schema checks. Publish an atomic private artifact plus sidecar containing the Termix release/commit, filenames, modes, sizes and hashes.

This is crash-consistent but not a zero-RPO snapshot of memory. Termix debounces dirty saves by about two seconds, also runs a five-minute persistence loop, and saves on graceful shutdown: [save trigger](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/utils/database-save-trigger.ts#L23-L97), [shutdown save](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/starter.ts#L198-L218). Save errors are logged but swallowed by the persistence function, so a filesystem reader cannot prove it captured the absolute latest in-memory mutation. If zero-second RPO is required, Termix needs an explicit force-save/quiesce seam or a controlled stop; neither belongs in this filesystem-only plugin without a new decision.

## Restore boundary and disposable drill

The plugin restore should be **create-only and local-only**: extract into an empty sentinel-marked destination, reject symlinks and unexpected paths, restore restrictive modes, verify manifest hashes, decrypt and check SQLite, and never overwrite a running installation. On that basis, declare `restore_capability` as `partial`: the plugin restores a valid data directory, but intentionally does not control a Termix process.

The integration drill should create an isolated Docker network and temporary host directory, restore the artifact there, and boot the exact `release-2.3.2` image with that directory mounted at `/app/data`. Require container health, successful authentication with seeded disposable credentials, and retrieval of representative seeded records, then repeat from a second independently produced artifact. The image's public `/health` endpoint only returns `{"status":"ok"}` and does not query the database, so health alone is insufficient: [health route](https://github.com/Termix-SSH/Termix/blob/c3282b5dca081d52513e94329bbc71084338217d/src/backend/database/database.ts#L214-L236). Do not test restores against production.

Pin validation to the exact release. Startup performs schema evolution, and newer releases may migrate the database irreversibly; this research makes no cross-version or downgrade guarantee.

## Recommended plugin boundary

Implement a `termix` filesystem plugin configured with a single `data_path`. Its backup is an exact, validated, encrypted-instance snapshot of the small authoritative allowlist. Its restore materializes that snapshot into an isolated empty directory. Keep Docker lifecycle and exact-image boot verification in the local integration drill rather than granting the production backup service Docker or network authority.

## STOP conditions

Stop safely and report a specific error when any of these is true:

- The deployed image/tag or observed database format is not exact Termix 2.3.2 v2/AES-256-GCM.
- `.env` or `db.sqlite.encrypted` is missing, unreadable, malformed, cannot authenticate/decrypt, or fails SQLite integrity/schema checks.
- The deployment contains an unencrypted `db.sqlite`, legacy companion `.meta` format, or another layout; supporting it would be a separate compatibility decision.
- The allowlisted files keep changing across bounded stable-read retries, including during key rotation or repeated saves.
- An unknown persistent file/directory appears under `/app/data` and cannot be proven regenerable from the exact source.
- The requested restore destination is not local, isolated, sentinel-marked and empty, or any path is a symlink.
- The exact 2.3.2 image cannot pass the boot/authentication/representative-data drill. Do not retry with a newer image and silently migrate the artifact.
- The requested guarantee is the latest in-memory mutation rather than the latest successfully persisted snapshot.
- Any implementation would require production restore, production mutation, Docker-socket access, application credentials, or downtime. The proposed boundary requires none of them.
