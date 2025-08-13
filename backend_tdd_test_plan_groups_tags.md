# Backend TDD Test Plan — Groups & Tags (Final)

> Goal: Ship robust, small‑footprint backend changes via TDD. Prioritize correctness, portability, and simplicity. No over‑engineering.

## Scope & Priorities
**Scope:** DB schema, models, services, scheduler behavior, and REST APIs for Groups, Tags, Targets, and Jobs‑by‑Tag.  
**Out of scope:** Frontend/UI.  
**Priorities (use to order implementation):**
- **P0 (must pass to merge):** Core invariants, propagation, API happy paths, cron validation, scheduler run/dedupe, deletion guards.
- **P1 (nice to have):** Concurrency limits, pagination, search filters, structured logs/metrics.
- **P2 (later):** Metrics counters, basic fuzzing, large fan‑out smoke tests.

---

## Test Harness & Fixtures
- Fresh DB per test (transaction rollbacks or temp file). Use UTC.
- Factories: `make_group(name)`, `make_tag(name)`, `make_target(name, group=None)`, `make_job(tag, cron="* * * * *", enabled=True)`.
- Clock freezer for timestamps.
- Scheduler stub: capture “submitted target runs” and simulate outcomes (success/fail/retry).
- Randomized name helper to avoid accidental collisions.
- HTTP client fixture for API tests (e.g., FastAPI test client).

---

## 1) Models & Validation (Unit) — **P0**
### Tags
- **Normalize on create**  
  Given `name="  Prod  "`, when creating, then `slug == "prod"` and `display_name == "  Prod  "`.
- **Case‑insensitive uniqueness**  
  Given a tag “prod”, when creating “PROD”, expect IntegrityError/409.
- **Rename normalization**  
  Updating display to “Prod-DB” updates `slug == "prod-db"`.
- **Reject empty/whitespace**  
  Creating with `name="   "` ⇒ 422.

### Groups
- **Unique name** duplicate group name ⇒ 409.
- **Reject empty/whitespace name** creating or updating with `name="   "` ⇒ 422.

### TargetTags Provenance
- **GROUP requires provenance**  
  `origin='GROUP'` with `source_group_id=None` ⇒ 422.
- **Provenance forbidden for AUTO/DIRECT**  
  `origin in ('AUTO','DIRECT')` with non‑null `source_group_id` ⇒ 422.
- **Uniqueness by origin**  
  `(target, tag, origin)` unique: duplicate DIRECT insert ⇒ IntegrityError/409; DIRECT + GROUP allowed.

### Jobs
- **Enabled is boolean** (defaults True).
- **Cron validation** Bad cron strings ⇒ 422.

### Targets & Slugs
- **Unique name** duplicate target name ⇒ 409.
- **Slug generated and immutable** slug is created non-empty on creation; renaming target leaves slug unchanged.
- **Plugin config is valid JSON** bad JSON rejected ⇒ 422 (if validated).

---

## 2) Services (Unit) — **P0**
### TargetService
- **Create ⇒ auto‑tag**  
  Auto‑tag exists: `origin='AUTO'`, `is_auto_tag=True`.
- **Create in group ⇒ propagation**  
  Group has tags A,B ⇒ target ends with {AUTO, GROUP:A, GROUP:B}.
- **Rename target updates auto‑tag**  
  Auto‑tag `display_name` & `slug` updated.
- **Rename collision**  
  If new normalized name collides with an existing tag belonging to another target ⇒ 409.
- **Move between groups**  
  From G1(A) to G2(B): remove only GROUP:A; add GROUP:B; DIRECT/AUTO remain.
- **Remove from group**  
  Leaves only removes GROUP‑origin rows for that group.

### GroupService
- **Add tag to group propagates to members (idempotent)**  
  Re‑adding same tag causes no duplicates.
- **Remove tag from group de‑propagates**  
  Removes only GROUP rows; DIRECT/AUTO remain intact.

