# Maintenance Jobs Implementation Summary

## Overview
Implemented a complete maintenance jobs system with separate `MaintenanceJob` and `MaintenanceRun` models, unified scheduling abstraction, and a new UI page to view maintenance run history.

## Implementation Details

### Backend Changes

#### 1. Models & Database
- **New Models**: `MaintenanceJob` and `MaintenanceRun` in `backend/app/models/maintenance.py`
  - `MaintenanceJob`: Defines scheduled maintenance tasks with `key` (unique), `job_type`, `schedule_cron`, `enabled`, `visible_in_ui`
  - `MaintenanceRun`: Tracks execution history with `status`, `message`, `result_json`
- **Migration**: `backend/migrations/004_add_maintenance_jobs.sql`
  - Creates both tables with proper indexes
  - Inserts default jobs: `retention_cleanup_nightly` (visible) and `retention_cleanup_manual` (hidden)
- **Enum**: Added `MaintenanceJobType` to `backend/app/domain/enums.py`

#### 2. Unified Scheduling
- **ScheduledItem DTO**: Domain-level abstraction for scheduling (no DB inheritance)
- **Adapters**: `ScheduledItem.from_backup_job()` and `ScheduledItem.from_maintenance_job()`
- **Dispatcher**: `scheduled_dispatch(kind, job_id)` routes to appropriate executor
- **Namespaced IDs**: APScheduler job IDs use `backup:{id}` and `maintenance:{id}`
- **Updated**: `schedule_jobs_on_startup()` loads both backup and maintenance jobs

#### 3. Maintenance Execution
- **Function**: `execute_maintenance_job(maintenance_job_id)` in `backend/app/core/scheduler.py`
  - Creates `MaintenanceRun` in running state
  - Executes based on `job_type` (currently supports `retention_cleanup`)
  - Updates run with success/failure and structured `result_json`

#### 4. Services
- **MaintenanceService**: `backend/app/services/maintenance.py`
  - `list_jobs(visible_in_ui=None)`
  - `get_job(job_id)` and `get_job_by_key(key)`
  - `list_runs(limit=None)` - sorted by most recent first
  - `get_run(run_id)`

#### 5. API Endpoints
- **Maintenance Router**: `backend/app/api/maintenance.py`
  - `GET /api/v1/maintenance/jobs?visible_in_ui=true` (default true)
  - `GET /api/v1/maintenance/jobs/{id}`
  - `GET /api/v1/maintenance/runs?limit=N`
  - `GET /api/v1/maintenance/runs/{id}`
- **Updated Settings API**: `POST /api/v1/settings/retention/run`
  - When called without `job_id`/`target_id`: uses hidden `retention_cleanup_manual` job and creates `MaintenanceRun`
  - Legacy behavior preserved for specific `job_id`/`target_id` pairs

#### 6. Schemas
- **Pydantic Schemas**: `backend/app/schemas/maintenance.py`
  - `MaintenanceJob`, `MaintenanceJobCreate`, `MaintenanceJobUpdate`
  - `MaintenanceRun` with `MaintenanceRunResult` (parsed from `result_json`)
  - `MaintenanceRun.from_orm_with_result()` helper for parsing

### Frontend Changes

#### 1. API Client
- **Types**: Added `MaintenanceJob`, `MaintenanceRun`, `MaintenanceRunResult` to `frontend/src/api/client.ts`
- **Methods**: 
  - `listMaintenanceJobs(visibleInUi?)`
  - `getMaintenanceJob(id)`
  - `listMaintenanceRuns(limit?)`
  - `getMaintenanceRun(id)`

#### 2. UI Page
- **New Page**: `frontend/src/pages/MaintenanceJobs.tsx`
  - Table showing: Job name/type, started, finished, duration, status badge, summary stats
  - Status badges: Success (green), Failed (red), Running (yellow)
  - Summary stats: pairs processed, deleted count, kept count
  - Empty state and error handling

#### 3. Navigation
- **Route**: Added `/maintenance-jobs` route in `frontend/src/routes.tsx`
- **Sidebar**: Added "Maintenance Jobs" nav item under Settings with Wrench icon

