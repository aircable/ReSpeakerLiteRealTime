from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    device_token: str = Field(default="change-device-token", min_length=8)
    ui_token: str = Field(default="change-ui-token", min_length=8)
    database_path: Path = Path("data/companion.db")
    realtime_model: str = "gpt-realtime-2.1"
    realtime_max_output_tokens: int = Field(default=4096, ge=1, le=4096)
    planner_model: str = "gpt-5.6-terra"
    transcription_model: str = "gpt-transcribe"
    voice: str = "marin"
    reasoning_effort: str = "low"
    vad_mode: Literal["semantic_vad", "server_vad"] = "semantic_vad"
    vad_eagerness: Literal["low", "medium", "high", "auto"] = "auto"
    vad_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    vad_prefix_padding_ms: int = Field(default=300, ge=0, le=5000)
    vad_silence_duration_ms: int = Field(default=500, ge=100, le=5000)
    input_noise_reduction: Literal["far_field", "near_field", "off"] = "far_field"
    # Zero keeps the Realtime session open until explicit stop or the hard limit.
    idle_timeout_seconds: int = Field(default=0, ge=0, le=900)
    hard_session_limit_seconds: int = Field(default=3600, ge=60, le=7200)
    playback_buffer_seconds: int = Field(default=120, ge=30, le=600)
    diagnostic_audio: bool = False
    openai_trace: bool = False
    barge_in_enabled: bool = False
    # PCM16 RMS required during assistant playback; 0 disables the local gate.
    barge_in_rms_threshold: int = Field(default=8000, ge=0, le=32768)
    # XVF3610 channel-0 fixed gain; 0 restores its adaptive AGC.
    xvf_agc_ch0_gain: float = Field(default=25.0, ge=0.0, le=1000.0)
    # Minimum PCM16 frame RMS for starting a turn while no assistant audio plays.
    # Initial XVF test value; zero disables the gate if normal speech is missed.
    listening_rms_threshold: int = Field(default=800, ge=0, le=32768)
    announce_active_project: bool = True
    transcript_retention_days: int = Field(default=0, ge=0, le=3650)


@lru_cache
def get_settings() -> Settings:
    return Settings()
