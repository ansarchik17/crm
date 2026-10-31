from typing import Literal
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.espocrm"), extra="ignore")
    llm_mode: Literal["demo", "openai"] = "demo"
    llm_timeout_seconds: float = Field(default=15, gt=0, le=60)
    session_idle_timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    stt_enabled: bool = False
    stt_base_url: str = "https://llm.alem.ai/v1"
    stt_model: Literal["speech-to-text", "speech-to-text-kk"] = "speech-to-text"
    stt_api_key: SecretStr = SecretStr("")
    stt_language: str = Field(default="", pattern=r"^([a-z]{2,3})?$")
    stt_timeout_seconds: float = Field(default=60, gt=0, le=120)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1)
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = "none"
    openai_realtime_model: str = Field(default="gpt-realtime-2.1", min_length=1)
    openai_realtime_voice: str = "marin"
    openai_realtime_reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = "minimal"
    openai_realtime_silence_ms: int = Field(default=300, ge=100, le=1000)
    openai_realtime_timeout_seconds: float = Field(default=25, gt=0, le=60)
    espocrm_url: str = "http://127.0.0.1:8080"
    espocrm_api_key: SecretStr = SecretStr("")
    espocrm_timeout_seconds: float = Field(default=10, gt=0, le=60)
    callback_assignee_id: str = Field(default="", pattern=r"^(?:[A-Za-z0-9_-]{1,100})?$")
    callback_store_path: Path = Path("runtime/callbacks.sqlite3")

    @model_validator(mode="after")
    def validate_provider(self):
        if self.stt_enabled:
            if not self.stt_api_key.get_secret_value():
                raise ValueError("STT_ENABLED requires STT_API_KEY")
            if not self.stt_base_url.startswith("https://"):
                raise ValueError("STT_BASE_URL must use HTTPS")
        return self
