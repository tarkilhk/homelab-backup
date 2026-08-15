# Calibre 9.11.0 control-plane backup and restore research

Research date: 2026-08-15

Scope: the exact Calibre deployment declared in `homelab-infra`, LinuxServer
image tag `v9.11.0-ls412`, and Calibre 9.11.0's first-party backup, library,
database, configuration, and command-line contracts. No production host,
endpoint, library, or configuration was contacted or inspected, and no
production state was changed.

## Decision summary

**A Calibre plugin is warranted, but only as a lower-priority, explicitly
partial control-plane backup.** There is valuable unique state beyond ebook
payload bytes:

- the library SQLite catalog, custom columns, tags, saved searches, reading
  positions, annotations, and library preferences;
- author/tag/series notes and their resources;
- per-book OPF metadata backups and cover art; and
- Calibre preferences, templates, device/conversion settings, installed
  plugins, and optional Content-server accounts in the Calibre configuration
  directory.

The plugin must **not** copy EPUB, PDF, MOBI, AZW, audiobook, or other book
format bytes. Per-book extra `data/`, deleted-book trash, and the derived
full-text index are excluded too. Those belong to the separate NAS media backup
policy. The artifact instead records hashes and relative identities of expected
external book files so a restore can prove it was paired with the right media
snapshot.

