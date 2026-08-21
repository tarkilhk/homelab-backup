# Plan 023 approval packet: Audiobookshelf production pilot

## Status

- **Prepared**: 2026-08-21
- **Attempted**: 2026-08-21
- **State**: BLOCKED ON READ-ONLY METADATA INVENTORY
- **Production writes performed**: two versioned deployments and two
  unscheduled targets were created; both validation attempts failed closed and
  both deployments and targets were rolled back
- **Production restore**: forbidden

## Pilot outcome

Release `v0.3.1` was cut from commit `a0683550d7e99dc9011f916cc49ac763d0f53cfc`
after the complete main and tag workflows passed. The published OCI indexes are:

- backend: `sha256:3260322f88d98eb57ea868f72b2a44bfef10716b9585357a8290edff8e5e4682`;
- frontend: `sha256:60b1828a66aae1d8c94bf25c96073c01a944f875e22b99d62a8ac9474d89bdfb`.

The exact infrastructure change was deployed in `homelab-infra` commit
`017c4e5`. Production returned healthy and ready, exposed the Audiobookshelf
plugin with `partial` restore capability, and retained all sixteen existing
targets and jobs. Target `17` was then created with only the two approved
read-only paths. Its mandatory non-mutating connectivity test failed closed:

```text
Audiobookshelf database schema is not exact 2.36.0
```

The live unauthenticated status endpoint independently reported server version
`2.36.0`, so this is a live-schema difference rather than an application-tag
drift. No job, schedule, Run, TargetRun, artifact, or sidecar was created.

The failed unscheduled target was deleted, infrastructure commit `017c4e5` was
reverted by commit `7abd915`, and GitOps restored the prior healthy deployment.
The final read-only audit again found the original plugin catalog and sixteen
targets. No Audiobookshelf state was written, stopped, restarted, or restored.

A schema-only read of the live database then confirmed `version` and
`maxVersion` are both `2.36.0`. Every required table and column matched the
fresh-image contract; the sole difference was Sequelize's native legacy
`SequelizeMeta(name)` migration table. The validator now permits only that
exact optional table, has positive and malformed-table regressions, and still
rejects all other extra state. The updated exact-image drill preserved the
upgrade-path table through two backups, two fresh restores, and app
boot/restart verification in two clean runs (`62.47s` and `42.13s`).

Release `v0.3.2` was then cut from commit
`2ff70fac987d9b4b924071fff1a09c5b2837542f`. Both its main workflow
(`32451533012`) and tag workflow (`32452073028`) passed. The published OCI
indexes and linux/amd64 manifests are:

- backend index `sha256:ad530752dc5444a91aa0b248cc7ebdd91bd3131f08791a10b7406fb9f6465a92`,
  amd64 `sha256:a34a13e9a1851ffe7627eadb131c51180a0bfa5dcc0929cf6106de023f542207`;
- frontend index `sha256:3b4724dc8fdf3fbb1608e84b5dbdab1d40c3bc054e4f00bf3d1f82ff960e7d24`,
  amd64 `sha256:deb5ed857aa28dba04680bc53006ea61e52d81fecd6de533ed2e31bfe6f7f6e7`.

Infrastructure commit `ddb9be9` deployed those images and the same two
read-only mounts. Production returned healthy and ready, retained all sixteen
existing targets and jobs, and exposed the partial Audiobookshelf plugin. The
new target reused ID `17`; its non-mutating test accepted the corrected
database contract but failed closed while reading the mounted metadata roots:

```text
Audiobookshelf filesystem operation failed
```

No schedule, Run, TargetRun, artifact, or sidecar was created. Target `17` was
deleted and infrastructure commit `8cfd8e5` reverted the deployment. The final
audit found the prior healthy/ready plugin set and the original sixteen targets
and jobs. The next retry is blocked until a one-shot read-only inventory proves
whether `/metadata/items` and `/metadata/authors` exist and are traversable and
whether either tree contains a symlink or non-regular entry.

## Read-only inventory

The dev server successfully reached the declared primary production backend at
`docker.tarkilnetwork:18080` and observed:

- `/health`: healthy;
- `/ready`: ready;
- deployed images declared as Homelab Backup backend/frontend `v0.2.1`;
- no deployed `audiobookshelf` plugin in `/api/v1/plugins/`;
- sixteen existing targets and sixteen jobs;
- daily jobs occupy 15-minute slots through 05:15 Asia/Singapore; and
- existing jobs use no explicit retention override.

Only non-secret names, identifiers, schedules, statuses, and protection facts
were read. Plugin configuration JSON and credential values were not printed or
retained.

The declared source is Audiobookshelf 2.36.0 on the same Docker VM:

- `/docker-apps/audiobookshelf/config:/config`;
- `/docker-apps/audiobookshelf/metadata:/metadata`; and
- `/mnt/nas-media/eBooks/audiobooks:/audiobooks`.

The plugin needs the first two roots only. The media root remains excluded.

## Approval A: versioned Homelab Backup release

