-- Migration 009: solution ingest (Stage 2)
--
-- Adds the tables backing the "Solution Ingest" feature: candidate solutions
-- for editors to review, and a retryable queue for LLM-required work.
--
--   * clue_candidates — one row per proposed solution for a clue.  This is
--     BOTH the editor review queue AND the calibration source (approved /
--     highest-unapproved confidence scores are read straight off these rows
--     at runtime; there is deliberately no separate calibration table).
--       - confidence : the pipeline's confidence score for this candidate.
--       - signals    : JSONB of the deterministic signal badges that produced
--                      the score, e.g. {"hash_verified":true,"enum_match":true}.
--       - status     : 'pending' (in review) | 'approved' | 'rejected'.
--       - source_message_id : the SE chat message the candidate came from.
--   * pending_llm — a DB-backed retry queue for LLM-required work (salt
--     extraction, wordplay-only extraction).  Survives container restarts so
--     an LLM outage never loses work; the worker drains it with backoff.
--
-- NOTE: the project's migration runner splits statements on the semicolon
-- character and does not understand comments, so no comment here may contain
-- a semicolon.

-- ── clue_candidates ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS clue_candidates (
    id                 SERIAL PRIMARY KEY,
    clue_id            INTEGER NOT NULL REFERENCES clues(id) ON DELETE CASCADE,
    solution           TEXT NOT NULL,
    solver             VARCHAR(255),
    explanation        TEXT,
    confidence         DOUBLE PRECISION NOT NULL DEFAULT 0,
    signals            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status             VARCHAR(16) NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected')),
    source_message_id  INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_clue_candidates_clue_id ON clue_candidates (clue_id);
CREATE INDEX IF NOT EXISTS ix_clue_candidates_status  ON clue_candidates (status);

-- ── pending_llm ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pending_llm (
    id          SERIAL PRIMARY KEY,
    clue_id     INTEGER REFERENCES clues(id) ON DELETE CASCADE,
    task_type   VARCHAR(32) NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts    INTEGER NOT NULL DEFAULT 0,
    status      VARCHAR(16) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'done', 'failed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pending_llm_status ON pending_llm (status);
