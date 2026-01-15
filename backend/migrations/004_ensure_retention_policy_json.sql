-- Migration: Ensure retention_policy_json column exists in jobs table
-- Date: 2026-01-15
-- Description: Re-applies migration 003 to ensure retention_policy_json column is added
-- This migration is identical to 003 to ensure the column gets added if previous migration failed

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
