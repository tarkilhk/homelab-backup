# TODO: Disaster Recovery - Orphaned Backup Restoration

## Overview
Implement functionality to restore backup artifacts that exist on disk but have no corresponding database records (orphaned backups). This enables disaster recovery scenarios where the database is lost but backup files remain intact.

## Prerequisites
✅ **Completed**: Restore endpoint now accepts file paths instead of requiring `source_target_run_id`
✅ **Completed**: `job_id` is nullable in Run model for restore operations

## Tasks

### 1. Backend: Scan Orphaned Artifacts API Endpoint

**Goal**: Create an API endpoint that scans the backup directory and identifies artifacts that don't have corresponding database records.

**Requirements**:
- Endpoint: `GET /api/v1/restores/orphaned` or `GET /api/v1/artifacts/orphaned`
- Scan `/backups/` directory (or path from `BACKUP_BASE_PATH` env var)
- Follow backup folder convention: `/backups/<target_slug>/<YYYY-MM-DD>/...`
- For each artifact file found:
  - Check if there's a `TargetRun` record with matching `artifact_path`
  - If no match found, include it in the orphaned list
- Return list of orphaned artifacts with metadata:
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

### 2. Frontend: Orphaned Backups UI Page

**Goal**: Create a UI page that displays orphaned backups and allows users to restore them.

**Requirements**:
- New page/route: `/orphaned` or `/disaster-recovery` or add section to existing Runs page
- Display list of orphaned artifacts with:
  - Artifact path
  - Inferred target slug and date
  - File size
  - Last modified timestamp
  - Plugin name (if inferred, or "Unknown")
- For each orphaned artifact:
  - Show "Restore" button
  - On click, show modal/dialog with:
    - Artifact details
    - Dropdown to select destination target (filtered by plugin if known)
    - If plugin unknown, show plugin selector first, then target selector
    - "Confirm Restore" button
- After restore:
  - Show success/error message
  - Refresh the orphaned list (artifact should disappear if restore creates a record)
  - Optionally navigate to the new restore run

**UI/UX Considerations**:
- Clear indication that these are "orphaned" backups (no database record)
- Warning message about disaster recovery scenario
- Help text explaining what orphaned backups are
- Loading states while scanning directory
- Empty state when no orphaned backups found

**Files to create/modify**:
- `frontend/src/pages/OrphanedBackups.tsx` (new page)
- `frontend/src/routes.tsx` (add new route)
- `frontend/src/api/client.ts` (add API client method for scanning orphaned artifacts)
- Update navigation/menu to include link to orphaned backups page

### 3. Testing

**Backend Tests**:
- Test scanning `/backups/` directory structure
- Test identifying orphaned vs. tracked artifacts
- Test plugin inference from filenames
- Test error handling (permissions, missing directories)
- Test API endpoint with various directory structures

**Frontend Tests**:
- Test orphaned backups list rendering
- Test restore flow from orphaned backup
- Test plugin/target selection
- Test empty state and error states

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

### Database Query for Orphaned Detection
```python
# Pseudo-code
for artifact_path in scanned_files:
    target_run = db.query(TargetRun).filter(
        TargetRun.artifact_path == artifact_path
    ).first()
    if target_run is None:
        # This is an orphaned artifact
        orphaned_list.append(artifact_path)
```

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

