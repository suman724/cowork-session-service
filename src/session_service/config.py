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

    # Sandbox launcher configuration
    sandbox_launcher_type: str = "ecs"  # "ecs" or "local"
    sandbox_max_concurrent_sessions: int = 5
    # ECS launcher settings
    ecs_cluster: str = ""
    ecs_task_definition: str = ""
    ecs_subnets: list[str] = []
    ecs_security_groups: list[str] = []
    sandbox_image: str = ""
    # Local launcher settings (for development)
    agent_runtime_path: str = "../cowork-agent-runtime"
    session_service_url: str = "http://localhost:8000"
    # Sandbox lifecycle settings
    sandbox_idle_timeout_seconds: int = 1800  # 30 minutes
    sandbox_max_duration_seconds: int = 14400  # 4 hours
    sandbox_provision_timeout_seconds: int = 180  # 3 minutes
    sandbox_lifecycle_check_interval_seconds: int = 300  # 5 minutes

    # Proxy settings
    proxy_endpoint_cache_ttl_seconds: int = 30
    proxy_activity_batch_seconds: int = 60
    proxy_timeout_seconds: float = 30.0
    proxy_sse_timeout_seconds: float = 14400.0  # 4 hours

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    @property
    def sessions_table(self) -> str:
        return f"{self.dynamodb_table_prefix}sessions"

    @property
    def tasks_table(self) -> str:
        return f"{self.dynamodb_table_prefix}tasks"
