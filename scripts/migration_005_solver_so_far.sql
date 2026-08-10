-- Migration 005: Add clues_by_solver_so_far column and backfill it.
-- Mirrors the existing clues_by_author_so_far: stores the "Nth clue solved by
-- this solver" pill so we don't have to compute it on every page view.

ALTER TABLE clues ADD COLUMN IF NOT EXISTS clues_by_solver_so_far INTEGER;

-- Backfill: for each clue, count how many clues with legacy_number <= this one
-- were solved by the same person.
UPDATE clues c
SET clues_by_solver_so_far = sub.cnt
FROM (
    SELECT
        c1.id,
        COUNT(*) AS cnt
    FROM clues c1
    JOIN clues c2 ON c2.solver = c1.solver
                   AND c2.legacy_number <= c1.legacy_number
    WHERE c1.solver IS NOT NULL
    GROUP BY c1.id
) sub
WHERE c.id = sub.id;
