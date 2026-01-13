-- Migration: Add settings table and retention_policy_json to jobs
-- Date: 2025-01-13
-- Description: Adds global settings table for retention policy and per-job retention override

-- Create settings table (singleton for global config)
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    global_retention_policy_json TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Insert default settings row if not exists
INSERT OR IGNORE INTO settings (id) VALUES (1);

-- Add retention_policy_json column to jobs table
-- SQLite: ALTER TABLE ADD COLUMN is supported
ALTER TABLE jobs ADD COLUMN retention_policy_json TEXT;

-- Note: For PostgreSQL/MySQL, the syntax is the same:
-- ALTER TABLE jobs ADD COLUMN retention_policy_json TEXT;
