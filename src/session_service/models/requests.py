"""API request models for input validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateTaskRequest(BaseModel):
    """POST /sessions/{session_id}/tasks request body."""

    task_id: str = Field(alias="taskId", min_length=1)
    prompt: str = Field(min_length=1)
    max_steps: int = Field(alias="maxSteps", default=50, ge=1, le=200)


class CompleteTaskRequest(BaseModel):
    """POST /sessions/{session_id}/tasks/{task_id}/complete request body."""

    status: Literal["completed", "failed", "cancelled"] = Field(min_length=1)
    step_count: int = Field(alias="stepCount", default=0, ge=0)
    completion_reason: str | None = Field(alias="completionReason", default=None)


class UpdateSessionNameRequest(BaseModel):
    """PATCH /sessions/{session_id}/name request body."""

    name: str = Field(min_length=1, max_length=200)
    auto_named: bool = Field(alias="autoNamed", default=True)


class CreateSessionRequest(BaseModel):
    """POST /sessions request body."""

    tenant_id: str = Field(alias="tenantId", min_length=1)
    user_id: str = Field(alias="userId", min_length=1)
    execution_environment: Literal["desktop", "cloud_sandbox"] = Field(
        alias="executionEnvironment", default="desktop"
    )
    workspace_hint: dict[str, Any] | None = Field(alias="workspaceHint", default=None)
    client_info: dict[str, Any] = Field(alias="clientInfo", default_factory=dict)
    supported_capabilities: list[str] = Field(alias="supportedCapabilities", default_factory=list)
    session_type: Literal["lead", "teammate", "solo"] = Field(alias="sessionType", default="solo")
    team_id: str | None = Field(alias="teamId", default=None)
    parent_session_id: str | None = Field(alias="parentSessionId", default=None)
