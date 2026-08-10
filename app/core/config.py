"""Application configuration loaded from environment variables."""

import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://cccc:cccc@localhost:5432/cccc"
    # Sync URL for scripts / migrations (uses psycopg2)
    database_url_sync: str = "postgresql+psycopg2://cccc:cccc@localhost:5432/cccc"

    # ── App ─────────────────────────────────────────────────────
    app_name: str = "CCCC DB"
    debug: bool = True
    secret_key: str = "dev-insecure-secret-change-me"

    # ── Stack Exchange OAuth2 ───────────────────────────────────
    # Register at https://stackapps.com/apps/oauth/register
    se_client_id: str = ""
    se_client_secret: str = ""
    se_key: str = ""  # optional API key for higher rate limits
    # The room ID for the CCCC chatroom on chat.stackexchange.com
    se_chat_room_id: int = 14524  # The Sphinx's Lair — CCCC's home room
    # The site domain to check room-ownership on
    se_chat_host: str = "chat.stackexchange.com"
    # OAuth redirect URI — must match what's registered on StackApps
    se_oauth_redirect: str = "http://localhost:8000/auth/callback"

    # ── Authorization ───────────────────────────────────────────
    # Comma-separated list of SE user IDs (as strings) who are
    # authorised as room owners (moderators).  These users can
    # create/edit/delete clues.  Leave empty to allow all
    # authenticated users (useful for initial testing).
    room_owner_ids: str = ""

    # ── Pagination ──────────────────────────────────────────────
    default_page_size: int = 50
    max_page_size: int = 500

    @property
    def owner_id_list(self) -> List[int]:
        if not self.room_owner_ids.strip():
            return []
        return [
            int(x.strip()) for x in self.room_owner_ids.split(",") if x.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
