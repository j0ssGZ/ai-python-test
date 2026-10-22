import os

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    database_path: str = "data/requests.sqlite3"
    backlog_capacity: int = Field(default=10_000, ge=1)
    max_input_chars: int = Field(default=8_000, ge=1)
    max_body_bytes: int = Field(default=32_768, ge=1)
    workers_enabled: bool = True
    provider_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:3001")
    provider_api_key: SecretStr | None = None
    ai_workers: int = Field(default=16, ge=1, le=64)
    notify_workers: int = Field(default=4, ge=1, le=50)
    ai_timeout: float = Field(default=8, gt=0)
    notify_timeout: float = Field(default=5, gt=0)
    lease_seconds: float = Field(default=30, gt=0)
    shutdown_seconds: float = Field(default=9, gt=0)
    poll_interval: float = Field(default=0.1, gt=0)
    ai_max_attempts: int = Field(default=3, ge=1, le=10)
    notify_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base: float = Field(default=1, ge=0)
    retry_cap: float = Field(default=30, gt=0)
    notify_rate_limit: int = Field(default=50, ge=1, le=50)
    notify_rate_window: float = Field(default=10, ge=10)
    notify_spacing: float = Field(default=1 / 4.5, ge=0)

    @model_validator(mode="after")
    def timing(self) -> "Settings":
        if self.lease_seconds <= max(self.ai_timeout, self.notify_timeout) + 2:
            raise ValueError("Lease must exceed attempt deadlines by more than two seconds")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.model_validate(
            {
                name: os.environ[f"AI_NOTIFY_{name.upper()}"]
                for name in cls.model_fields
                if f"AI_NOTIFY_{name.upper()}" in os.environ
            }
        )
