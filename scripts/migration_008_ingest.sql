-- Migration 008: ingest support
--
-- Adds the columns needed for the chat ingest daemon to track where a clue
-- came from (SE chat message ID) and whether an in-DB actor is a bot rather
-- than a logged-in human, plus the ingest_state watermark table used for
-- recovery after a disconnect.
--
--   * clues.message_id        — SE chat message ID this clue was ingested from.
--                               UNIQUE (partial, NON-NULL rows only) so new
--                               entries are deduplicated on re-poll / restart,
--                               while legacy clues (added manually, no message
--                               ID known) simply leave it NULL and never collide.
--   * clues.source            — how the clue entered the DB: 'ingest' (bot
--                               detected it in chat) vs 'manual' (editor form).
--   * users.is_bot            — distinguishes the ingest service user from a
--                               real human, so bot writes in the audit trail
--                               aren't mistaken for a person.
--   * ingest_state            — a single-row table persisting the last-seen
--                               SE chat message ID (the watermark).  The daemon
--                               reads it on startup and uses it for a lightweight
--                               catch-up after a disconnect.  Survives container
--                               rebuilds (unlike a local file).
--
-- NOTE: the project's migration runner splits statements on the semicolon
-- character and does not understand comments, so no comment here may contain
-- a semicolon.

-- ── clues additions ────────────────────────────────────────

ALTER TABLE clues ADD COLUMN IF NOT EXISTS message_id     INTEGER;
ALTER TABLE clues ADD COLUMN IF NOT EXISTS source         VARCHAR(16) NOT NULL DEFAULT 'manual';

-- Partial unique index: dedupe rule for messages that DO have an ID, without
-- blocking multiple legacy clues that legitimately share a NULL message_id.
CREATE UNIQUE INDEX IF NOT EXISTS ux_clues_message_id ON clues (message_id) WHERE message_id IS NOT NULL;

-- ── users additions ────────────────────────────────────────

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;

-- Seed the ingest bot account.  It is stored directly (a DB row, not an OAuth
-- session), so there is no login expiry to worry about.  is_editor lets the
-- bot's writes be attributed like an editor in the UI/history, matching the
-- app's existing assumptions.
INSERT INTO users (se_user_id, display_name, profile_link, is_room_owner, is_editor, is_admin, is_bot)
VALUES (0, 'CCCC Ingest Bot', NULL, FALSE, TRUE, FALSE, TRUE)
ON CONFLICT (se_user_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    is_editor    = TRUE,
    is_bot       = TRUE;

-- ── ingest_state (watermark) ───────────────────────────────

CREATE TABLE IF NOT EXISTS ingest_state (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    watermark   INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the single watermark row.
INSERT INTO ingest_state (id, watermark)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

-- Backfill source for any rows we just transformed: rows that already have a
-- message_id set are treated as ingest-sourced.  (Idempotent, safe to re-run.)
UPDATE clues SET source = 'ingest' WHERE message_id IS NOT NULL;
