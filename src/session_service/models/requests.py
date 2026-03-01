"""API request models for input validation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """POST /sessions request body."""

    tenant_id: str = Field(alias="tenantId", min_length=1)
    user_id: str = Field(alias="userId", min_length=1)
    execution_environment: str = Field(alias="executionEnvironment", default="desktop")
    workspace_hint: dict[str, Any] | None = Field(alias="workspaceHint", default=None)
    client_info: dict[str, Any] = Field(alias="clientInfo", default_factory=dict)
    supported_capabilities: list[str] = Field(alias="supportedCapabilities", default_factory=list)
