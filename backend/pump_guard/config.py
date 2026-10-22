from pathlib import Path
from typing import Literal
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .research_data import DATABASE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUMP_GUARD_", env_file=".env", extra="ignore")
    database_path: Path = Path("data/pump_guard.sqlite3")
    research_database_path: Path = DATABASE
    cors_origins: list[str] = ["http://localhost:5173"]
    analysis_timeout_seconds: float = Field(default=60, gt=0, le=300)
    heartbeat_seconds: float = Field(default=10, gt=0, le=30)
    max_body_bytes: int = Field(default=16384, ge=1024, le=1048576)
    llm_mode: Literal["mock", "ollama", "openai"] = "mock"
    openai_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("PUMP_GUARD_OPENAI_API_KEY", "OPENAI_API_KEY"))
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=200)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = Field(default=120, gt=0, le=300)
    openai_max_output_tokens: int = Field(default=4096, ge=512, le=16384)
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = Field(default="qwen3:8b", min_length=1, max_length=200)
    ollama_timeout_seconds: float = Field(default=120, gt=0, le=300)
    ollama_num_ctx: int = Field(default=8192, ge=2048, le=32768)
    ollama_num_predict: int = Field(default=1024, ge=128, le=4096)
    max_concurrent_analyses: int = Field(default=2, ge=1, le=8)
    max_concurrent_llm: int = Field(default=1, ge=1, le=4)
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "pump-guard"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_workspace_id: str | None = None
    chat_grounded_fallback: bool = True
    agent_max_turns: int = Field(default=4, ge=2, le=8)
    agent_max_revisions: int = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def tracing_key_required(self):
        if self.langsmith_tracing and (not self.langsmith_api_key or not self.langsmith_api_key.get_secret_value().strip()):
            raise ValueError("LangSmith tracing requires PUMP_GUARD_LANGSMITH_API_KEY")
        return self

    @property
    def active_model(self):
        return self.openai_model if self.llm_mode == "openai" else self.ollama_model if self.llm_mode == "ollama" else None
