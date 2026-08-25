from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    device_token: str = Field(default="change-device-token", min_length=8)
    ui_token: str = Field(default="change-ui-token", min_length=8)
    database_path: Path = Path("data/companion.db")
    realtime_model: str = "gpt-realtime-2.1"
    planner_model: str = "gpt-5.6-terra"
    transcription_model: str = "gpt-transcribe"
    voice: str = "marin"
    reasoning_effort: str = "low"
    idle_timeout_seconds: int = Field(default=30, ge=5, le=900)
    hard_session_limit_seconds: int = Field(default=3600, ge=60, le=7200)
    diagnostic_audio: bool = False
    transcript_retention_days: int = Field(default=0, ge=0, le=3650)


@lru_cache
def get_settings() -> Settings:
    return Settings()
