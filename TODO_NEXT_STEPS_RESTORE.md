# ✅ COMPLETED: Disk Backup Restoration

## Overview
Functionality to restore backups has been implemented. The system scans the backup directory and shows ALL available backups, regardless of whether they have corresponding database records. This enables both normal restore operations and disaster recovery scenarios where the database is lost but backup files remain intact.

## Prerequisites
✅ **Completed**: Restore endpoint now accepts file paths instead of requiring `source_target_run_id`
✅ **Completed**: `job_id` is nullable in Run model for restore operations

## Implementation Status

### ✅ 1. Backend: Scan Backup Artifacts API Endpoint

**Status**: **COMPLETED**

**Implementation**:
- Endpoint: `GET /api/v1/backups/from-disk`
- Scans `/backups/` directory (or path from `BACKUP_BASE_PATH` env var)
- Follows backup folder convention: `/backups/<target_slug>/<YYYY-MM-DD>/...`
- For each artifact file found:
  - Returns ALL artifacts found on disk (both tracked and untracked)
  - Uses sidecar metadata when available, otherwise infers from filename/path
- Returns list of all backup artifacts with metadata:
  - `artifact_path`: Full path to the file
  - `target_slug`: Inferred from path structure
  - `date`: Inferred from path structure (YYYY-MM-DD)
  - `file_size`: Size in bytes
  - `modified_at`: File modification timestamp
  - `plugin_name`: Try to infer from filename pattern or require manual selection
  - `sha256`: Optional - compute if needed for verification

**Considerations**:
- Performance: May need pagination or async scanning for large backup directories
- Plugin inference: Filenames follow patterns like `{plugin}-backup-{timestamp}.{ext}`
- Error handling: Handle permission errors, missing directories gracefully

**Files to create/modify**:
- `backend/app/api/restores.py` or new `backend/app/api/artifacts.py`
- `backend/app/services/artifacts.py` (new service for scanning logic)
- `backend/app/schemas/artifacts.py` (new schema for orphaned artifact response)

### ✅ 2. Frontend: Restore Page

**Status**: **COMPLETED**

**Implementation**:
- Page/route: `/restore` (component: `Restore`)
- Displays list of ALL available backups with:
  - Artifact path
  - Inferred target slug and date
  - File size
  - Last modified timestamp
  - Plugin name (if inferred, or "Unknown")
- For each backup artifact:
  - Shows "Restore" button
  - On click, shows modal/dialog with:
    - Artifact details
    - Dropdown to select destination target (filtered by plugin if known)
    - If plugin unknown, shows plugin selector first, then target selector
    - "Confirm Restore" button
- After restore:
  - Shows success/error message
  - Refreshes the backup list
  - Invalidates runs query to show new restore run

**UI/UX Features**:
- Statistics dashboard showing total backups, size, unique targets/plugins
- Charts showing metadata source distribution and backups per target
- Clear indication of metadata source (sidecar vs inferred)
- Loading states while scanning directory
- Empty state when no backups found
- Full table view with sorting and filtering capabilities

**Files**:
- ✅ `frontend/src/pages/Restore.tsx` (implemented)
- ✅ `frontend/src/routes.tsx` (route added at `/restore`)
- ✅ `frontend/src/api/client.ts` (API client method: `listBackupsFromDisk`)
- ✅ Navigation/menu includes link to restore page

### ✅ 3. Testing

**Backend Tests** (✅ Implemented):
- ✅ Test scanning `/backups/` directory structure (`test_scan_backups_basic`)
- ✅ Test that ALL backups are included (tracked and untracked) (`test_scan_backups_includes_tracked`)
- ✅ Test plugin inference from filenames (`test_infer_plugin_from_filename`)
- ✅ Test error handling (permissions, missing directories)
- ✅ Test API endpoint with various directory structures (`test_list_backups_from_disk`)

**Frontend Tests** (✅ Implemented):
- ✅ Test backups list rendering with sidecar and inferred metadata
- ✅ Test restore flow
- ✅ Test plugin/target selection
- ✅ Test empty state and error states
- ✅ Test file size formatting and statistics display

## Design Decision

**Note**: The implementation shows ALL backups found on disk, not just orphaned ones. This design choice:
- Provides a unified view of all available backups for restore operations
- Simplifies the user experience - users don't need to know whether a backup is tracked or not
- Still supports disaster recovery scenarios (orphaned backups are included)
- Makes the feature more useful for general restore operations from any available backup

The original plan was to show only orphaned backups, but showing all backups is more practical and user-friendly.

## Implementation Notes

### Backup Directory Structure
The system follows this convention:
```
/backups/
  <target_slug>/
    <YYYY-MM-DD>/
      <plugin>-backup-<timestamp>.<ext>
```

Examples:
- `/backups/pihole/2025-01-15/pihole-teleporter-20250115T143022.zip`
- `/backups/postgresql/2025-01-15/postgresql-dump-20250115T143022.sql`
- `/backups/vaultwarden/2025-01-15/vaultwarden-backup-20250115T143022.tar.gz`

### Plugin Inference Strategy
1. Extract filename pattern: `{plugin}-backup-{timestamp}.{ext}` or `{plugin}-dump-{timestamp}.{ext}`
2. Match against known plugin names
3. If no match, require user to select plugin manually
4. Use plugin to filter eligible destination targets

### Implementation Details

The service scans all backup files on disk and returns them regardless of database tracking status. Metadata is obtained from:
1. Sidecar files (`.meta.json`) when available - most reliable
2. Filename patterns and path structure - inferred metadata

The API endpoint `/api/v1/backups/from-disk` returns all backups found, making it useful for both normal restore operations and disaster recovery scenarios.

## Future Enhancements (Out of Scope for Initial Implementation)
- Bulk restore multiple orphaned artifacts
- Export/import orphaned backup metadata
- Automatic re-indexing of orphaned backups into database
- Backup verification (check file integrity)
- Search/filter orphaned backups by date, plugin, size

## Related Files
- `backend/app/services/restores.py` - Restore service (already supports file-based restore)
- `backend/app/api/restores.py` - Restore API (already accepts `artifact_path`)
- `frontend/src/pages/Runs.tsx` - Runs page (reference for UI patterns)
- `ADDING_PLUGINS.md` - Documents backup folder structure conventions

