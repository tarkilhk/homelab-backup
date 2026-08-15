# Profilarr 1.1.5 backup and restore research

Research date: 2026-08-16

Scope: the exact Profilarr deployment declared in `homelab-infra`, image tag
`santiagosayshey/profilarr:v1.1.5`, and Profilarr 1.1.5's first-party source,
data model, Git workflow, backup API, and restore behavior. No production host,
endpoint, container, repository, configuration, or database was contacted or
inspected, and no production state was changed.

## Decision summary

**A Profilarr plugin is warranted and can be built and proven completely on the
dev VM without another user decision or production downtime.** Its state has two
different authorities:

1. Profile, custom-format, regex, and media-management definitions live in a
   real Git working repository under `/config/db`. Commits reachable from a
   remote are Git-rebuildable; local-only commits are not. Dirty, staged,
   untracked, merge, and conflict state is local and is not represented by a
   remote repository.
2. `/config/profilarr.db` is a separate SQLite authority for the linked
   repository URL, Radarr/Sonarr targets and API keys, selected sync content,
   sync schedules, app authentication/API key/session secret, application
   settings, rename choices, and language-import policy. None of that is stored
   in the profile Git history or declared in `homelab-infra`.

The plugin should **not** use Profilarr's native backup endpoint. Version 1.1.5
walks the live `/config` tree and copies each file into a ZIP. It neither opens a
SQLite snapshot nor establishes a Git consistency boundary, so it can race a
database write, profile edit, commit, pull, or merge. It also includes logs and
per-repository Git configuration unnecessarily
([exact native backup](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/task/backup/backup.py)).

The safe live boundary is instead:

- SQLite's online backup API for a coherent snapshot of `profilarr.db`; and
- a self-contained `git bundle --all` made only while the repository is clean
  and its HEAD, refs, index/worktree status, and operation state are identical
  before and after capture.

