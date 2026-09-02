-- CCCC DB — Full-text search: stopword-free text search configuration
--
-- Problem: the 'english' text search configuration drops common words
-- ("out", "in", "up", "not", ...) as stopwords, both when indexing and when
-- parsing queries. Searching for e.g. "out" in the web UI / API matched
-- nothing, even though many clues contain it.
--
-- Fix: create a custom 'cccc_english' configuration that keeps the English
-- Snowball stemmer but uses no stopword list, switch the FTS trigger to it,
-- and rebuild all existing search_vector values with it.
--
-- Idempotent: safe to re-run at every app startup (CREATEs are guarded,
-- and the vector rebuild only runs when some row would actually change).
-- Assumes migration_001_fts.sql has been applied (clues.search_vector exists).

-- 1. Stemmer dictionary with no stopwords (no stopword file loaded).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictname = 'english_nostop') THEN
        CREATE TEXT SEARCH DICTIONARY english_nostop (
            TEMPLATE = snowball,
            Language = english,
            StopWords = ''
        );
    END IF;
END $$;

-- 2. Custom configuration: copy of 'english' with the stopwords removed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'cccc_english') THEN
        CREATE TEXT SEARCH CONFIGURATION cccc_english (COPY = english);
    END IF;
END $$;

-- Remap any token types still using the stopword-bearing english_stem
-- dictionary (no-op once every token type is on english_nostop).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_ts_config_map m
        JOIN pg_ts_config c ON c.oid = m.mapcfg
        WHERE c.cfgname = 'cccc_english'
          AND m.mapdict = (SELECT oid FROM pg_ts_dict WHERE dictname = 'english_stem')
    ) THEN
        ALTER TEXT SEARCH CONFIGURATION cccc_english
            ALTER MAPPING REPLACE english_stem WITH english_nostop;
    END IF;
END $$;

-- 3. Point the trigger function at the new configuration.
CREATE OR REPLACE FUNCTION clues_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('cccc_english', coalesce(NEW.clue_text, '')), 'A') ||
        setweight(to_tsvector('cccc_english', coalesce(NEW.solution, '')), 'A') ||
        setweight(to_tsvector('cccc_english', coalesce(NEW.explanation, '')), 'B') ||
        setweight(to_tsvector('cccc_english', coalesce(NEW.author, '')), 'C') ||
        setweight(to_tsvector('cccc_english', coalesce(NEW.solver, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS clues_search_vector_trigger ON clues;
CREATE TRIGGER clues_search_vector_trigger
BEFORE INSERT OR UPDATE ON clues
FOR EACH ROW EXECUTE FUNCTION clues_search_vector_update();

-- 4. Rebuild existing vectors with the new configuration — only if some
-- row's stored vector differs from what cccc_english would produce
-- (i.e. skip entirely on re-runs / fresh imports).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM clues
        WHERE search_vector IS DISTINCT FROM (
            setweight(to_tsvector('cccc_english', coalesce(clue_text, '')), 'A') ||
            setweight(to_tsvector('cccc_english', coalesce(solution, '')), 'A') ||
            setweight(to_tsvector('cccc_english', coalesce(explanation, '')), 'B') ||
            setweight(to_tsvector('cccc_english', coalesce(author, '')), 'C') ||
            setweight(to_tsvector('cccc_english', coalesce(solver, '')), 'C')
        )
    ) THEN
        UPDATE clues
        SET search_vector =
            setweight(to_tsvector('cccc_english', coalesce(clue_text, '')), 'A') ||
            setweight(to_tsvector('cccc_english', coalesce(solution, '')), 'A') ||
            setweight(to_tsvector('cccc_english', coalesce(explanation, '')), 'B') ||
            setweight(to_tsvector('cccc_english', coalesce(author, '')), 'C') ||
            setweight(to_tsvector('cccc_english', coalesce(solver, '')), 'C');
    END IF;
END $$;
