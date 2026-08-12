-- Migration 007: Drop the stale answer_count column
--
-- answer_count was a legacy snapshot computed once during the spreadsheet CSV
-- import. It was never updated when new clues with reused solutions were added,
-- so it always drifted out of date. Every consumer (stats page, clue detail,
-- API) now derives solution reuse with a live COUNT/GROUP BY over the clues
-- table. This stale denormalized count is dead weight, so we drop it.
--
-- NOTE: the migration runner splits on the semicolon character and does not
-- understand comments, so no comment in this file may contain a semicolon.

ALTER TABLE clues DROP COLUMN IF EXISTS answer_count;
