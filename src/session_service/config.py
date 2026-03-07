"""Application settings loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    env: str = "dev"
    log_level: str = "info"
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None
    dynamodb_table_prefix: str = "dev-"
    policy_service_url: str = "http://localhost:8001"
    workspace_service_url: str = "http://localhost:8002"
    downstream_timeout: float = 30.0
    min_desktop_app_version: str = "0.1.0"
    min_agent_host_version: str = "0.1.0"
    session_expiry_hours: int = 24

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    @property
    def sessions_table(self) -> str:
        return f"{self.dynamodb_table_prefix}sessions"

    @property
    def tasks_table(self) -> str:
        return f"{self.dynamodb_table_prefix}tasks"
