-- Migration 010: rename the candidate signal key 'llm_extract' -> 'classifier'
--
-- The review UI shows signal badges from the JSONB `signals` column on
-- clue_candidates. The key that meant "LLM reconstruction, wordplay-only"
-- was stored as 'llm_extract' and we now store it as 'classifier' so the raw
-- key matches what the UI displays (no display-label remap needed).
--
-- NOTE: the project's migration runner splits statements on the semicolon
-- character and does not understand comments, so no comment here may contain
-- a semicolon.

UPDATE clue_candidates
SET signals = (signals - 'llm_extract') || jsonb_build_object('classifier', signals -> 'llm_extract')
WHERE signals ? 'llm_extract';
