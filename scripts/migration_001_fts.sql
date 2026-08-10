-- CCCC DB — Initial schema migration
-- Run this after SQLAlchemy create_all() to add the FTS trigger.
-- The trigger keeps the search_vector column in sync with clue_text,
-- solution, explanation, author, and solver.

-- Full-text search vector: combines clue text, solution, explanation, author, solver
-- Uses 'english' configuration since the content is in English.
CREATE OR REPLACE FUNCTION clues_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.clue_text, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.solution, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.explanation, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.author, '')), 'C') ||
        setweight(to_tsvector('english', coalesce(NEW.solver, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clues_search_vector_trigger
BEFORE INSERT OR UPDATE ON clues
FOR EACH ROW EXECUTE FUNCTION clues_search_vector_update();
