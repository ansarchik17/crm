from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    llm_mode: Literal["demo", "qwen"] = "demo"
    qwen_base_url: str = ""
    qwen_model: str = ""
    qwen_api_key: SecretStr = SecretStr("")
    # Omit the provider-specific option unless explicitly configured.
    qwen_enable_thinking: bool | None = None
    llm_timeout_seconds: float = Field(default=15, gt=0, le=60)
    stt_enabled: bool = False
    stt_base_url: str = "https://llm.alem.ai/v1"
    stt_model: Literal["speech-to-text", "speech-to-text-kk"] = "speech-to-text"
    stt_api_key: SecretStr = SecretStr("")
    stt_language: str = Field(default="", pattern=r"^([a-z]{2,3})?$")
    stt_timeout_seconds: float = Field(default=60, gt=0, le=120)
    openai_api_key: SecretStr = SecretStr("")
    openai_realtime_model: str = Field(default="gpt-realtime-2.1", min_length=1)
    openai_realtime_voice: str = "marin"
    openai_realtime_reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = "minimal"
    openai_realtime_timeout_seconds: float = Field(default=25, gt=0, le=60)

    @model_validator(mode="after")
    def validate_provider(self):
        if self.llm_mode == "qwen":
            if not (self.qwen_base_url and self.qwen_model and self.qwen_api_key.get_secret_value()):
                raise ValueError("qwen mode requires QWEN_BASE_URL, QWEN_MODEL and QWEN_API_KEY")
            if not self.qwen_base_url.startswith("https://"):
                raise ValueError("QWEN_BASE_URL must use HTTPS")
        if self.stt_enabled:
            if not self.stt_api_key.get_secret_value():
                raise ValueError("STT_ENABLED requires STT_API_KEY")
            if not self.stt_base_url.startswith("https://"):
                raise ValueError("STT_BASE_URL must use HTTPS")
        return self
