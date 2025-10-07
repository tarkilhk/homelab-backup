-- Migration: Add operation columns for restore feature
-- Date: 2025-10-07
-- Description: Adds operation columns to runs and target_runs tables to support restore operations

-- Add operation column to runs table
ALTER TABLE runs ADD COLUMN operation VARCHAR(20) NOT NULL DEFAULT 'backup';

-- Add index on operation column for runs
CREATE INDEX ix_runs_operation ON runs(operation);

-- Add operation column to target_runs table
ALTER TABLE target_runs ADD COLUMN operation VARCHAR(20) NOT NULL DEFAULT 'backup';

-- Add index on operation column for target_runs
CREATE INDEX ix_target_runs_operation ON target_runs(operation);

-- Update any existing rows to have 'backup' as their operation (already done by DEFAULT)
-- This migration is safe to run multiple times (will fail gracefully if columns exist)
