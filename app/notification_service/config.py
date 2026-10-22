import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    database_path: str = "data/requests.sqlite3"
    backlog_capacity: int = Field(default=10_000, ge=1)
    max_input_chars: int = Field(default=8_000, ge=1)
    max_body_bytes: int = Field(default=32_768, ge=1)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.model_validate(
            {
                name: os.environ[f"AI_NOTIFY_{name.upper()}"]
                for name in cls.model_fields
                if f"AI_NOTIFY_{name.upper()}" in os.environ
            }
        )
