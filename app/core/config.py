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
    # App-level API key for anonymous/unauthed requests.
    # Authenticated users' tokens take priority over this key.
    se_key: str = ""
    # The room ID for the CCCC chatroom on chat.stackexchange.com
    se_chat_room_id: int = 14524  # The Sphinx's Lair — CCCC's home room
    # OAuth redirect URI — must match what's registered on StackApps
    se_oauth_redirect: str = "http://localhost:8000/api/auth/callback"
    # SE site to use for user lookups and moderator checks
    se_site: str = "puzzling.stackexchange.com"

    # ── SE Chat Bot Account ────────────────────────────────────
    # Email+password for a bot account used to authenticate with
    # chat.stackexchange.com (bypasses Cloudflare).  This account
    # is used to fetch room owner lists and message content.
    # Leave empty to disable chat features (room owner check falls
    # back to ROOM_OWNER_IDS allowlist).
    se_bot_email: str = ""
    se_bot_password: str = ""

    # ── Authorization ───────────────────────────────────────────
    # Comma-separated list of SE user IDs (as strings) who are
    # authorised as room owners (moderators).  These users can
    # create/edit/delete clues.  Leave empty to rely on automatic
    # room-owner detection via the chat API (requires SE_BOT_*).
    room_owner_ids: str = ""

    # ── Pagination ──────────────────────────────────────────────
    default_page_size: int = 50
    max_page_size: int = 500

    # ── Solution Ingest (Stage 2) ───────────────────────────────
    # LLM provider used for extraction (salt / wordplay-only answers).
    # The model is an *extractor*, never asked to solve a clue cold.
    llm_base_url: str = ""           # OpenAI-compatible base URL
    llm_api_key: str = ""            # API key for the provider
    llm_model: str = ""              # e.g. "deepseek-ai/DeepSeek-V3-0731" or DS4-flash
    llm_max_retries: int = 5
    llm_backoff_base: float = 2.0    # exponential backoff multiplier

    # Auto-accept confidence threshold.  Default 0.0 => nothing auto-accepted;
    # every candidate lands in the review queue until we calibrate the
    # threshold from recorded approve/reject scores.
    solution_auto_accept_threshold: float = 0.0

    # Comma-separated SE user IDs whose messages are pure noise (e.g. RSS
    # feed bots) and are dropped from windows before detection.
    solution_noise_user_ids: str = ""

    @property
    def owner_id_list(self) -> List[int]:
        if not self.room_owner_ids.strip():
            return []
        return [
            int(x.strip()) for x in self.room_owner_ids.split(",") if x.strip()
        ]

    @property
    def noise_user_id_list(self) -> List[int]:
        if not self.solution_noise_user_ids.strip():
            return []
        return [
            int(x.strip())
            for x in self.solution_noise_user_ids.split(",")
            if x.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
