-- Migration: Make runs.job_id nullable for restore operations
-- Date: 2025-01-XX
-- Description: Makes job_id nullable in runs table to support restore operations that aren't tied to jobs

-- Make job_id nullable in runs table
-- SQLite doesn't support ALTER COLUMN directly, so we need to recreate the table
-- However, for SQLite, we'll use a simpler approach that works with the ORM
-- The actual change will be in the model definition, but we document it here

-- Note: SQLite has limited ALTER TABLE support. If using SQLite:
-- 1. The model change (nullable=True) will be applied on next schema creation
-- 2. For existing databases, you may need to recreate the table or use a migration tool
-- 3. For PostgreSQL/MySQL, you can use: ALTER TABLE runs ALTER COLUMN job_id DROP NOT NULL;

-- For PostgreSQL:
-- ALTER TABLE runs ALTER COLUMN job_id DROP NOT NULL;

-- For MySQL:
-- ALTER TABLE runs MODIFY COLUMN job_id INTEGER NULL;

-- For SQLite (requires table recreation or migration tool):
-- This migration documents the intent. The actual change happens via model update.
-- Existing backup runs will keep their job_id values (no data changes needed).
-- Restore runs will have NULL job_id.

