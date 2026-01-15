-- Migration: Add maintenance_jobs and maintenance_runs tables
-- Date: 2025-01-20
-- Description: Adds maintenance job scheduling and execution history tracking

-- Create maintenance_jobs table (definitions)
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key VARCHAR(100) NOT NULL UNIQUE,
    job_type VARCHAR(50) NOT NULL,
    name VARCHAR(255) NOT NULL,
    schedule_cron VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    config_json TEXT,
    visible_in_ui BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for maintenance_jobs
CREATE INDEX IF NOT EXISTS ix_maintenance_jobs_key ON maintenance_jobs(key);
CREATE INDEX IF NOT EXISTS ix_maintenance_jobs_job_type ON maintenance_jobs(job_type);
CREATE INDEX IF NOT EXISTS ix_maintenance_jobs_enabled ON maintenance_jobs(enabled);
CREATE INDEX IF NOT EXISTS ix_maintenance_jobs_visible_in_ui ON maintenance_jobs(visible_in_ui);

-- Create maintenance_runs table (executions)
CREATE TABLE IF NOT EXISTS maintenance_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_job_id INTEGER NOT NULL,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    status VARCHAR(20) NOT NULL,
    message TEXT,
    result_json TEXT,
    FOREIGN KEY (maintenance_job_id) REFERENCES maintenance_jobs(id) ON DELETE CASCADE
);

-- Create indexes for maintenance_runs
CREATE INDEX IF NOT EXISTS ix_maintenance_runs_job_id ON maintenance_runs(maintenance_job_id);
CREATE INDEX IF NOT EXISTS ix_maintenance_runs_status ON maintenance_runs(status);
CREATE INDEX IF NOT EXISTS ix_maintenance_runs_started_at ON maintenance_runs(started_at);

-- Insert default retention cleanup jobs (idempotent)
-- Nightly scheduled job (visible in UI)
INSERT OR IGNORE INTO maintenance_jobs (key, job_type, name, schedule_cron, enabled, visible_in_ui, created_at, updated_at)
VALUES ('retention_cleanup_nightly', 'retention_cleanup', 'Nightly Retention Cleanup', '0 3 * * *', 1, 1, datetime('now'), datetime('now'));

-- Manual hidden/system job (not scheduled, not shown in UI)
INSERT OR IGNORE INTO maintenance_jobs (key, job_type, name, schedule_cron, enabled, visible_in_ui, created_at, updated_at)
VALUES ('retention_cleanup_manual', 'retention_cleanup', 'Manual Retention Cleanup', '0 0 1 1 *', 0, 0, datetime('now'), datetime('now'));