Retry release: `v0.3.2`. Release `v0.3.1` remains immutable and valid, but its
strict fresh-database schema contract rejected the legitimate production
upgrade-path table before any backup was attempted.

Before release:

1. finish and commit the current Plan 022 closure;
2. run the repository's single final backend/frontend release gate;
3. synchronize `VERSION`, backend/frontend package versions and lockfile;
4. record the focused fix in the dated `0.3.2` section;
5. merge the exact reviewed commit to `main`;
6. create and push `v0.3.2` only after main CI passes; and
7. record the published backend/frontend image digests.

Do not deploy a floating `latest`, branch build, dirty tree, or the WIP
checkpoint.

## Approval B: primary backend Compose change

After the immutable `v0.3.2` images exist, change only
`docker.compose/system/homelab-backup/homelab-backup.yaml` in `homelab-infra`:

```diff
 services:
   backend:
-    image: tarkilhk/homelab-backup:backend-v0.2.1
+    image: tarkilhk/homelab-backup:backend-v0.3.2
     volumes:
     - /mnt/nas-shared/backup/homelab-backup:/backups
     - /docker-apps/homelab-backup/db:/app/db
     - /docker-apps/jellyfin/config/data/backups:/jellyfin-backups
+    - /docker-apps/audiobookshelf/config:/sources/audiobookshelf/config:ro
+    - /docker-apps/audiobookshelf/metadata:/sources/audiobookshelf/metadata:ro
```

Update the frontend to `frontend-v0.3.2` in the same reviewed deployment commit.
Do not change the NAS deployment during this pilot.

This grants no Audiobookshelf network, API credential, media path, Docker
socket, or lifecycle authority. The protected Audiobookshelf container is not
restarted. Only the Homelab Backup stack is redeployed through its existing
GitOps/Portainer path.

Post-deploy read-only checks:

- backend and frontend report healthy/ready;
- discovery contains `audiobookshelf` with `partial` restore capability;
- both source paths are genuine read-only mounts;
- neither source can be opened for write;
- no media path is visible to the backend; and
- existing targets/jobs remain present.

## Approval C: production target

Create exactly one target after Approval B passes:

```json
{
  "name": "Audiobookshelf",
  "plugin_name": "audiobookshelf",
  "plugin_config_json": "{\"config_path\":\"/sources/audiobookshelf/config\",\"metadata_path\":\"/sources/audiobookshelf/metadata\"}"
}
```

Expected slug: `audiobookshelf`. The configuration contains no secret.

Immediately call the target's non-destructive `/test` endpoint. Stop without
creating a job unless it proves the exact 2.36.0 SQLite and metadata contract
through the two read-only mounts.

## Approval D: schedule and first backup

After the target test succeeds, use its automatically created tag to create:

```json
{
  "name": "Daily Audiobookshelf Backup",
  "schedule_cron": "30 5 * * *",
  "enabled": true
}
```

- Timezone: backend deployment timezone, Asia/Singapore.
- Retention override: none, matching existing production jobs.
- First validation: allow the schedule to dispatch naturally. Do not use a
  manual trigger unless separately approved to accelerate the pilot.

## Acceptance evidence

The pilot passes only when:

1. the scheduled parent Run and final TargetRun succeed;
2. the artifact and sidecar are regular private files under
   `/backups/audiobookshelf/<date>/`;
3. recorded and independently calculated size/SHA-256 match;
4. the strict Audiobookshelf validator confirms the expected SQLite and bounded
   metadata archive without media bytes;
5. logs, sidecar, and API evidence contain no private metadata path or secret;
6. `/api/v1/protection/targets` reports no gap for Audiobookshelf; and
7. the measured duration, size, and source effect are reviewed before Wave 2.

Then update the Audiobookshelf ledger row from `plugin-local` to
`verified-plugin` and commit the redacted evidence separately.

## Failure and rollback

If deployment or the target test fails, do not create the schedule. Revert the
single infrastructure commit through the normal GitOps path and redeploy the
prior versioned images.

If the scheduled backup fails, disable only the new job, preserve its run
evidence and any published artifact, and diagnose before retrying. Do not delete
or modify Audiobookshelf state and never attempt a production restore.

## Human access required before retry

Release, deployment, target creation, validation, and rollback authority were
sufficient for both attempts. The schema blocker is closed. The only missing
evidence is a structure-only inventory of the two mounted metadata subtrees.
The preferred options are, in order:

1. an operator runs a reviewed one-shot read-only diagnostic on the Docker host
   and returns only existence, type, mode, entry-count, and traversal-error
   evidence for `/metadata/items` and `/metadata/authors`; or
2. the operator approves a temporary, reviewed Gitea Actions/Ansible diagnostic
   that mounts only the Audiobookshelf metadata root read-only, has no network,
   emits only those allowed structural fields, and removes itself after
   capture.

Do not grant a general production shell, Docker socket, Portainer token,
Audiobookshelf administrator credential, writable mount, or metadata/media
access. Those are unnecessary and materially broader than the blocker.
