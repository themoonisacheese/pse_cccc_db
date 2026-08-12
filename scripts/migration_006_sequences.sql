-- Migration 006: sequences (themes + author sequences)
--
-- Sequences are many-to-many linked to clues. The old spreadsheet tracked
-- loose shared themes and setter-revealed author sequences as two separate
-- concepts, but here they are unified into one "sequences" table
-- distinguished only by seq_type (author = setter-revealed run, theme = loose
-- theme). NOTE: this project's migration runner splits statements on the
-- semicolon character and does not understand comments, so this file's
-- comments must not contain a semicolon anywhere.

CREATE TABLE IF NOT EXISTS sequences (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(512),                    -- optional (legacy/unnamed groups are NULL)
    seq_type    VARCHAR(16) NOT NULL DEFAULT 'theme',
    author      VARCHAR(255),                    -- informative, for author sequences
    color       VARCHAR(32),                     -- optional override, UI defaults by type
    description TEXT,                            -- optional note about the sequence
    legacy_key  INTEGER,                         -- representative clue# from the old sheet
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sequences_type_legacy UNIQUE (seq_type, legacy_key),
    CONSTRAINT ck_sequences_type CHECK (seq_type IN ('author', 'theme'))
);

CREATE INDEX IF NOT EXISTS ix_sequences_legacy_key ON sequences (legacy_key);
CREATE INDEX IF NOT EXISTS ix_sequences_type ON sequences (seq_type);

-- Many-to-many link between clues and sequences
CREATE TABLE IF NOT EXISTS clue_sequences (
    clue_id     INTEGER NOT NULL REFERENCES clues (id) ON DELETE CASCADE,
    sequence_id INTEGER NOT NULL REFERENCES sequences (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clue_id, sequence_id)
);

CREATE INDEX IF NOT EXISTS ix_clue_sequences_sequence_id ON clue_sequences (sequence_id);
CREATE INDEX IF NOT EXISTS ix_clue_sequences_clue_id ON clue_sequences (clue_id);
