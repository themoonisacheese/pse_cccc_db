-- Migration 011: allow 'tag' in the sequences.seq_type CHECK constraint
--
-- The 'tag' sequence type (loose labels like "all &lit clues") was added to
-- the app/schema/UI in commit e6b153f, but the DB CHECK constraint
-- ck_sequences_type still only allowed ('author', 'theme'). As a result, any
-- sequence edit that submitted seq_type='tag' (the edit form always sends the
-- full field set) failed on commit with a CheckViolationError, surfacing in
-- the UI as a generic "Save failed". This migration widens the constraint so
-- 'tag' sequences can be saved. It is idempotent and safe to re-run.

ALTER TABLE sequences DROP CONSTRAINT IF EXISTS ck_sequences_type;
ALTER TABLE sequences ADD CONSTRAINT ck_sequences_type
    CHECK (seq_type IN ('author', 'theme', 'tag'));
