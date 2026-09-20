"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    slack_signing_secret: str = ""
    # Set to True to skip signature verification (local dev / CI)
    slack_skip_signature_verification: bool = False

    # ── Telegram ───────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── OpenAI (Blast Radius Auditor) ───────────────────────────────────────
    openai_api_key: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./governance.db"

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000

    # ── MCP tool timeouts ─────────────────────────────────────────────────────
    approval_timeout_seconds: int = 45


settings = Settings()
