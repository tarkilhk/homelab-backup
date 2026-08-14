# Backup protection facts

Homelab Backup reports concrete facts for every configured target. It does not
collapse them into a generic health score. The Dashboard and Prometheus endpoint
both use `GET /api/v1/protection/targets` as their source of truth.

## What counts as a validated backup

A successful target attempt counts as the latest validated backup only when the
execution ledger contains all of the evidence produced by artifact validation:

- status is `success`;
- the artifact path is present;
- artifact size is greater than zero; and
- a complete SHA-256 digest is present.

This evidence is recorded only after the scheduler validates the artifact and
its required sidecar. A success-shaped row without that evidence does not hide a
protection gap.

Retries remain visible as individual target attempts. Latest outcome and
consecutive-failure calculations use the final attempt for each parent run, so a
failed first attempt followed by a successful retry is one successful run rather
than one current failure.

## Gap reasons

Each target has either no gap or exactly one of these reasons:

- `not_scheduled`: no enabled job currently covers the target through its tag.
- `never_succeeded`: the target is covered, but has no validated successful backup.
- `scheduled_backup_missing`: at least one covered schedule was due after the
  latest validated backup and no qualifying backup has completed. An in-progress
  covering run suppresses this reason until all of its retries and targets finish.

There is no configurable freshness threshold. Freshness is derived from the
target's actual enabled cron schedules.

## Durable dispatch outcomes

Manual and scheduled jobs that resolve no targets finish as failed runs.
Scheduled overlap prevention creates a terminal `skipped` run instead of only a
log line. Per-target retries create one TargetRun row per attempt. This keeps the
Runs UI and database ledger truthful without adding a second event subsystem.

## Prometheus metrics

`GET /metrics` exports these target-level metrics with `target_id`, `target_name`,
and `target_slug` labels:

- `homelab_backup_target_covering_jobs`
- `homelab_backup_target_latest_attempt_info{status=...}`
- `homelab_backup_target_last_attempt_timestamp_seconds`
- `homelab_backup_target_last_success_timestamp_seconds`
- `homelab_backup_target_artifact_age_seconds`
- `homelab_backup_target_next_run_timestamp_seconds`
- `homelab_backup_target_consecutive_failures`
- `homelab_backup_target_gap_info{reason=...}`

Timestamp and age series are omitted when the corresponding fact does not exist.
Gap series are emitted only for targets with a current gap. Example alert inputs:

```promql
homelab_backup_target_gap_info == 1
```

```promql
homelab_backup_target_consecutive_failures > 0
```

The former exposes the exact reason as a label, allowing Alertmanager rules to
route or describe `not_scheduled`, `never_succeeded`, and
`scheduled_backup_missing` differently.
