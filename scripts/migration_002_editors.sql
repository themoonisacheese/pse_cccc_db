-- Migration 002: Add is_editor column and seed initial editor

-- Add is_editor column (defaults to false)
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_editor BOOLEAN DEFAULT FALSE;

-- Seed the initial editor: Themoonisacheese (SE user ID 93099)
-- Insert if not exists, update if exists
INSERT INTO users (se_user_id, display_name, profile_link, is_room_owner, is_editor, is_admin, is_bot)
VALUES (93099, 'Themoonisacheese', 'https://puzzling.stackexchange.com/users/93099/themoonisacheese', FALSE, TRUE, FALSE, FALSE)
ON CONFLICT (se_user_id) DO UPDATE
SET is_editor = TRUE,
    display_name = EXCLUDED.display_name,
    profile_link = EXCLUDED.profile_link;
