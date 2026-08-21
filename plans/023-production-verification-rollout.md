# Plan 023: Promote plugin-local services through production rollout

## Status

- **Priority**: P0
- **Effort**: XL, delivered as small independently approved waves
- **Risk**: HIGH because this changes production backup configuration
- **Depends on**: completed local plugin milestones and a clean versioned release
- **State**: TODO
- **Restore policy**: every production restore is forbidden

## Outcome

Promote each selected `plugin-local` row in `plans/007-coverage-ledger.md` to
`verified-plugin` by producing current production evidence for:

1. a versioned Homelab Backup deployment;
2. the narrow production access required by the plugin;
3. a persisted target and enabled schedule;
4. a successful non-destructive target probe;
5. a successful backup-only scheduled run; and
6. an independently validated private artifact and recovery sidecar.

Local isolated restore proof already exists for these rows. Do not repeat a
restore against production and do not create a production restore sentinel or
destination.

## Scope

This plan covers the current `plugin-local` rows only:

- Invoice Ninja and Cal.com;
- primary Gitea, NAS Gitea, and the Gitea package/OCI registry boundary;
- SFTPGo control plane;
- Homelab Backup on the Docker host and NAS;
- shared PostgreSQL databases and Hindsight;
- Termix;
- Readarr and Prowlarr;
- Radarr, Sonarr, and Lidarr after Plan 022 local completion;
- Audiobookshelf;
- Bazarr; and
- Profilarr.

Blocked, planned, external, and unprotected services are not silently included.
Each requires its own existing decision or local-completion gate.

## Responsibility boundary

| Work | Codex can perform after approval | Human action required |
| --- | --- | --- |
| Repository and release | Finish gates, prepare release changes, commit, tag, push, and verify published images | Approve the release/version and any merge required by repository policy |
| Production inventory | Inspect declared versions, mounts, networks, targets, schedules, and status without mutation | Provide access where the dev server cannot reach a host or control plane |
| Infrastructure changes | Prepare and apply focused Compose/IaC patches after reviewing the exact diff | Explicitly approve every production mount, network, credential, target, schedule, cleanup, or service change |
| Secrets | Reference existing secret names and verify redaction | Create/rotate secret values through the approved secret store; never paste them into Git or chat |
| Target and schedule | Create through the Homelab Backup UI/API after approval | Approve target configuration, cron, retention, and expected load window |
| Validation | Run `test()`, trigger or observe backup-only execution, inspect evidence, and update documentation | Confirm any service-owner policy or application-specific residual privilege |
| Restore | None in production | No human production restore is part of this plan |

If the dev server lacks authenticated production access, Codex prepares an
exact command/UI runbook and the human executes that step. Codex then validates
the resulting read-only evidence.

## Mandatory preflight

1. Start from a clean, reviewed commit. Do not deploy the current dirty Plan 022
   worktree or a floating `latest` image.
2. Finish the repository gates for the selected release, update versions and
   changelog, publish immutable backend/frontend image tags, and record their
   digests.
3. Inventory both Homelab Backup deployments and record, without secret values:
   current image digest, health/readiness, persistent database path, artifact
   root, free space, filesystem ownership, and existing targets/jobs.
4. Confirm that the artifact filesystem is durable and that the retention
   policy cannot remove the validation artifact before evidence is recorded.
5. Prepare one approval packet per rollout wave containing the exact diff,
   mounts, network attachments, credential names/scopes, target names, cron,
   retention, expected source effect, and rollback.

STOP if the running image cannot be pinned, the source identity differs from
the locally proved contract, a required path would be broader or writable, a
credential is broader than the plugin plan permits, or a secret appears in a
diff, log, command transcript, sidecar, or evidence note.

## Per-target rollout procedure

Perform these steps for one target at a time:

1. Verify the exact source application version, database/backend mode, and
   plugin-specific invariant using read-only inspection.
2. Apply only the approved narrow access change. Restart Homelab Backup only if
   the deployment change requires it; do not restart the protected service.
3. Confirm Homelab Backup readiness and prove that no unexpected path, socket,
   network, or credential became available.
4. Create the target with a unique production slug. Keep secret values out of
   exported JSON and evidence.
5. Run the target connectivity test. It must perform only the methods allowed by
   the plugin contract and return a checked, exact identity.
6. Create one enabled job with an approved cron and retention policy.
7. Let the scheduler dispatch the first backup, or use the same job's manual-run
   endpoint when the approval explicitly permits an immediate backup trigger.
