# Plan 023 approval packet: Audiobookshelf production pilot

## Status

- **Prepared**: 2026-08-21
- **State**: READY FOR HUMAN APPROVAL
- **Production writes performed**: none
- **Production restore**: forbidden

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

Recommended release: `v0.3.0`, because the unreleased program adds multiple
capabilities and intentional clean-breaking artifact/config contracts after
`v0.2.1`.

Before release:

1. finish and commit the current Plan 022 closure;
2. run the repository's single final backend/frontend release gate;
3. synchronize `VERSION`, backend/frontend package versions and lockfile;
4. move Unreleased notes to the dated `0.3.0` section;
5. merge the exact reviewed commit to `main`;
6. create and push `v0.3.0`; and
7. record the published backend/frontend image digests.

Do not deploy a floating `latest`, branch build, dirty tree, or the WIP
checkpoint.

## Approval B: primary backend Compose change

After the immutable `v0.3.0` images exist, change only
`docker.compose/system/homelab-backup/homelab-backup.yaml` in `homelab-infra`:

```diff
 services:
   backend:
-    image: tarkilhk/homelab-backup:backend-v0.2.1
+    image: tarkilhk/homelab-backup:backend-v0.3.0
     volumes:
     - /mnt/nas-shared/backup/homelab-backup:/backups
     - /docker-apps/homelab-backup/db:/app/db
     - /docker-apps/jellyfin/config/data/backups:/jellyfin-backups
+    - /docker-apps/audiobookshelf/config:/sources/audiobookshelf/config:ro
+    - /docker-apps/audiobookshelf/metadata:/sources/audiobookshelf/metadata:ro
```

Update the frontend to `frontend-v0.3.0` in the same reviewed deployment commit.
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

## Human access/approval required

Codex currently has enough read access to prepare and validate this packet, but
cannot complete the rollout without:

1. approval of release version `v0.3.0` and its final merge/tag;
2. write access to the `homelab-infra` workspace if Codex should prepare the
   Compose diff there;
3. the operator's commit/push of the reviewed `homelab-infra` change, as that
   repository explicitly reserves production deployment commits for the human
   operator;
4. the operator/CI/Portainer deployment of the primary Homelab Backup stack;
5. approval to create the exact target and daily 05:30 schedule; and
6. approval for a manual backup trigger only if waiting for the first scheduled
   run is undesirable.

No new secret, Audiobookshelf credential, production SSH, Docker socket,
Portainer token, or protected-service control is required for this pilot.