### TagService
- **Delete protected auto‑tag ⇒ 409**  
- **Delete tag used by jobs ⇒ 409**  
- **Delete unused manual tag** cascades relations (group/target links).
 - **Update protected auto‑tag ⇒ 409/422** attempts to rename an auto‑tag directly are rejected; auto‑tag names change only via target rename flow.

### JobService
- **Create requires existing tag** unknown tag_id ⇒ 404.
- **Update cron validates** bad cron ⇒ 422.

---

## 3) Scheduler/Execution (Unit) — **P0**
- **Dynamic resolution**  
  Tag T on targets {A,B} ⇒ executor receives A and B.
- **Dedupe per tick**  
  Target A matches via DIRECT and GROUP ⇒ run once.
- **No‑overlap per job**  
  Given job J still running when next tick fires ⇒ chosen policy holds (recommend: **skip and log**). Assert skipped.
- **Per‑target retry**  
  Fail once, then success; respects retry/backoff counters (simple mock time ok).

### Concurrency (P1)
- **Bounded concurrency**  
  With N targets and pool size P ⇒ no more than P parallel submissions.

---

## 4) API (Integration) — **P0**

### Groups `/api/v1/groups`
- **Create** 201 returns `{id,name,created_at,updated_at}`; duplicate name ⇒ 409.
- **Create validation** empty/whitespace `name` ⇒ 422.
- **List** 200 returns array.
- **Update** 200; unknown id ⇒ 404; renaming to an existing name ⇒ 409; whitespace `name` ⇒ 422.
- **Delete non‑empty ⇒ 409**; empty ⇒ 204; unknown id ⇒ 404.

### Group targets
- **Add targets** `POST /groups/{id}/targets` body `{target_ids}` moves targets from prior groups; 200 shows members; unknown group id ⇒ 404.
- **Remove targets** `DELETE /groups/{id}/targets` body `{target_ids}`; 200 shows remaining; unknown group id ⇒ 404.
- **Get members** `GET /groups/{id}/targets` 200; unknown group id ⇒ 404.

### Group tags
- **Add tags** `POST /groups/{id}/tags` body `{tag_names}` creates missing tags; propagates to members; 200; unknown group id ⇒ 404.
  - **Normalization & idempotency** names like `"  Prod  "` create tag with `slug="prod"` and `display_name="  Prod  "`; re-adding as `"prod"` is idempotent (no dup rows or extra propagation).
- **Remove tags** `DELETE /groups/{id}/tags` body `{tag_names}` de‑propagates; 200.
- **Get tags** `GET /groups/{id}/tags` 200; unknown group id ⇒ 404.

### Tags `/api/v1/tags`
- **List** 200 (support `q`, `limit`, `offset`) — *(P1 pagination optional)*.
- **Get** 200; unknown id ⇒ 404.
- **Delete** 204 when allowed; 409 if auto‑tag or in use by jobs; unknown id ⇒ 404.
- **Tag targets** `GET /tags/{id}/targets` 200; unknown id ⇒ 404.
  - **Response semantics (per-attachment entries)** returns an array where each element corresponds to a single attachment of this tag to a target with fields `{target, origin, source_group_id}`.
  - **Field rules** `source_group_id` is non-null only when `origin='GROUP'`; it is null for `AUTO` and `DIRECT`.
  - **Dedupe** Multiple origins for the same target appear as separate entries (no aggregation by target).

### Targets `/api/v1/targets`
- **Create** 201; auto‑tag exists; optional `group_id` propagates tags; unknown `group_id` ⇒ 404.
- **Move to group** 200; provenance set.
- **Remove from group** 200; GROUP tags removed.
- **Direct tag attach** `POST /targets/{id}/tags` body `{tag_names}` creates‑if‑missing with `origin='DIRECT'`; 200 returns tags with origins.
- **Direct tag detach** `DELETE /targets/{id}/tags` body `{tag_names}` removes only DIRECT; 200.
- **Get target tags** `GET /targets/{id}/tags` 200 returns tags with `origin` and `source_group_id` (nullable).