This is narrower than Calibre's supported complete backup, which requires the
entire library folder. Calibre explicitly says that folder contains all books
and metadata and must be copied in full. It also says restoring a copied
configuration directory is not officially supported, though it usually works
([Calibre 9.11 FAQ source](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/manual/faq.rst),
[published backup FAQ](https://manual.calibre-ebook.com/en/faq.html#how-do-i-backup-calibre)).
Therefore the honest declaration is `restore_capability = "partial"`.

**Local-build verdict:** the control-plane plugin, safe restore, validation,
and exact two-backup/two-restore drill are fully buildable on the dev VM without
a user decision or production access. Production activation still requires:

1. declaring the actual Calibre library root, which is runtime configuration
   and is not present in the compose file; and
2. approving a short quiescence window for Calibre and every other writer to
   that root. Readarr shares the deployed `/eBooks` mount and must be treated as
   a potential concurrent writer.

The correct production source is a stopped, read-only filesystem view or an
immutable snapshot. No Calibre API credential, Docker socket, broad host mount,
or production restore path is needed.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/media/books/books.yaml`](../../../homelab-infra/docker.compose/media/books/books.yaml):

| Property | Declared value |
| --- | --- |
| Image | `ghcr.io/linuxserver/calibre:v9.11.0-ls412` |
| Container | `calibre`, 640 MiB memory limit, restart unless stopped |
| Identity | `PUID=0`, `PGID=0` |
| Persistent container home | `/docker-apps/calibre/config:/config` |
| Ebook tree | `/mnt/nas-media/eBooks:/eBooks` |
| Published ports | `8080:8080`, `8081:8181`, and `8666:8666` |
| Compose network | the books fragment's private `default_network` |

The LAN HAProxy declaration routes `calibre.hollinger.asia` to the Docker host's
port 8081 with TLS to the backend
([declared proxy](../../../homelab-infra/files/pfsense/haproxy-services.yaml)).
This establishes intended service use, not current runtime activity. Production
contact is forbidden, so no library, active process, user, or endpoint was
queried.

Readarr mounts the same `/mnt/nas-media/eBooks` host tree at `/eBooks` with
write-capable compose syntax. Audiobookshelf mounts its `audiobooks` subtree.
Consequently, stopping only Calibre does not prove the library tree is immutable.
The quiescence contract must identify the actual library root and all writers;
it must not assume that a read-only HTTP Content server is the only writer.

The Docker-host Homelab Backup backend currently mounts only its backup root,
its own database, and Jellyfin's native backup directory. It has no Calibre
config/library mount and no Docker socket
([Homelab Backup declaration](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml)).

### Exact image and source provenance

LinuxServer tag `v9.11.0-ls412` resolves to repository commit
`c339e88e033578dfc398e90b21856f3b5b1d6809`. The image sets `HOME=/config`,
creates `/config/.config/calibre`, seeds `server-config.txt`, and starts Calibre
through its desktop autostart definition
([exact LinuxServer source](https://github.com/linuxserver/docker-calibre/tree/c339e88e033578dfc398e90b21856f3b5b1d6809),
[Dockerfile](https://github.com/linuxserver/docker-calibre/blob/c339e88e033578dfc398e90b21856f3b5b1d6809/Dockerfile),
[configuration initializer](https://github.com/linuxserver/docker-calibre/blob/c339e88e033578dfc398e90b21856f3b5b1d6809/root/etc/s6-overlay/s6-rc.d/init-calibre-config/run)).

At research time, the deployed tag resolved to OCI index
`sha256:6b8fba77f741987d5c964ae3ce8afb0035ace5c43a0e9f0545ef0352b2e3dc9a`
and Linux/amd64 manifest
`sha256:d7ff56f4366e97c2479337a1614ebd5bd814e2e3abeb3347d28422492ab55280`.
Pin the amd64 digest for local drills rather than relying on the mutable tag.

Calibre tag `v9.11.0` resolves to source commit
`b23dfb5d42b93919511ef472d6a85945d7e8c8c5`; the first-party release page
also publishes the 9.11.0 binaries and source
([release files](https://download.calibre-ebook.com/9.11.0/),
[exact source](https://github.com/kovidgoyal/calibre/tree/b23dfb5d42b93919511ef472d6a85945d7e8c8c5)).
If the pinned container does not report Calibre 9.11.0 and LinuxServer ls412,
stop and re-research.

## Where authoritative state lives

### Application configuration

LinuxServer sets `HOME=/config`. On Linux, Calibre defaults its configuration
directory to `$XDG_CONFIG_HOME/calibre` or `~/.config/calibre`; it can be
overridden by `CALIBRE_CONFIG_DIRECTORY`
([Calibre environment variables](https://manual.calibre-ebook.com/en/customize.html#environment-variables),
[exact config-path source](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/constants.py)).
No override is declared in compose, so the expected application directory is
`/config/.config/calibre`. Verify it from the stopped filesystem and refuse an
unexpected or escaping path.

Include the complete Calibre application configuration directory. It can hold:

- global/GUI preferences and the selected/known library paths;
- conversion, metadata-download, viewer, device, email, and Content-server
  settings;
- custom templates, icons, dictionaries, recipes, and resources;
- installed third-party plugin packages and plugin-specific state; and
- `server-users.sqlite` when Content-server authentication is configured.

The last item is especially sensitive. Calibre's exact user-database source
states that it stores the Content-server password unhashed because digest auth
needs it
([user database](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/srv/users.py)).
Configuration may also contain mail credentials, private-key paths, network
locations, plugin code, and reading history. It must never enter logs or
sidecars.

Do not archive the whole LinuxServer `/config` home blindly. Desktop caches,
Selkies/Openbox state, temporary files, and an accidental default
`/config/Calibre Library` could add irrelevant data or book payloads. The
initial contract is the exact Calibre configuration directory only. If an
operator intentionally configured additional Calibre resources outside it,
that is an explicit future boundary decision.

### Library metadata

Calibre defines a library as one root folder. `metadata.db` at its top level is
the SQLite catalog used to render the book list. It holds title/author/rating,
tags, identifiers, custom columns, saved searches and other library preferences,
book-relative paths and formats, reading positions, and annotations. Calibre's
FAQ states that moving the full library preserves metadata, tags, custom
columns, and related state
([library structure and transfer](https://manual.calibre-ebook.com/en/faq.html#how-do-i-move-my-calibre-data-from-one-computer-to-another),
[exact database API](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/cache.py)).

The metadata-only artifact includes:

- root `metadata.db` plus any live `-wal`/`-shm` companions present in the
  stopped or snapshotted source;
- root `metadata_db_prefs_backup.json` when present;
- `.calnotes/**`, including `notes.db` and resources for notes attached to
  authors, series, tags, publishers, and other categories;
- every regular per-book `metadata.opf`; and
- every regular per-book `cover.jpg`.

Calibre automatically writes each book's metadata into its `metadata.opf` and
can reconstruct `metadata.db` from those files. The OPF files are valuable
secondary recovery material, but the quiesced database remains primary because
automatic OPF backup can lag and newer database state includes annotations and
other relationships
([GUI library maintenance](https://manual.calibre-ebook.com/en/gui.html#library),
[exact OPF backup command](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/cli/cmd_backup_metadata.py),
[database restore implementation](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/restore.py)).

`.calnotes` is not optional metadata noise. Calibre 9.11 names it as the library
notes directory, maintains a separate `notes.db`, and restores its resources
alongside book records
([library constants](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/constants.py),
[restore source](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/restore.py)).

The exact production library root is **unknown**. Compose exposes `/eBooks`, but
Calibre stores the selected path in its runtime configuration and supports
multiple libraries. LinuxServer recommends `/config/Calibre Library`, while this
deployment deliberately adds `/eBooks`. Do not guess that `/eBooks` itself is
the library or scan every `metadata.db` recursively and back up whatever is
found. Require one or more explicit library roots in target configuration and
validate each root has its own top-level `metadata.db`.

### Explicitly excluded

- Every ebook/audiobook/document format and associated source-media byte.
- Per-book `data/**`. Calibre treats these as extra files associated with the
  book; they belong to the separate payload backup.
- `.caltrash/**`, which contains recoverable deleted books and therefore media
  payload as well as metadata.
- `full-text-search.db` and its sidecars. It is a derived content index that can
  be rebuilt; including it would increase artifact sensitivity and size without
  preserving unique state.
- `metadata_pre_restore.db`, temporary files, caches, logs, lock files, sockets,
  container layers, downloaded image bytes, and process memory.
- Readarr and Audiobookshelf state, even though they share part of the media
  tree. They have separate plugin/backup contracts.
- NAS configuration, mount credentials, reverse proxy, TLS, DNS, Compose, and
  environment/secrets supplied by infrastructure.

Because formats and `data/` are excluded, build an internal payload manifest
while the source is quiesced. It records each expected book-relative regular
file's format, size, and SHA-256. Store that detailed path/hash map inside the
private artifact, not in the public sidecar. The manifest proves which external
media snapshot is required without copying its bytes.

## Existing deployment risk: Calibre on NAS

Calibre's official FAQ says not to place a library on a network drive. It cites
missing locking/hardlink behavior, flaky filesystems, and corruption from two
Calibre instances, and recommends its Content server instead. If a sync tool is
unavoidable, Calibre and the sync tool must never access the library at the same
time
([network-drive warning](https://manual.calibre-ebook.com/en/faq.html#i-am-getting-errors-with-my-calibre-library-on-a-networked-drive-nas)).

The deployed `/eBooks` path is NAS-backed. This research does not prove that the
actual selected library lives there, what mount protocol/options are used, or
whether locking is sound. A backup plugin cannot make an unsafe live library
safe. Before production activation, verify the selected root, run Calibre's own
library checks on a copy, and document the accepted NAS risk. Do not use the
plugin as evidence that the live write topology is supported.

## Consistency and downtime

Calibre's documented complete backup is a filesystem copy of the entire library.
Its FAQ warns against concurrent library access. `metadata.db`, notes, OPFs,
covers, book files, and configuration can all change while the GUI or embedded
Content server is active. Readarr can also change the shared tree.

Selected production precondition:

1. Resolve each explicit library root to a literal, allowlisted path.
2. Reject new Calibre writes and stop the `calibre` service cleanly, including
   the embedded Content server.
3. Stop or otherwise prove quiescent every other writer to that root, currently
   including `readarr` unless its actual path/write behavior proves disjoint.
4. Confirm processes have exited. HTTP failure is not proof of quiescence.
5. Hold one host-side lock and either keep writers stopped through selection,
   hashing, archive, and validation, or capture immutable snapshots of config
   and library roots while stopped and then restart immediately.
6. Always restart exactly the services that were running before the operation,
   including on error, timeout, cancellation, or client disconnect.

Do not run `calibredb backup_metadata` against production as part of backup. It
marks/writes book metadata and therefore changes the source. Do not use
`calibre-debug --export-all-calibre-data`: Calibre's export includes book
formats and covers by design, so it violates this plugin's media exclusion
([exact export implementation](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/cache.py)).

An online raw copy is not selected. Although SQLite has snapshot mechanisms,
it would not establish the same instant for config, notes resources, OPFs,
covers, and payload-manifest files. A filesystem snapshot may reduce downtime,
but snapshot support across the Docker-host config filesystem and NAS library
is not declared and must not be invented.

## Least privilege

The plugin needs no Calibre username, Content-server password, browser session,
or API token. It needs read access only to the quiesced Calibre config and
selected metadata files plus the ability to hash, but not archive, payload
files.

Recommended production shape:

- a root-owned, fixed-purpose host helper controlled through a forced command
  or narrowly scoped local RPC;
- literal allowlisted config and library roots, no caller-provided paths or
  glob expansion;
- authority to stop/start only `calibre` and the separately declared co-writers,
  preserve prior running state, and hold one backup lock;
- a streaming selector that emits only the declared metadata allowlist and a
  payload hash manifest; and
- no shell, PTY, SFTP, arbitrary command/argument, Docker API proxy, restore,
  upload, delete, retention, or destination write operation.

A simpler read-only mount of all `/eBooks` would technically let the root-running
Homelab Backup backend read every excluded ebook. The selector/helper is a
better capability boundary. If simplicity wins and a broad read-only mount is
accepted explicitly, the plugin must still prove that no payload member reaches
the artifact. Never mount `/var/run/docker.sock`, `/`, `/docker-apps`, or the
entire NAS.

The later infrastructure work should also reconsider `PUID=0`/`PGID=0`; it is
not needed for the plugin contract. That hardening is separate from building
the backup.

## Artifact and validation contract

Publish one deterministic archive through `create_backup_artifact()` or
`write_backup_bytes()` only after validation. Use fixed logical roots such as
`config/calibre/`, `libraries/<stable-slug>/control/`, and
`libraries/<stable-slug>/book-metadata/`. Never encode absolute host paths in
member names.

Required checks:

1. Reject an empty config root, absent/non-regular top-level `metadata.db`,
   nested library roots, duplicate roots, or roots outside the allowlist.
2. Require a structured quiescence/snapshot attestation; free-form helper output
   and HTTP status are insufficient.
3. Run `PRAGMA quick_check` and `PRAGMA foreign_key_check` on a private extracted
   copy of `metadata.db` and `notes.db` when present
   ([SQLite integrity check](https://www.sqlite.org/pragma.html#pragma_integrity_check),
   [foreign-key check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)).
4. Validate Calibre's database schema/user version against an explicit tested
   range for 9.11.0. Unknown migrations fail closed.
5. Enumerate database book IDs, relative book paths, declared formats, and
   covers. Require safe normalized relative paths with no escape, symlink,
   case-collision, or duplicate identity.
6. Require every database-declared format in the payload manifest with matching
   size/hash. Report orphan payloads in aggregate; do not include them.
7. Require every selected OPF/cover/notes member to correspond to the same
   library boundary. Unknown selected files fail rather than broaden the archive.
8. Reject symlinks, hardlinks, devices, FIFOs, sockets, absolute/parent paths,
   sparse or oversized members, duplicate members, decompression bombs, and
   mutation between manifest and archive.
9. Record artifact digest, exact image/source version, plugin version, schema
   fingerprints, config/file/book/format counts, aggregate bytes, excluded
   boundary, and quiescence timestamps in the sidecar. Do not expose titles,
   authors, usernames, paths, identifiers, plugin names, or credentials there.

Artifact mode is `0600`. Config can contain plaintext Content-server passwords
and executable plugins; the catalog exposes reading interests and annotations.
Never log file contents or names, SQL rows, title/author metadata, URLs, email
addresses, account names, passwords, tokens, certificates, or private keys.

## Honest restore contract

Declare `restore_capability = "partial"`. Restore is local/dev-only and
create-only:

1. Require a trusted artifact and a separately supplied payload snapshot whose
   hashes exactly satisfy the artifact manifest. No payload means validation
   only; it is not a usable Calibre restore.
2. Require absent config and library destinations under an explicit test root.
   Refuse existing directories, symlinks, mount points, production-looking
   paths/hosts, `/config`, `/eBooks`, and source aliases.
3. Copy the external payload fixture into an isolated staging library without
   changing its bytes. Add the backed-up OPFs, covers, database, prefs backup,
   and notes; extract config into a separate staging home.
4. Validate archive safety, hashes, SQLite, schema, database-to-payload closure,
   and selected-file allowlists before atomic rename into final test roots.
5. Start the exact pinned LinuxServer image with no host/prod mounts and outbound
   network denied. Point it only at restored test config/library paths.
6. Verify catalog metadata, custom columns, searches, notes/resources,
   annotations, reading positions, covers, conversion/device preferences,
   plugin inventory, and synthetic Content-server authentication. Prove every
   book format downloads/opens with the exact externally restored hash.

Calibre warns that restoring configuration directories is not officially
supported, and plugin packages are executable code. Start a restored config
only from a trusted artifact in the isolated drill, with no secrets or network.
Do not claim that arbitrary third-party plugins will remain compatible across
Calibre versions.

The restore never contacts production, changes NAS media, starts/stops a remote
container, invokes a host helper, overwrites a library, or calls Calibre's
destructive database-rebuild action. A future human disaster-recovery runbook
can use the locally proven output, but production restore implementation and
execution remain forbidden.

## Exact local two-backup / two-restore drill

Run entirely on the dev VM with temporary paths and the exact Linux/amd64 image
digest. Pull once if needed, then deny outbound network.

### Harness and State A

1. Create isolated `config-source`, `library-source`, `payload-fixtures`,
   backup, and restore roots. Mount no production or broad host path.
2. Start exact Calibre with `HOME=/config`, a synthetic Content-server user,
   and one library at `/library`. Use only synthetic credentials.
3. Through supported GUI/CLI operations, add two tiny deterministic fixtures in
   different formats, set distinct titles/authors/tags/ratings/identifiers,
   create a custom column and saved search, choose a custom cover, and set a
   deterministic conversion/device preference.
4. Add an annotation/read position and an author/tag note with one tiny embedded
   resource. Configure a harmless local-only Content-server account and one
   inert synthetic plugin or plugin preference to prove config persistence.
5. Run Calibre's metadata backup command in the fixture setup only, wait for all
   jobs to settle, then stop the container. This source write is acceptable only
   in the isolated local setup, not in the production backup path.
6. Run Backup A through a local helper emulator. Assert it captures only config,
   metadata DB/prefs/notes, OPFs, covers, and payload hashes—not format bytes.

### State B and Backup B

1. Restart the same isolated source. Add a third book/format; remove or replace
   one A format through Calibre; change title/tag/rating/custom-column values,
   saved search, cover, annotation/read position, note resource, preference, and
   synthetic Content-server user state.
2. Create one deleted-book trash entry and enable/build full-text search so the
   exclusion tests exercise `.caltrash` and `full-text-search.db`.
3. Settle jobs, stop the source and local writer emulator, and create Backup B.
   Assert A/B artifact hashes, control manifests, and logical state differ where
   expected while neither contains payload, trash, per-book `data`, or FTS DB.

### Negative copies

On copies of artifacts and fixtures:

- truncate/corrupt metadata or notes SQLite;
- remove, replace, or alter one external ebook payload;
- delete a required OPF/cover/notes resource;
- supply an unknown schema/version or mismatched library slug;
- inject `../`, absolute, duplicate, symlink, hardlink, FIFO, device, sparse,
  oversized, excessive-member, and case-colliding archive members;
- inject an excluded EPUB/PDF, `.caltrash`, `data/`, or FTS member;
- simulate concurrent backup, writer mutation, disconnect, timeout,
  cancellation, helper failure, and restart failure; and
- point restore at an existing, symlinked, mounted, production-like, or source
  directory.

Every case must fail before publication or destination mutation. The local
helper emulator must prove it restores exactly the prior running/stopped state
on every exit path.

### Restore A and B

1. Restore A with its A payload fixture into absent `restore-A`; restore B with
   its B fixture into absent `restore-B`. Repeating either restore must fail.
2. Start the pinned image against each restored pair one at a time, with
   outbound network denied and distinct ports.
3. Use `calibredb list`, Calibre's own library check on the restored copy, and
   GUI/Content-server reads to verify exact A state and exact B state. Note that
   `calibredb check_library` vacuums/writes, so it runs only on the restored test
   copy
   ([exact command source](https://github.com/kovidgoyal/calibre/blob/b23dfb5d42b93919511ef472d6a85945d7e8c8c5/src/calibre/db/cli/cmd_check_library.py)).
4. Verify custom metadata, saved searches, notes/resources, annotations,
   positions, covers, settings, plugin inventory, and synthetic authentication.
5. Download/open every format and compare it byte-for-byte with the separate
   payload fixture. Prove A lacks B-only state and neither restore reaches the
   network or any production path.
6. Retain machine-readable drill evidence: image digest, artifact hash,
   aggregate counts, validation results, external-payload manifest match, and
   zero forbidden-network requests. Do not retain synthetic passwords in logs.

## STOP conditions

Stop and report rather than weakening the contract if any condition applies:

- any production restore, library/config write, database repair, metadata dump,
  file replacement, or other production mutation is proposed;
- the actual library root is not explicitly configured, contains no top-level
  `metadata.db`, escapes the allowlist, or multiple libraries are discovered
  without separate explicit roots;
- the exact Calibre/LinuxServer version or digest differs from the tested one;
- Calibre, its embedded Content server, Readarr, a sync tool, or another writer
  cannot be proven quiescent for the selected root;
- a stable snapshot cannot be proven and source files mutate during selection,
  hashing, archive, or validation;
- the helper cannot guarantee restoration of every service's prior running
  state after failure, cancellation, timeout, or disconnect;
- the mechanism requires a Docker socket, root/host mount, arbitrary SSH shell,
  SFTP, free-form path/arguments, broad sudo, API password, or remote restore;
- database integrity, schema, or foreign-key checks fail;
- database-declared payload is missing or its separate snapshot hash/size does
  not match;
- the artifact contains a book format, per-book `data/`, `.caltrash`, FTS DB,
  unrelated `/config` state, or any other excluded payload;
- archive paths/types are unsafe or selected metadata does not correspond to
  the declared library;
- a password, token, private key, title, author, annotation, user, plugin name,
  or other sensitive value would enter logs or sidecars;
- restore does not target an absent isolated dev directory, or any production/
  NAS path could alias the destination;
- a restored plugin or process attempts real network, LAN, NAS, or production
  access; or
- someone asks the plugin to claim a complete Calibre restore without the
  separately verified ebook payload and an acknowledged configuration-restore
  limitation.

The existing NAS topology warning is not silently waived by a passing local
drill. If production shows locking errors, concurrent Calibre instances, or
library corruption, stop and resolve that operational architecture before
enabling scheduled backups.

## Production questions after local implementation

No decision is needed to build and prove the plugin locally. Before creating a
production target, the user must confirm:

1. the exact selected library root or roots under `/eBooks` or `/config`;
2. which services can write each root and whether the brief Calibre/Readarr
   quiescence window is acceptable; and
3. whether the narrow selector/helper is preferred over granting Homelab Backup
   a broad read-only view of the entire ebook tree.

Until then, classify Calibre as **plugin warranted, local implementation ready,
production activation pending topology confirmation**.