## Tests Added

### Model Tests
- `backend/tests/test_models/test_maintenance_model.py`
  - Test maintenance job creation
  - Test key uniqueness constraint
  - Test maintenance run creation
  - Test job/runs relationship
  - Test cascade delete

### Service Tests
- `backend/tests/test_services/test_maintenance_service.py`
  - Test listing jobs (all and filtered by visible_in_ui)
  - Test getting job by ID and by key
  - Test listing runs (with and without limit)
  - Test getting run by ID

### API Tests
- `backend/tests/test_api/test_maintenance_api.py`
  - Test listing maintenance jobs (default visible filter)
  - Test getting specific maintenance job
  - Test listing maintenance runs with parsed results
  - Test getting specific maintenance run
  - Test 404 handling

### Scheduler Tests
- `backend/tests/test_core/test_scheduler_maintenance.py`
  - Test ScheduledItem adapters (backup and maintenance)
  - Test schedule_jobs_on_startup loads both types
  - Test scheduled_dispatch routing
  - Test execute_maintenance_job creates runs
  - Test execute_maintenance_job handles failures

### Updated Tests
- `backend/tests/test_api/test_settings_api.py`
  - Added `TestRetentionRun` class
  - Test manual retention creates MaintenanceRun
  - Test legacy behavior for specific job/target pairs

## Documentation Updates

- **README.md**: Added maintenance jobs to features list
- **backend/README.md**: Updated API endpoints and features
- **AGENTS.md**: Added note about maintenance jobs and deterministic keys

## Key Design Decisions

1. **Separate Models**: `MaintenanceJob`/`MaintenanceRun` are separate from `Job`/`Run` to maintain clear separation of concerns
2. **Deterministic Keys**: Jobs identified by `key` (not numeric IDs) for stability across environments
3. **Hidden System Jobs**: Manual retention uses hidden job (`visible_in_ui=false`) created by migration
4. **Unified Scheduling**: Backend-only abstraction (no DB inheritance) allows one scheduling loop for both job types
5. **Structured Results**: `result_json` stores execution stats as JSON, parsed by schemas for API responses

## Testing Checklist

### Backend Tests
- [ ] Run all tests: `cd backend && python3 -m pytest -v`
- [ ] Verify maintenance model tests pass
- [ ] Verify maintenance service tests pass
- [ ] Verify maintenance API tests pass
- [ ] Verify scheduler maintenance tests pass
- [ ] Verify updated settings API tests pass
- [ ] Verify no regressions in existing tests

### Frontend Tests
- [ ] Verify MaintenanceJobs page compiles
- [ ] Verify API client types are correct
- [ ] Verify routing works
- [ ] Verify sidebar navigation works

### Integration Testing
- [ ] Test migration creates tables and default jobs
- [ ] Test scheduler loads maintenance jobs on startup
- [ ] Test scheduled maintenance job execution creates runs
- [ ] Test manual retention cleanup creates MaintenanceRun
- [ ] Test API endpoints return correct data
- [ ] Test UI displays maintenance runs correctly

### UI Verification
- [ ] Navigate to Settings → Maintenance Jobs
- [ ] Verify page loads without errors
- [ ] Verify empty state displays when no runs exist
- [ ] Trigger manual retention cleanup from Options page
- [ ] Verify maintenance run appears in Maintenance Jobs page
- [ ] Verify status badges display correctly
- [ ] Verify summary stats display correctly
- [ ] Verify job name and type display correctly

## Migration Notes

The migration `004_add_maintenance_jobs.sql`:
- Creates `maintenance_jobs` and `maintenance_runs` tables
- Inserts default jobs using `INSERT OR IGNORE` (idempotent)
- Should run automatically on next app startup via `init_db()`

## Future Enhancements

- Add more maintenance job types (beyond retention cleanup)
- Add UI for managing maintenance job schedules
- Add filtering/searching in Maintenance Jobs page
- Add detailed run view with full logs
- Add scheduled maintenance job management
