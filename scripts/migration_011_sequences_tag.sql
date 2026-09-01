-- Migration 011: allow 'tag' in the sequences.seq_type CHECK constraint
--
-- The 'tag' sequence type (loose labels like "all &lit clues") was added to
-- the app/schema/UI in commit e6b153f, but the live DB had never actually
-- gotten a seq_type CHECK constraint applied (migration_006 used
-- CREATE TABLE IF NOT EXISTS, so on a pre-existing table the constraint was
-- skipped). As a result legacy rows with seq_type='sequence' (an obsolete
-- pre-author/theme type) survived, and any attempt to ADD a strict CHECK
-- would fail on those rows. This migration:
--   1. normalizes the obsolete 'sequence' type to 'author' (they all carry an
--      author setter name, so they are setter-revealed author sequences), and
--   2. (re)creates the widened CHECK allowing 'author'/'theme'/'tag'.
-- It is idempotent and safe to re-run.

UPDATE sequences SET seq_type = 'author' WHERE seq_type = 'sequence';

ALTER TABLE sequences DROP CONSTRAINT IF EXISTS ck_sequences_type;
ALTER TABLE sequences ADD CONSTRAINT ck_sequences_type
    CHECK (seq_type IN ('author', 'theme', 'tag'));