### Jobs `/api/v1/jobs`
- **Create** 201 with `{tag_id,name,schedule_cron,enabled}`; bad cron ⇒ 422.
- **Update** 200; invalid updates ⇒ 422.
- **Delete** 204.
- **Run now by tag** `POST /jobs/by-tag/{tag_id}/run` 200 returns per‑target run results; unknown tag ⇒ 404.
  - (P1) Minimal response shape asserted (e.g., `{target_id,status}`) and dedupe verified at endpoint level.

---

## 5) DB/Constraints (Integration) — **P0**
- **Uniqueness**
  - `targets.name` unique ⇒ 409 on duplicate.
  - `tags.slug` unique ⇒ 409 for “prod” vs “PROD”.
  - `target_tags (target_id, tag_id, origin)` unique ⇒ duplicate origin insert ⇒ 409.
- **FK rules**
  - `jobs.tag_id` is **RESTRICT**: deleting a tag used by jobs ⇒ 409.
  - Deleting a group cascades `group_tags` and any `target_tags` with `origin='GROUP'` & matching `source_group_id`.
  - **Preservation check** DIRECT and AUTO `target_tags` remain after group deletion (integration assertion).
- **Timestamps** created/updated are set by app; updated changes on mutation.

---

## 6) Edge Cases (Integration) — **P0**
- **Idempotent propagation**  
  Add same tag to group twice ⇒ no dup rows.
- **Late joiner**  
  After adding tag to group, adding new target to group ⇒ target receives tag.
- **Group move with overlapping tags**  
  Move target from G1 (A) to G2 (A) ⇒ still a single GROUP row for tag A (origin uniqueness protects).
- **Mixed origins**  
  Target has DIRECT A, group adds/removes A ⇒ DIRECT remains.
- **Rename target collision**  
  Rename to a name where normalized auto‑tag already belongs to another target ⇒ 409.
- **Large fan‑out (smoke)** *(P2)*  
  Tag with 200 targets ⇒ job resolves and submits 200 runs, deduped, within reasonable time.

---

## 7) Observability (Unit/Integration) — **P1**
- **Structured logs present**  
  - Group tag add/remove: include counts added/removed, origin, group_id.
  - Job tick: include `job_id`, resolved count, deduped count, per‑target status distribution.
- **(Optional) Metrics** basic counters increment: `job_runs_total{status}`, `tag_resolution_count`.

---

## 8) Property/Invariant Tests — **P1**
- **Propagation reversibility**  
  For any state S, add tag T to group G then remove T from G ⇒ restores GROUP rows to S.
- **Move equivalence**  
  Move target G1→G2 ≡ leave G1 then join G2 (final tag set equal).

---

## 9) Suggested Test Names (pytest)
- `test_create_target_creates_auto_tag()`
- `test_group_add_tag_propagates_to_members_idempotent()`
- `test_move_target_between_groups_updates_group_origin_tags_correctly()`
- `test_job_run_by_tag_deduplicates_targets()`
- `test_delete_tag_in_use_by_job_returns_409()`
- `test_rename_target_updates_auto_tag_and_detects_collision()`
- `test_direct_tag_attach_and_detach_affects_only_direct_origin()`
- `test_scheduler_no_overlap_skips_next_tick()`
- `test_group_name_whitespace_rejected_422()`
- `test_create_target_slug_created_and_remains_immutable_on_rename()`
- `test_create_target_with_unknown_group_id_returns_404()`
- `test_groups_unknown_id_endpoints_return_404()`
- `test_group_add_tag_normalizes_name_and_is_idempotent()`
- `test_tags_targets_returns_per_attachment_with_correct_origin_fields()`
- `test_group_delete_preserves_auto_and_direct_origin_rows()`
- `test_update_auto_tag_name_is_rejected()`

---

## Notes on Approach Improvements
- **Portability:** tests avoid DB‑specific features (no `ON UPDATE`), rely on app timestamps.
- **Simplicity:** uniqueness on `(target, tag, origin)` removes need for expression indexes.
- **Robustness:** explicit provenance validations prevent silent tag leaks or deletions.
- **DX:** add minimal direct tag attach/detach APIs so origins can be tested cleanly.
