-- Migration 004: Drop unused columns answer_in_grid and standard_clue
-- These fields serve no purpose: we're not making a grid, and "standard clue" was never useful.

ALTER TABLE clues DROP COLUMN IF EXISTS answer_in_grid;
ALTER TABLE clues DROP COLUMN IF EXISTS standard_clue;
