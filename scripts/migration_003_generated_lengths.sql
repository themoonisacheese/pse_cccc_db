-- Migration 003: Make answer_length and clue_length generated columns

-- These are now computed automatically from solution and clue_text.
-- answer_length = number of letters in solution (ignoring spaces and hyphens)
-- clue_length = character count of clue_text

-- Drop the old manually-set columns and recreate as GENERATED ALWAYS AS ... STORED.
-- Existing data is recomputed from the source columns.

-- answer_length: count letters only, ignoring spaces and hyphens
ALTER TABLE clues DROP COLUMN IF EXISTS answer_length;
ALTER TABLE clues ADD COLUMN answer_length INTEGER GENERATED ALWAYS AS (
    LENGTH(REPLACE(REPLACE(solution, ' ', ''), '-', ''))
) STORED;

-- clue_length: simple character count of the clue text
ALTER TABLE clues DROP COLUMN IF EXISTS clue_length;
ALTER TABLE clues ADD COLUMN clue_length INTEGER GENERATED ALWAYS AS (
    LENGTH(clue_text)
) STORED;
