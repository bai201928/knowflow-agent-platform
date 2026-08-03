from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "mysql+asyncmy://knowflow:knowflow@127.0.0.1:3306/knowflow"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_JWT_SECRET = "replace-this-local-secret-with-at-least-32-chars"  # noqa: S105 - local sentinel
LOCAL_ENVIRONMENTS = frozenset({"local", "test"})


class RuntimeRole(StrEnum):
    API = "api"
    WORKFLOW = "workflow"
    OUTBOX = "outbox"
    CONSUMER = "consumer"
    RECONCILIATION = "reconciliation"


class ModelMode(StrEnum):
    STUB = "stub"
    REAL = "real"


class Settings(BaseSettings):
    """Validated process configuration. Secrets are never represented as plain strings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KNOWFLOW_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "local"
    runtime_role: RuntimeRole = RuntimeRole.API
    debug: bool = False

    database_url: SecretStr = SecretStr(DEFAULT_DATABASE_URL)
    redis_url: SecretStr = SecretStr(DEFAULT_REDIS_URL)
    milvus_uri: str = "http://127.0.0.1:19530"
    rocketmq_endpoints: str = "127.0.0.1:8081"
    mailpit_smtp_host: str = "127.0.0.1"
    mailpit_smtp_port: int = Field(default=1025, ge=1, le=65535)

    jwt_secret: SecretStr = SecretStr(DEFAULT_JWT_SECRET)
    jwt_issuer: str = "knowflow-local"
    jwt_audience: str = "knowflow-api"
    jwt_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    model_mode: ModelMode = ModelMode.STUB
    model_base_url: str = "https://api.deepseek.com/v1"
    model_name: str = "deepseek-chat"
    model_api_key: SecretStr | None = None
    model_timeout_seconds: float = Field(default=20.0, gt=0, le=60)

    max_request_concurrency: int = Field(default=20, ge=1, le=200)
    max_model_concurrency: int = Field(default=4, ge=1, le=32)
    default_interaction_deadline_seconds: float = Field(default=15.0, ge=3, le=60)
    max_document_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    operations_sandbox_enabled: bool = True
    notification_sandbox_enabled: bool = True
    telemetry_export_enabled: bool = False

    @model_validator(mode="after")
    def validate_security_boundary(self) -> Settings:
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("KNOWFLOW_JWT_SECRET must contain at least 32 characters")
        if self.model_mode is ModelMode.REAL and (
            self.model_api_key is None or not self.model_api_key.get_secret_value().strip()
        ):
            raise ValueError("KNOWFLOW_MODEL_API_KEY is required when MODEL_MODE=real")
        if self.environment.lower() not in LOCAL_ENVIRONMENTS:
            if secret == DEFAULT_JWT_SECRET:
                raise ValueError("non-local environments require an explicit JWT secret")
            if self.database_url.get_secret_value() == DEFAULT_DATABASE_URL:
                raise ValueError("non-local environments require an explicit database URL")
            if self.redis_url.get_secret_value() == DEFAULT_REDIS_URL:
                raise ValueError("non-local environments require an explicit Redis URL")
            if not self.operations_sandbox_enabled or not self.notification_sandbox_enabled:
                raise ValueError(
                    "non-local MVP execution must keep both sandbox boundaries enabled"
                )
        return self

    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()

    def redis_dsn(self) -> str:
        return self.redis_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
