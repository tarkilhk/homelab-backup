-- Snapshot the non-secret source database identity used by each backup run.
-- Restore preflight uses this immutable run record instead of mutable target config.
ALTER TABLE target_runs ADD COLUMN source_identity_json TEXT;
