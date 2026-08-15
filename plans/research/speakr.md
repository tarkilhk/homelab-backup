# Speakr / WhisperX backup and restore research

Research date: 2026-08-15

Scope: the exact Speakr and custom RunPod WhisperX adapter deployment declared
in `homelab-infra`, Speakr `v0.10.3-alpha`, and the exact deployed adapter
revision. No production host, endpoint, database, object store, or RunPod
account was contacted. No production state was read or changed.

## Decision summary

Speakr is declared as a public, monitored application and is intended to be the
authoritative home for recordings, transcripts, summaries, notes, speaker
profiles, sharing state, and user configuration. Its runtime activity cannot be
proven during this research because production contact is expressly forbidden.

For the deployed local-storage configuration, the authoritative backup boundary
is the complete contents of both persistent Speakr volumes:

- `/data/instance`, principally the SQLite database and persistent application
  secret; and
- `/data/uploads`, including final audio/video, staging data, and in-progress
  recording-session chunks.

Speakr's own backup instructions call the SQLite database, uploads, and `.env`
the three essential components and require stopping the container before the
copy. In this homelab, environment configuration and secrets are already owned
by `homelab-infra` and its secret-delivery system, so they are restore
prerequisites rather than duplicate artifact contents. The plugin must record
required variable **names**, never their values
([Speakr backup FAQ](https://murtaza-nasir.github.io/speakr/faq/#how-do-i-backup-my-speakr-data)).

The WhisperX adapter and its RunPod GPU pod are processing infrastructure, not
the record of truth. The adapter's persisted pod ID and lock file are ephemeral
leases and must not be backed up. Completed transcripts, summaries, diarization,
and speaker embeddings are already stored by Speakr in SQLite.

The production deployment is on the isolated DMZ VM, while the one Homelab
Backup backend has neither a Speakr data mount nor a declared file-transfer
path into the DMZ. A reliable production target therefore needs a user decision:

1. **Recommended:** approve a short maintenance window and a dedicated
   forced-command SSH helper on the DMZ VM. It may stop only `speakr`, stream a
   read-only archive of the two declared data directories, and always restart
   `speakr` in a trap. It exposes no shell and implements no restore command.
2. If the DMZ storage supports it, stop Speakr only long enough to capture one
   atomic filesystem snapshot, restart it, and stream that immutable snapshot.
   No such snapshot capability is declared today, so it must not be assumed.
3. Run a separate Homelab Backup worker in the DMZ. This is technically viable
   but conflicts with the stated preference for one actual application instance.

An application-API export, live raw copy, general SSH/SFTP credential, Docker
socket, or reuse of the logs-only reverse tunnel is not an acceptable fallback.

**Feasibility verdict:** the plugin core, validation, safe create-only restore,
and exact two-backup/two-restore drill are buildable on the dev VM. A production-
usable target is **not fully buildable without the user's DMZ transport and
maintenance-window decision**. The selected honest capability is
`restore_capability = "partial"`, chiefly because infrastructure secrets,
external model services, RunPod template state, and currently unpersisted VAPID
keys are outside the artifact.

## Exact deployed stack

The declaration inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/dmz/speakr/speakr.yaml`](../../../homelab-infra/docker.compose/dmz/speakr/speakr.yaml):

| Component | Exact declaration | Persistent host state |
| --- | --- | --- |
| Speakr | `learnedmachine/speakr:0.10.3-alpha-lite`, UID/GID `1000:1000`, 640 MiB limit | `/opt/dmz/data/speakr/uploads:/data/uploads`; `/opt/dmz/data/speakr/instance:/data/instance` |
| WhisperX adapter | `tarkilhk/speakr-adapter:sha-4f51cd943df8`, 128 MiB limit | `/opt/dmz/data/speakr/pod-state:/data` |
| RunPod log drain | same adapter image, one-shot `python -m adapter.cli_drain` | same pod-state directory |

Speakr's healthcheck requests `/api/config` on localhost port 8899; the compose
comment says this exercises SQLite. The adapter healthcheck requests its local
port 9000. All three services receive the common non-secret
[`speakr.env`](../../../homelab-infra/docker.compose/dmz/speakr/speakr.env) and
the host-managed `/opt/dmz/secrets/dmz-secrets.env`. Secret values were not read
or reproduced.

The declared integration is:

- Speakr posts ASR work to `http://whisperx-adapter:9000/asr` and requests
  diarization and speaker embeddings;
- the adapter can deploy one RunPod pod from a configured template, waits for
  the authenticated WhisperX wrapper, and terminates the pod after idle time;
- text-model requests go to an OpenAI-compatible external endpoint; and
- registration is disabled and an initial admin identity is configured through
  environment variables.

The public application is referenced by the blackbox-monitoring declaration and
the mobile-upload runbook in `homelab-infra`, and recent deployment history pins
its images. These facts establish declared operational intent, not live
activity. A later production backup trigger may be used only after the plugin
and transport are approved; this research performed no such trigger.

### Version and image provenance

The deployed Speakr tag corresponds to upstream tag `v0.10.3-alpha`, commit
`b2dc897677035321737eee07906d5ce0a2d8add2`. At research time, the public image
resolved to OCI index
`sha256:c4d5047252cbaa82987104e9e3378e205c1d4a6c9080a7f3326b021a4b944ed5`
and Linux/amd64 manifest
`sha256:ace85fb4723698754f1f2e92d493787dfd1e738ecc5fb015baa0295b3b9404da`.
Pin that digest for the drill; if its embedded source/version does not match the
tag commit, stop.

The custom adapter tag is the prefix of exact source commit
`4f51cd943df85c1483f3bf115f47faf30633ff9d`. Its public image resolved to OCI
index `sha256:e04e9c3a936021de9ac9c711c75f2cb434f2601c29e3364ef0ffa6852298756d`
and Linux/amd64 manifest
`sha256:9ae745dda963171cafa47b55ce1a868b02ebe16227ef88245bb4a23f8135c140`.
The image/template used inside RunPod is external state and is not pinned by the
DMZ compose declaration.

Primary source:

- [Speakr tag source](https://github.com/murtaza-nasir/speakr/tree/b2dc897677035321737eee07906d5ce0a2d8add2)
- [Speakr Dockerfile at that commit](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/Dockerfile)
- [adapter source at deployed commit](https://github.com/tarkilhk/speakr-runpod-whisperx/tree/4f51cd943df85c1483f3bf115f47faf30633ff9d)

## Authoritative state boundary

### `/data/instance`

The exact image defaults to
`sqlite:////data/instance/transcriptions.db`, sets
`HF_HOME=/data/instance/huggingface`, and generates an application secret at
`/data/instance/secret_key` when no explicit secret is configured
([Dockerfile](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/Dockerfile),
[secret-key resolution](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/src/utils/security.py)).
The compose file does not declare `SECRET_KEY`, `SECRET_KEY_FILE`, or a database
override. The external secrets file was deliberately not inspected, so the
plugin must detect and report whether the persistent key exists rather than
assume it.

The SQLite database is authoritative for, among other state:

- users, password hashes, SSO associations, preferences, limits, and roles;
- recordings, original file locators and hashes, processing status, language,
  transcription, summaries, notes, prompts, and metadata;
- transcript chunks, semantic embeddings, speaker profiles, voice embeddings,
  and speaker snippets;
- groups, memberships, folders, tags, templates, internal/public shares, share
  audit data, and per-user share state;
- API-token hashes, webhooks and delivery history, push subscriptions, system
  settings, usage accounting, events, and Inquire sessions; and
- processing jobs and server-side recording-session rows.

These are visible in the exact version's
[model package](https://github.com/murtaza-nasir/speakr/tree/b2dc897677035321737eee07906d5ce0a2d8add2/src/models).
The durable job queue resets orphaned `processing` work to `queued` on startup,
so retaining job rows is part of crash recovery
([job recovery](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/src/services/job_queue.py)).

The `instance/huggingface` subtree is a regenerable embedding-model cache. The
upstream FAQ nevertheless prescribes backing up the whole `instance` directory.
The initial plugin should include it for a simple, upstream-aligned boundary;
excluding it later is a separately measured optimization, not an implicit
default.

### `/data/uploads`

The deployment leaves `FILE_STORAGE_BACKEND` unset in the checked-in
configuration. The exact source therefore selects `local` and uses
`/data/uploads`, with `/data/uploads/_staging` as its staging default
([application storage config](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/src/config/app_config.py)).
This must still be verified from the stopped source's locators and non-secret
effective configuration before each run; the source supports mixed historical
local/S3 locators after migration.

Include the entire uploads tree:

- final audio/video files referenced by recording rows;
- server-side recording chunks and `session.json` files under
  `_sessions/<session-id>`; and
- `_staging`, conservatively, because a stopped complete-volume archive is safer
  than guessing whether a file was between lifecycle phases.

Recording sessions are deliberately durable across disconnects. The exact
source records their state in SQLite and stores chunks in `_sessions`; startup
cleanup can re-enqueue finalization, preserve active sessions, or recover
abandoned sessions with data
([recording-session guide](https://murtaza-nasir.github.io/speakr/admin-guide/recording-sessions/),
[session API and cleanup](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/src/api/recording_sessions.py)).
A backup that omits either the database rows or the chunk tree is incomplete.

### Explicitly excluded

- `/opt/dmz/data/speakr/pod-state`. It contains the active RunPod pod ID and a
  deployment lock. The adapter clears the ID when a pod disappears or is
  terminated and can deploy a replacement from configuration
  ([pod-state source](https://github.com/tarkilhk/speakr-runpod-whisperx/blob/4f51cd943df85c1483f3bf115f47faf30633ff9d/adapter/adapter/pod_state.py),
  [RunPod manager](https://github.com/tarkilhk/speakr-runpod-whisperx/blob/4f51cd943df85c1483f3bf115f47faf30633ff9d/adapter/adapter/runpod.py)).
- RunPod pods, container disks, image/model caches, TCP mappings, cloud logs,
  and provider account state. These are ephemeral processing resources.
- OpenRouter/LLM state and RunPod template/credential configuration. They are
  external restore prerequisites.
- Compose, reverse-proxy, DNS, TLS, Cloudflare, monitoring, and secret files,
  which remain infrastructure-as-code or separately managed secrets.
- Process memory, container writable layers, sockets, locks, and generated
  runtime logs.
- Any S3 objects. The declared configuration is local, and the plugin must stop
  rather than silently produce a database-only artifact if `s3://` locators or
  an S3 backend are detected
  ([Speakr storage guide](https://murtaza-nasir.github.io/speakr/admin-guide/storage/)).

### Known persistence gap: VAPID keys

The exact Speakr source generates push-notification VAPID keys in
`$CONFIG_DIR/vapid_keys.json`, with `/config` as the default
([VAPID source](https://github.com/murtaza-nasir/speakr/blob/b2dc897677035321737eee07906d5ce0a2d8add2/src/utils/vapid_keys.py)).
The DMZ declaration neither mounts `/config` nor sets `CONFIG_DIR`. Therefore a
generated VAPID private key can live only in the container layer and rotate on
recreation while push subscriptions remain in SQLite.

This is why restore capability is partial. Before anyone claims push
notifications are recoverable, change infrastructure to persist VAPID keys
(for example, set `CONFIG_DIR` to a private subdirectory of `/data/instance`),
then prove a subscription created before backup still works after local restore.
That deployment change is outside this research and should not be smuggled into
the plugin.

## Consistency and downtime contract

Speakr's official backup procedure is unambiguous: stop the container first,
then archive `uploads`, `instance`, and configuration
([FAQ](https://murtaza-nasir.github.io/speakr/faq/#how-do-i-backup-my-speakr-data)).
This matters because SQLite can have live WAL/SHM state, upload processing can
rewrite or replace files, and recording sessions span database rows plus chunks.

Selected preconditions:

1. Reject new uploads at the maintenance boundary.
2. Stop only the Speakr application. The adapter may also be stopped to avoid
   wasted cloud work, but its state is not archived.
3. Confirm the Speakr process has exited; HTTP unreachability alone is not proof.
4. Hold an exclusive host-side backup lock.
5. Either stream both directories while stopped or capture one immutable
   filesystem snapshot encompassing both and restart immediately.
6. Validate that the source paths and device/snapshot identity did not change.
7. Always restart Speakr on success, error, cancellation, timeout, or client
   disconnect. Failure to guarantee restart is a hard stop.

An online SQLite backup plus a separately live uploads copy is not selected: it
cannot provide one consistency point for database locators, files being moved
from staging, and recording-session chunks. The application has no documented
complete export/restore API. A raw live archive is likewise unsupported.

## Least-privilege production shape

The existing Homelab Backup backend runs on the Docker host, not on the DMZ VM.
Its declaration has no Speakr data volume. The existing Docker-to-DMZ reverse
SSH tunnel exposes Docker-side Loki to the DMZ for outbound log push only; it is
not an inbound backup channel
([DMZ operating guide](../../../homelab-infra/docker.compose/dmz/AGENTS.md),
[tunnel variables](../../../homelab-infra/ansible/playbooks/vars/dmz_to_lan_tunnel.yaml)).
Do not reuse its restricted key or alter its purpose.

If the recommended transport is approved, provision a separate DMZ identity:

- dedicated key and unprivileged account;
- `authorized_keys` forced command, with no shell, PTY, agent forwarding, port
  forwarding, X11, SFTP, arbitrary arguments, or user-controlled paths;
- one audited root-owned helper that accepts only a backup operation, holds one
  lock, stops/starts only compose service `speakr`, and streams only the two
  literal roots;
- no restore, upload, delete, retention, Docker API proxy, or arbitrary command;
- strict host-key pinning, short connect/read deadlines, bounded archive member
  counts and sizes, and a restart trap; and
- a Homelab Backup credential readable only by its backend process.

The helper will need narrowly scoped authority to control one service and read
its UID-1000 data. Do not grant the Homelab Backup container a Docker socket,
host root, broad sudo, general DMZ SSH, or a Speakr admin/API token. Speakr's API
credential cannot quiesce and export the full database/identity boundary anyway.

## Backup artifact and validation contract

The plugin should stream one deterministic archive into
`create_backup_artifact()` or `write_backup_bytes()` and publish only after all
checks pass. The archive has two fixed top-level directories, `instance/` and
`uploads/`; it contains no absolute paths, parent traversal, device nodes,
FIFOs, sockets, symlinks, or hardlinks.

Before publication:

1. Verify the sideband protocol identifies exact helper/plugin versions, exact
   source roots, a unique quiescence ID, start/end timestamps, and stopped or
   immutable-snapshot status. Do not trust free-form remote text.
2. Require non-empty `instance/transcriptions.db` and a plausible SQLite header.
3. Open a private extracted validation copy read-only; run `PRAGMA quick_check`
   and `PRAGMA foreign_key_check`
   ([SQLite integrity checks](https://www.sqlite.org/pragma.html#pragma_integrity_check),
   [foreign-key check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)).
4. Fingerprint the exact table/schema set for Speakr 0.10.3-alpha. Unknown
   migrations or missing mandatory tables are failures, not warnings.
5. For every non-deleted local recording locator, require exactly one regular
   archive member beneath `uploads/`; compare stored size/hash when available.
6. For every active/finalizing recording-session row with chunks, require the
   corresponding safe `_sessions` directory and expected chunk sequence.
7. Record aggregate row counts, file counts, byte counts, schema fingerprint,
   artifact digest, source image/digest, plugin version, included/excluded
   boundary, and whether a persistent secret key was present.
8. Reject unexpected file types, case/path collisions, duplicate members,
   sparse/unbounded expansion, archive bombs, or any file mutation across the
   captured manifest.

The artifact is highly sensitive: it can contain raw private recordings,
transcripts, summaries, notes, names, voice embeddings, password/token hashes,
share tokens, webhook details, and a session-signing secret. Use mode `0600` and
never log filenames, titles, users, transcript content, URLs, tokens, secret
values, or row data. Sidecars contain only counts, sizes, hashes, versions, and
boundary facts.

## Honest restore contract

Declare `restore_capability = "partial"` and implement a **local/dev-only,
create-only** restore:

1. The destination must be an explicitly configured absent directory under a
   test root. Refuse an existing path, symlink, mount point, production-looking
   hostname/path, or the backup source.
2. Validate sidecar, artifact hash, archive structure, size/member limits, and
   exact plugin/schema compatibility before creating output.
3. Extract safely to a sibling temporary directory, validate SQLite and every
   database-to-upload reference again, fsync, then atomically rename into place.
4. Set private, explicit ownership/modes suitable for the isolated test
   container. Never call SSH, Docker on a remote host, RunPod, or an external
   model provider from restore.
5. Start the exact pinned Speakr image against the restored `instance` and
   `uploads`, with synthetic environment secrets and local mock ASR/LLM services.
   Confirm login, listing, playback/download, transcript/summary/notes, speakers,
   shares, folders/tags, queue/session recovery, and signing-secret continuity.

Restored historical recordings and text must work without WhisperX or RunPod.
New transcription, summarization, email/SSO, webhooks, public sharing, external
object storage, and push delivery require separately restored infrastructure and
credentials. The artifact does not recreate them. Existing push subscriptions
may be unusable because of the VAPID persistence gap.

The plugin must never restore into production, mutate a live Speakr database,
replace uploads, start/stop production during restore, or expose a remote restore
helper. A human production recovery runbook can consume a locally proven
artifact later, but implementing or executing that runbook is out of scope.

## Exact local two-backup / two-restore drill

Run entirely on the dev VM with temporary directories and outbound network
denied after pinned images are present.

### Harness

1. Pin Speakr's Linux/amd64 digest listed above. Pin the adapter digest only if
   adapter startup behavior is being tested; the historical-data restore does
   not need it.
2. Create isolated networks, temporary `instance-A`, `uploads-A`, backup, and
   restore roots. Mount no host or production path.
3. Start exact Speakr with a synthetic admin password and explicit synthetic
   `SECRET_KEY`. Point ASR at a deterministic local `/asr` mock and the
   OpenAI-compatible text connector at a deterministic local LLM mock. Route
   webhook delivery only to a local capture sink.
4. Block DNS/default egress and assert no request reaches RunPod, OpenRouter,
   Hugging Face, production DNS, or RFC1918 addresses outside the test network.

### State A and Backup A

1. Through supported local UI/API flows, create an admin and second user, API
   token, folder, tag, template, and a small deterministic WAV recording.
2. Drive processing through mocks so the recording has deterministic
   transcript chunks, summary, notes, speaker labels/embedding, and completed
   state. Create internal/public share state and one webhook aimed at the local
   sink.
3. Create an incomplete server-side recording session and upload at least two
   deterministic chunks so both its SQLite row and `_sessions` files matter.
   Optionally retain one known recoverable queued job.
4. Stop exact Speakr cleanly. Run Backup A through a local emulator of the
   selected forced-command protocol. Validate and publish A. Restart Speakr.

### State B and Backup B

1. Add a second recording with different bytes and text. Edit A's title/notes,
   tag/folder placement, speaker identity, sharing state, and webhook state.
2. Finalize or delete the incomplete A session through supported application
   behavior, then create a different incomplete B session with distinct chunks.
3. Stop Speakr, run and publish Backup B, then restart. Assert A and B hashes,
   manifests, counts, and logical state differ exactly where expected.

### Negative copies

Create copies, never modify the published artifacts:

- truncate archive and SQLite payloads;
- remove a referenced upload or session chunk;
- alter a byte covered by a hash;
- inject `../`, absolute, duplicate, symlink, hardlink, device, FIFO, oversized,
  and excessive-member entries;
- supply a wrong sidecar digest/schema/image version;
- add an `s3://` locator; and
- simulate disconnect, timeout, cancellation, concurrent backup, and remote
  non-zero exit.

Every case must fail before publication or destination mutation. The helper
emulator must prove that its restart trap runs exactly once on all exit paths.

### Restore and prove independence

1. Restore A to absent `restore-A`; restore B to absent `restore-B`. A second
   restore into either existing root must fail.
2. Start one exact Speakr container at a time against each restored pair with
   the same synthetic config and mocks.
3. For A, verify only State A exists, exact audio hashes/playback, transcript,
   summary, notes, speaker data, folders/tags/shares, API auth, secret-key
   continuity, and deterministic recovery of its session/job.
4. For B, verify all intended B changes and no contamination from A. Exercise
   playback/download and supported exports for both restored recordings.
5. Start the adapter separately with a mock RunPod API and an empty pod-state
   directory. Prove no active-pod ID was restored and historical Speakr content
   remains usable while the adapter is absent.
6. Tear down only the named temporary resources and retain machine-readable
   drill evidence: exact digests, artifact hashes, validation results, test
   counts, and zero forbidden-network requests. Never retain test secrets in
   logs or sidecars.

## STOP conditions

Stop and report rather than weakening the contract if any condition is true:

- a production restore, artifact upload, database edit, file replacement, or
  other production write is proposed; only an explicitly approved backup
  trigger/quiescence operation may ever be added later;
- the user has not selected and approved DMZ transport and maintenance policy;
- the exact source/image digest does not match Speakr 0.10.3-alpha or the exact
  adapter revision;
- effective storage is S3, mixed local/S3, or any non-local locator is found;
- Speakr cannot be proven stopped or the source cannot be proven immutable for
  the entire capture;
- active upload/session/job state cannot be quiesced or represented consistently;
- the remote mechanism exposes a shell, SFTP, arbitrary paths/arguments, Docker
  socket/API, broad sudo/root, port forwarding, or a restore/write operation;
- cancellation, timeout, disconnect, or validation failure cannot guarantee
  Speakr is restarted;
- database checks fail, schema is unknown, or a referenced upload/session chunk
  is absent, duplicated, changed, or unsafe;
- a secret value, transcript, recording name, user identity, share token, audio
  byte, or other sensitive content would enter logs or sidecars;
- archive extraction would target an existing path, symlink, mount point,
  production hostname/path, or anything outside the isolated dev root;
- any local mock attempts a real RunPod, model-provider, production, LAN, or
  internet request; or
- pod-state, RunPod disks, `.env` secret values, or unrelated DMZ state is
  proposed for inclusion.

If active push subscriptions exist and VAPID keys are not persisted, the plugin
may still produce an explicitly partial backup, but it must never claim push
delivery is recoverable. A claim of full application restore is a STOP until
that persistence gap and a local restore test are resolved.

## Decision needed before implementation reaches production

Approve or reject the recommended forced-command SSH + short maintenance-window
shape. If approving it, also decide whether downtime may last for the full
archive stream or whether infrastructure must first provide a filesystem
snapshot so downtime covers only snapshot creation. Everything else in the
plugin and local drill can be built without production access while that
decision is pending.