8. Require successful parent Run and final TargetRun records with a nonempty
   artifact path, positive size, and complete SHA-256.
9. Independently verify the artifact and sidecar: regular files, private mode,
   correct target/date path, matching size and SHA-256, plugin/version/provenance
   fields, and the plugin's vendor-level structural validator.
10. Check logs and API responses for secret or private-path leakage and confirm
    the source experienced no action beyond the approved native export and exact
    post-publication cleanup, if applicable.
11. Query `/api/v1/protection/targets` and require no `not_scheduled`,
    `never_succeeded`, or `scheduled_backup_missing` gap for the target.
12. Record redacted evidence, update the ledger row to `verified-plugin`, and
    commit that promotion separately.

On failure, disable the new job, preserve the failed Run/TargetRun and any
published artifact for diagnosis, and revert only the approved rollout diff.
Do not retry a mutating native export or cleanup until its source state is
understood. Never attempt a restore as remediation.

## Rollout waves

### Wave 1: Audiobookshelf pilot

Use Audiobookshelf to validate the complete production process with the lowest
authority boundary:

- mount only its documented config and metadata control-plane roots read-only;
- expose no media root, network route, application credential, Docker socket,
  or lifecycle control;
- create one target and schedule; and
- produce and validate one scheduled artifact.

Stop after the pilot and review its operational load, artifact size, duration,
logs, and protection facts before approving another wave.

### Wave 2: narrow read-only filesystem sources

Roll out separately:

- Profilarr with its exact SQLite and Git mounts;
- Termix with its encrypted-state mount; and
- SFTPGo control-plane access according to its completed plan.

Profilarr must fail closed if its repository is dirty or in an interrupted Git
operation. Do not repair or commit the production repository from the backup
system.

### Wave 3: native application exports

Roll out separately:

- Bazarr with SQLite-mode proof, its narrow API credential/network, and exact
  read-only native-backup mount;
- Readarr, Prowlarr, Radarr, Sonarr, and Lidarr with immutable runtime pins,
  fixed read-only native-backup mounts, broad application API-key approval, and
  explicit approval to delete only the exactly attributed native backup after
  durable publication;
- Invoice Ninja with its exact runtime pin and API export contract; and
- primary/NAS Gitea, including separate proof that the package/OCI registry
  state is present in the protected native artifact.

Any API credential with residual write authority must be named in the approval
packet with the compensating method/path restrictions. Do not introduce UI
cookies, Docker sockets, or broad `/config` mounts as fallbacks.

### Wave 4: database sources

Roll out separately:

- Hindsight with its approved private network and dedicated denied-write dump
  identity;
- Cal.com after recording the actual application/PostgreSQL runtime digests,
  DMZ database-only route, and dedicated denied-write grants; and
- every authoritative PostgreSQL database as an individually enumerated target
  with its runtime version, owner, role/default grants, and schedule.

STOP a database rollout on RLS, unsupported objects, unexpected extensions,
wrong server/client major version, excessive grants, or any write capability
not explicitly allowed by the completed plugin plan.

### Wave 5: Homelab Backup self-protection and duplicate deployments

Create one self-backup target/job on each Homelab Backup deployment and prove
their state separately. Confirm that the NAS and Docker-host instances have
distinct databases, target slugs, artifact roots, and run evidence. Do not make
one instance's success stand in for the other.

## Evidence record

For every promoted row, record only non-secret evidence:

- release version, commit, backend/frontend image digests, and deployment;
- protected service exact version/digest and target slug;
- approved access shape and secret reference name, never its value;
- job ID/name, cron, timezone, retention, and first scheduled time;
- Run/TargetRun IDs, start/end time, duration, and final status;
- artifact basename, byte size, SHA-256, sidecar basename, and private modes;
- structural validator result and approved source cleanup result;
- `/api/v1/protection/targets` result and log-redaction check; and
- the commit that changes the ledger category to `verified-plugin`.

## Completion

Plan 023 is complete only when every selected row is either:

- `verified-plugin` with the evidence above;
- deliberately left `plugin-local` with a named rollout blocker and owner; or
- reclassified through a separately approved decision.

Run final backend/frontend/static/repository gates, update
`docs/PLUGIN_COMPATIBILITY.md`, `docs/RECOVERY.md`, `CHANGELOG.md`, and
`plans/007-coverage-ledger.md`, then commit and push the evidence-only closure.

This plan never authorizes a production restore, production restore drill,
unreviewed secret creation, downtime, broad Docker access, compatibility
fallback, or expansion into a blocked/planned service.