SQLite documents that a completed online backup is a snapshot of the source as
it was when copying began. Git documents `git bundle create --all` as a full
backup of all refs and explicitly warns against recursively copying a repository
that may be written during the copy
([SQLite Online Backup API](https://www.sqlite.org/backup.html),
[Git bundle documentation](https://git-scm.com/docs/git-bundle)).

This clean-repository contract deliberately refuses dirty or in-progress Git
state. Profilarr's own v1 documentation says local changes must be staged and
committed to become durable versioned customizations; a dirty worktree is an
actionable failed backup, not something the plugin should capture
probabilistically. Local-only commits remain protected by the bundle even when
they have not been pushed.

The artifact restores all authoritative Profilarr application state, so the
honest declaration is `restore_capability = "full"` for Profilarr itself.
Radarr, Sonarr, and Git hosting remain independent dependencies with their own
recovery contracts. Logs, old native backups, session-rate-limit history, and
other operational residue are not authoritative.

## Exact deployed topology

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/media/profilarr/profilarr.yaml`](../../../homelab-infra/docker.compose/media/profilarr/profilarr.yaml):

| Property | Declared value |
| --- | --- |
| Image | `santiagosayshey/profilarr:v1.1.5` |
| Container | `profilarr`, 256 MiB memory limit, restart unless stopped |
| Persistent state | `/docker-apps/profilarr/config:/config` |
| Published port | `6868:6868` |
| Compose network | the fragment's private `default_network` |
| Effective app identity | `PUID=1003`, `PGID=1004` |
| Declared app-specific environment names | `AUTH`, `PUID`, `PGID` |

Only environment names and the non-secret numeric identity were inspected; no
secret value is reproduced here. No `PROFILARR_PAT` is declared in the tracked
app-specific environment file. That means static infrastructure does not
declare authenticated Git push capability, but it does not prove whether the
runtime repository is public, has credentials embedded in a URL, or has local
commits. Production was not contacted.

The LAN HAProxy declaration routes `profilarr.hollinger.asia` to the Docker
host's port 6868. This establishes intended service use, not current activity or
health
([declared proxy](../../../homelab-infra/files/pfsense/haproxy-services.yaml)).
The current Homelab Backup backend declaration has no Profilarr source mounts
and no Docker socket
([Homelab Backup declaration](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml)).

Production wiring later needs only two read-only sources, ideally exposed under
one narrow parent:

- `/docker-apps/profilarr/config/profilarr.db`; and
- `/docker-apps/profilarr/config/db`, including its `.git` directory.

Do not mount all of `/config`, its logs/backups, host root, or the Docker socket.
Grant the Homelab Backup process read access with a dedicated group or ACL; it
does not need UID 0, the Profilarr API key, a Git PAT, or write access to either
source.

Profilarr 1.1.5 never changes SQLite's journal mode, so a normal application-
created database uses SQLite's default rollback journal rather than WAL. A
single-file read-only bind is selected only under that exact condition. The
plugin must inspect `PRAGMA journal_mode` and stop if it is `wal` or if a hot
journal/recovery condition is detected; a later WAL deployment needs an audited
mount/snapshot contract that includes its companion files.

### Exact image and source provenance

Profilarr tag `v1.1.5` resolves to commit
`21c8eaeb93241588323672866854275ff7dbed67`. At research time, the deployed
Docker tag resolved to OCI index
`sha256:8033e9c6d6995f37625afeb93d7020e99566f549ae83b65f1db7e11048952d0f`
and Linux/amd64 manifest
`sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad`.
Use the amd64 digest for the local drill rather than relying on the mutable tag
([exact source](https://github.com/Dictionarry-Hub/profilarr/tree/21c8eaeb93241588323672866854275ff7dbed67),
[release](https://github.com/Dictionarry-Hub/profilarr/releases/tag/v1.1.5)).

The image sets `/config` as the persistent root and starts Gunicorn only after a
root entrypoint recursively changes `/config` ownership, then drops to the
configured PUID/PGID with `gosu`
([Dockerfile](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/Dockerfile),
[entrypoint](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/entrypoint.sh)).
This is relevant only to isolated restore setup. It does not justify running the
backup plugin as root.

Upstream marks 1.1.5 as the final, unmaintained v1 release and states that v2 is
not compatible with v1 databases or configurations. This plugin must therefore
be explicitly version-scoped. A future v2 deployment is a new research and
plugin contract, not a transparent continuation of this one
([v1.1.5 release notice](https://github.com/Dictionarry-Hub/profilarr/releases/tag/v1.1.5)).

## Where authoritative state lives

### Git repository: definitions and local history

Profilarr hardcodes `/config/db` as `DB_DIR` and these application directories
within it:

- `regex_patterns/`;
- `custom_formats/`;
- `profiles/`; and
- `media_management/`.

The app reads and writes YAML there, caches it only in process memory, and uses
Git operations for clone, stage, commit, push, pull, branch, merge, conflict
resolution, revert, rename, and deletion
([path configuration](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/config/config.py),
[data implementation](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/data/utils.py),
[Git API](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/git/__init__.py)).
The first-party product description likewise identifies Git-based version
control, preservation of local customizations, and conflict resolution as core
behavior
([v1 product documentation](https://v1.dictionarry.dev/)).

Classify repository state precisely:

- **Remote-rebuildable:** commits and tags reachable from a verified remote ref.
- **Locally authoritative:** commits/branches/tags/notes/stashes reachable only
  from local refs. `git bundle --all` preserves these refs and objects.
- **Not accepted for backup:** dirty tracked files, staged but uncommitted index
  state, untracked application YAML, unresolved conflicts, merge/rebase/cherry-
  pick/bisect state, or an unborn/invalid repository. These are neither a
  committed recovery point nor safely capturable while Profilarr remains live.
- **Disposable/excluded:** caches and generated/transient files that are neither
  tracked Git content nor one of the four authoritative application directories.

Git bundles preserve refs and reachable objects, not the working tree, index,
reflogs, hooks, or repository configuration. That limitation is intentional:
the plugin requires a clean worktree/index and rebuilds it from the recorded
commit. It records the symbolic branch, HEAD, ref digest, and sanitized upstream
identity in the private manifest. It does not copy `.git/config`, because
Profilarr can place an authentication token directly into a private clone URL
([Git authentication source](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/git/auth/authenticate.py)).

Inventory every regular file under the four authoritative directories. Each
must be tracked and reachable from the captured commit. Reject untracked or
ignored YAML/data there; an ignore rule must not silently hide unique state.
Reject submodules, Git LFS pointers, alternates, partial/shallow repositories,
replace refs, or missing objects unless a future contract proves their external
objects are also self-contained. A full v1 profile database should be ordinary
Git objects only.

### SQLite database: non-Git control plane

Profilarr hardcodes `/config/profilarr.db` and uses ordinary Python SQLite
connections
([database connection](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/db/connection.py)).
Its migrations define these authoritative tables:

- `arr_config`: target name/type, Radarr/Sonarr URL, API key, tags, selected
  profiles/formats, sync method/interval, unique-import choice, last run and task
  linkage;
- `scheduled_tasks`: repository refresh, backup, and configured import schedules;
- `settings`: linked `gitRepo`, generated Flask secret, auto-pull state, PAT
  presence marker, and other settings;
- `auth`: username, password hash, Profilarr API key, and session ID;
- `format_renames`: locally selected rename policy;
- `language_import_config`: the local language import score;
- `migrations`: schema versions; and
- `backups` and `failed_attempts`: native backup history and temporary login
  throttling history.

See the
[initial schema](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/db/migrations/versions/001_initial_schema.py)
and
[later migrations](https://github.com/Dictionarry-Hub/profilarr/tree/21c8eaeb93241588323672866854275ff7dbed67/backend/app/db/migrations/versions).
Arr targets, API keys, selected sync content, auth, and policy are unique local
state and justify the plugin even if all profile Git commits were pushed.

The database is secret-bearing. The artifact may contain API keys, password
hashes, session secrets, private repository URLs, and internal addresses. Never
log SQL rows or values, expose them through sidecars/metrics, or use a database
value as an artifact filename. `backups` and `failed_attempts` are not valuable
by themselves, but retaining the complete coherent database is safer than a
selective logical rewrite and preserves the exact app-supported schema.

### Explicitly excluded

- `/config/log/**` and rotated logs.
- `/config/backups/**` and all native Profilarr backup ZIPs; Homelab Backup owns
  artifact retention.
- Repository working-tree/index/conflict state because the backup precondition
  requires a clean, settled commit.
- Git reflogs, hooks, credential-bearing `.git/config`, lock files, temporary
  pack state, caches, and unreachable/dangling objects.
- Git credentials supplied through `PROFILARR_PAT` or another infrastructure
  secret; credentials belong to deployment secret management.
- Radarr/Sonarr databases, profiles as applied inside those services, media,
  download state, and their runtime history. Their own backups remain required.
- Container layers, process memory, Docker state/socket, reverse proxy, DNS,
  TLS, compose, and host/NAS configuration.

## Safe live backup contract

### Consistency boundary

No downtime is needed when the repository is clean and stable.

1. Acquire Homelab Backup's per-target overlap lock.
2. Resolve both configured source paths under exact allowlisted read-only mount
   roots with no symlink escape.
3. Establish repository fence A: require a valid non-shallow Git repository;
   no Git lock or in-progress operation; a symbolic checked-out branch; clean
   index and tracked worktree; no untracked/ignored application data; complete
   objects; and record HEAD plus a sorted digest of all refs.
4. Open `profilarr.db` read-only through SQLite and use
   `sqlite3.Connection.backup()` to create a new private temporary database.
   This is a database transaction snapshot and tolerates concurrent app DB
   writes without raw-file copying.
5. Run `git bundle create <private-temp>/repository.bundle --all` against the
   source repository. This writes only outside the source mount.
6. Establish repository fence B using the same checks. Require identical HEAD,
   ref digest, symbolic branch, index/tree state, and authoritative-file
   inventory. If they differ, discard temporary output and retry a bounded
   number of times; then fail as “repository changing/dirty.”
7. Validate the SQLite snapshot and bundle, construct a deterministic private
   manifest, and publish a single archive atomically through
   `write_backup_bytes()` or `create_backup_artifact()`.
8. Reread and validate the published artifact/sidecar before returning
   `artifact_path`.

The database snapshot and Git ref snapshot are not one filesystem-atomic instant,
but the stable Git fence makes the only cross-store definitions immutable over
the SQLite snapshot interval. Independent DB activity is captured transactionally.
If a Profilarr file edit makes the tree dirty or a commit/pull changes refs, the
fence rejects the run. This is a truthful, retryable live boundary rather than a
timing assumption.

All source Git inspection and bundling runs with optional locks disabled
(`GIT_OPTIONAL_LOCKS=0` / `git --no-optional-locks`) so commands cannot refresh
or rewrite the source index. Any command that cannot complete against the
read-only mount fails the run; the plugin never retries by making the repository
writable.

Do not trigger `POST /api/backup`, download its ZIP, stop/restart Profilarr, or
write a marker/lock into its repository. No application or Git credential is
needed. `test()` is read-only: confirm source readability and type, database
schema/integrity, repository validity/cleanliness/self-containment, and return
specific redacted errors.

### Artifact format and validation

Use one deterministic archive containing exactly:

- `profilarr.db`: the SQLite online snapshot;
- `repository.bundle`: a full self-contained bundle of all refs; and
- `manifest.json`: private versioned restore metadata.

The manifest contains artifact-format version, Profilarr/image version, SQLite
schema/migration versions, database hash/size and non-sensitive table counts,
bundle hash/size, object format, symbolic branch, HEAD, sorted ref names/tips,
clean-state proof, and creation timestamps. It does not contain API keys,
password data, internal URLs, remote credentials, SQL values, profile names, or
private commit messages. The external sidecar contains only the minimum generic
artifact hashes/sizes and redacted capability metadata.

Validation must:

- enforce exact archive names, member types/count, bounded compressed and
  uncompressed sizes/ratio, no encryption/duplicates/extra paths/traversal/
  links/devices, and matching SHA-256 values;
- open SQLite read-only, require its header, `PRAGMA quick_check`,
  `PRAGMA foreign_key_check`, exact v1 migrations 1–4, the expected tables and
  columns, sane single-auth/user assumptions, and no active temp/corrupt schema;
- run `git bundle verify`, require no prerequisites, list and bind every bundle
  ref, require the manifest HEAD to be reachable, clone it into a temporary
  validation root, run `git fsck --full`, and verify the checked-out authoritative
  file inventory against the manifest;
- parse every tracked application YAML safely and verify Profilarr's required
  top-level fields for regexes, formats, and profiles without logging contents;
- reject token-bearing remote URLs in public metadata and treat the entire
  artifact as secret-bearing even when scans find no apparent key; and
- prove source and published artifact files were not mutated during hashing.

## Restore contract

Declare `restore_capability = "full"` for Profilarr 1.1.5 application state.
Restore is local/dev-only and create-only:

1. Require an absent isolated destination and the exact pinned image. Validate
   artifact, sidecar, manifest, SQLite, and bundle before creating service paths.
2. Stream `profilarr.db` into the new `/config/profilarr.db` with restrictive
   permissions; do not restore WAL/journal files.
3. Clone `repository.bundle` into new `/config/db`, restore the exact symbolic
   branch and HEAD, recreate local refs, and verify a clean worktree, ref digest,
   file inventory, and `git fsck`. Never extract `.git` bytes from an archive.
4. Recreate a sanitized `origin` only from the database's linked repository URL
   after rejecting embedded credentials and unsafe schemes. It is acceptable to
   boot with no network remote; Git connectivity is a separate post-restore
   dependency. Never copy `.git/config` from production.
5. Start the exact image in an ephemeral network with no route to production or
   the internet. Provide deterministic local mock Radarr/Sonarr and Git origins
   only when exercising integration behavior.
6. Verify authentication, settings, Arr targets/selections/schedules, Git branch
   and commit history, profiles/formats/regexes/media-management definitions,
   and compilation/read paths. Do not run a sync that writes to real Arrs.

The restore does not recreate Radarr/Sonarr state, push local-only commits, or
recover Git-hosting credentials. Those are explicit dependencies, not missing
Profilarr state. A full service recovery must restore the Arr services separately
and provision any desired Git PAT before enabling scheduled imports/pulls.

Never use Profilarr's native restore or import endpoints. Version 1.1.5 validates
only that an uploaded ZIP contains `profilarr.db`, then uses
`ZipFile.extractall()`, removes/replaces live directories, and overwrites the
running config without a safe archive/member contract
([backup API and validator](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/backup/__init__.py),
[restore implementation](https://github.com/Dictionarry-Hub/profilarr/blob/21c8eaeb93241588323672866854275ff7dbed67/backend/app/task/backup/backup.py)).

## Exact local two-backup/two-restore drill

All credentials, repositories, hosts, profile names, and Arr objects are
synthetic. Use the pinned Linux/amd64 image, temporary directories, a local bare
Git origin, and deterministic mock Radarr/Sonarr HTTP servers. The Profilarr and
restore networks have no production route or general internet egress.

### Fixture and backup A

1. Start a fresh exact Profilarr 1.1.5 instance with temporary `/config` and
   synthetic PUID/PGID. Complete auth setup with synthetic values.
2. Through Profilarr's supported API/UI, link the local bare origin and create
   or import one regex, one custom format, one profile, and one media-management
   definition. Commit the work through Profilarr; create a second local branch
   and tag, and leave one commit deliberately unpushed while returning to the
   primary clean branch. Do not configure a real PAT.
3. Add one mock Radarr and one mock Sonarr target with synthetic keys, different
   selected profiles/formats, and deterministic manual/interval schedules.
   Set language-import, auto-pull, and rename choices. Record expected state by
   hashes/counts, not secret values.
4. Mount only the database file and Git repository read-only into the local
   Homelab Backup backend. Run `test()` and backup A while Profilarr stays live.
   During the SQLite copy, make a harmless DB-only update through the local API
   to prove the online snapshot remains valid.
5. Verify A's SQLite integrity and v1 schema, bundle self-containment, all local
   refs including the unpushed commit, clean checkout at the recorded HEAD, exact
   manifest/hash binding, absence of logs/native backups/`.git/config`, and no
   secret values in logs, metrics, exceptions, sidecar, or filenames.

### Mutation and backup B

6. Through supported flows, modify the profile and custom format, add a second
   profile, change the Arr selection/schedule and language score, commit on the
   primary branch, and add another local-only branch/ref. Ensure the worktree
   and index are clean.
7. Run backup B live. Assert A is immutable; B has different database, bundle,
   manifest, and artifact hashes; B contains all new refs and profile state; and
   only one successful artifact is associated with each run.

### Restore A and B independently

8. Restore A into absent root A. Prove only the database and reconstructed Git
   repository are created, both match the manifest, and no production remote or
   token is configured. Start the exact image offline against mock dependencies.
   Authenticate and verify A's Arr targets/selections/schedules, settings,
   primary branch/HEAD/tag/local-only commit, and profile/format/regex/media-
   management state. Prove B-only state is absent.
9. Restore B into separate absent root B and repeat. Verify every B mutation and
   additional local ref; prove A and B are observably distinct.
10. For each restore, configure only the local mock Git origin/Arr endpoints in
    a disposable verification copy. Exercise a dry/read/compile path and a sync
    against the mocks, proving no production or public host was contacted and
    the recovered configuration is operational. Tear down all secret-bearing
    fixtures afterward.

### Required negative proofs

Automated tests and the drill also cover:

- source database missing/unreadable/replaced, busy/timeout, online-backup
  interruption, WAL/hot-journal mode, corruption, wrong migrations/schema/
  version, failed integrity or foreign-key checks, artifact-helper failure,
  cancellation, and disk full;
- no repository, unborn or detached HEAD, shallow/partial repo, alternates,
  missing/corrupt objects, invalid refs, submodule/LFS/replace-ref dependency,
  active Git lock/merge/rebase/cherry-pick/bisect, dirty index/worktree, and
  untracked or ignored application YAML;
- HEAD/ref/status/file-inventory change during capture, bounded retry followed by
  honest failure, and overlap skip;
- invalid/thin/prerequisite/corrupt bundle, manifest/ref mismatch, checkout
  mismatch, YAML parse/schema failure, missing local-only refs, and secret
  leakage from a credential-bearing source `.git/config`;
- archive duplicates/extras, traversal/absolute/link/device/encrypted members,
  decompression bomb, hash/sidecar mismatch, and source mutation while hashing;
- restore destination exists, production-like destination or network, wrong
  image/version, overwrite attempt, unsafe remote scheme/embedded credential,
  missing mock dependency, and network escape; and
- spy transports proving no native Profilarr backup/restore/import/delete API,
  no production service, and no remote Git/Arr host is contacted by backup.

## STOP conditions

Stop without reporting a successful backup or restore when any of these holds:

- any production restore, native restore/import call, production Arr sync, or
  production filesystem write would occur;
- the source is not the exact v1.1.5 contract or the image/database migration
  differs; v2 requires a new design because upstream declares it incompatible;
- the Git repository is dirty, detached/unborn, incomplete, shallow/partial,
  corrupt, in an operation/conflict, changes across the fence, contains
  unsupported external object dependencies, or has authoritative untracked/
  ignored files;
- SQLite online snapshot, quick/integrity/foreign-key/schema validation, Git
  journal-mode check, bundle verification/fsck/ref binding, YAML validation, or
  artifact hash/sidecar validation fails;
- source paths escape their two narrow read-only allowlists, require root/write
  access, or would require all `/config`, logs/backups, Docker socket, host root,
  app API credentials, or Git PAT;
- any credential, internal URL, SQL value, repository content/profile name,
  commit message, or private path would enter logs, errors, metrics, sidecars, or
  public metadata;
- the artifact would include `.git/config`, hooks, logs, native backups, temp
  files, WAL/journal residue, or raw recursively copied live repository bytes;
- no unique clean snapshot can be completed within bounded retries/deadline;
- restore destination exists or aliases production, the exact image cannot be
  pinned, network isolation cannot be proved, or repository reconstruction is
  not clean and byte/ref-equivalent; or
- either restored A/B cannot be distinguished or the unpushed local commit is
  absent, because that would disprove the core recovery promise.

## Build and activation verdict

The version-scoped plugin, validator, safe local restore, mocked tests, and exact
two-backup/two-restore drill are **fully buildable on the dev VM now**. No
downtime or product choice is required. The important product behavior is an
actionable backup failure while Profilarr has uncommitted/in-progress Git state;
the operator commits or resolves it, and the next run succeeds.

Production activation later is a small infrastructure change: add the database
file and Git repository as read-only mounts, grant non-root read access, and
create one Profilarr target. It requires no production API call, backup trigger,
service restart, lifecycle control, Docker socket, or restore. Production
restore remains absolutely forbidden.
