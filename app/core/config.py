"""Central configuration.

Local development reads from a `.env` file. In AWS, configuration comes from
environment variables injected by the ECS task definition, and secrets are
pulled from AWS Secrets Manager at startup (see `load_secrets`).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AIProviderName = Literal["bedrock", "mock"]
LedgerBackendName = Literal["sqlite", "dynamodb"]
RazorpayMode = Literal["test", "mock"]


class Settings(BaseSettings):
    """Runtime settings for both agents."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -----------------------------------------------------
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="console", alias="LOG_FORMAT")

    # --- AWS -------------------------------------------------------------
    aws_region: str = Field(default="ap-south-1", alias="AWS_REGION")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_profile: str | None = Field(default=None, alias="AWS_PROFILE")

    # --- AI provider -----------------------------------------------------
    ai_provider: AIProviderName = Field(default="mock", alias="AI_PROVIDER")
    bedrock_model_id: str = Field(
        default="anthropic.claude-3-5-sonnet-20240620-v1:0", alias="BEDROCK_MODEL_ID"
    )
    bedrock_embedding_model_id: str = Field(
        default="amazon.titan-embed-text-v2:0", alias="BEDROCK_EMBEDDING_MODEL_ID"
    )
    bedrock_max_tokens: int = Field(default=1024, alias="BEDROCK_MAX_TOKENS")
    bedrock_temperature: float = Field(default=0.0, alias="BEDROCK_TEMPERATURE")
    bedrock_guardrail_id: str | None = Field(default=None, alias="BEDROCK_GUARDRAIL_ID")
    bedrock_guardrail_version: str | None = Field(
        default=None, alias="BEDROCK_GUARDRAIL_VERSION"
    )

    # --- Razorpay --------------------------------------------------------
    razorpay_mode: RazorpayMode = Field(default="mock", alias="RAZORPAY_MODE")
    razorpay_key_id: str | None = Field(default=None, alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str | None = Field(default=None, alias="RAZORPAY_KEY_SECRET")
    razorpay_webhook_secret: str | None = Field(default=None, alias="RAZORPAY_WEBHOOK_SECRET")

    # --- AP2 mandates ----------------------------------------------------
    ap2_mandate_secret: str = Field(default="local-dev-insecure-secret", alias="AP2_MANDATE_SECRET")
    ap2_mandate_ttl_seconds: int = Field(default=900, alias="AP2_MANDATE_TTL_SECONDS")
    ap2_absolute_max_amount: int = Field(
        default=5_000_00, alias="AP2_ABSOLUTE_MAX_AMOUNT"
    )  # hard ceiling in minor units (paise)

    # --- Storage ---------------------------------------------------------
    ledger_backend: LedgerBackendName = Field(default="sqlite", alias="LEDGER_BACKEND")
    database_url: str = Field(default="sqlite:///./data/merchant_ledger.db", alias="DATABASE_URL")
    buyer_database_url: str = Field(
        default="sqlite:///./data/buyer_ledger.db", alias="BUYER_DATABASE_URL"
    )
    dynamodb_table_name: str = Field(default="acp-audit-ledger", alias="DYNAMODB_TABLE_NAME")
    dynamodb_endpoint_url: str | None = Field(default=None, alias="DYNAMODB_ENDPOINT_URL")

    # --- Security --------------------------------------------------------
    secrets_manager_secret_name: str | None = Field(
        default=None, alias="SECRETS_MANAGER_SECRET_NAME"
    )
    kms_key_id: str | None = Field(default=None, alias="KMS_KEY_ID")
    a2a_api_key: str = Field(default="local-dev-a2a-key", alias="A2A_API_KEY")

    # --- Networking ------------------------------------------------------
    buyer_agent_url: str = Field(default="http://localhost:8001", alias="BUYER_AGENT_URL")
    merchant_agent_url: str = Field(default="http://localhost:8000", alias="MERCHANT_AGENT_URL")
    buyer_agent_port: int = Field(default=8001, alias="BUYER_AGENT_PORT")
    merchant_agent_port: int = Field(default=8000, alias="MERCHANT_AGENT_PORT")
    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")

    # --- Catalog ---------------------------------------------------------
    catalog_path: str = Field(default="./data/catalog.json", alias="CATALOG_PATH")
    embedding_cache_path: str = Field(
        default="./data/embedding_cache.json", alias="EMBEDDING_CACHE_PATH"
    )

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod", "staging"}

    @property
    def sqlite_path(self) -> str:
        return _sqlite_path(self.database_url)

    @property
    def buyer_sqlite_path(self) -> str:
        return _sqlite_path(self.buyer_database_url)

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs."""
        candidates = [
            self.ap2_mandate_secret,
            self.razorpay_key_secret,
            self.razorpay_webhook_secret,
            self.aws_secret_access_key,
            self.a2a_api_key,
        ]
        return [c for c in candidates if c and len(c) >= 8]


def _sqlite_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    if url.startswith("sqlite://"):
        return url[len("sqlite://") :]
    return url


def load_secrets_from_aws(settings: Settings) -> dict[str, Any]:
    """Fetch a JSON secret bundle from AWS Secrets Manager.

    Returns an empty dict when no secret name is configured or when the fetch
    fails in a non-production environment (so local development never breaks).
    """
    if not settings.secrets_manager_secret_name:
        return {}
    try:  # pragma: no cover - requires live AWS
        import boto3

        session = boto3.session.Session(
            profile_name=settings.aws_profile, region_name=settings.aws_region
        )
        client = session.client("secretsmanager")
        response = client.get_secret_value(SecretId=settings.secrets_manager_secret_name)
        payload = response.get("SecretString") or "{}"
        data = json.loads(payload)
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items()}
    except Exception as exc:  # pragma: no cover - requires live AWS
        if settings.is_production:
            raise
        import logging

        logging.getLogger(__name__).warning(
            "Secrets Manager unavailable, continuing with .env values: %s", type(exc).__name__
        )
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once, overlaying AWS Secrets Manager values if configured."""
    settings = Settings()
    overlay = load_secrets_from_aws(settings)
    if overlay:
        for key, value in overlay.items():
            os.environ[key.upper()] = str(value)
        settings = Settings()
    return settings


def reset_settings_cache() -> None:
    """Testing helper: drop the cached Settings instance."""
    get_settings.cache_clear()
