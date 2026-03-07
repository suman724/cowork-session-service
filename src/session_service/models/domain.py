"""Session domain model with state machine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

SessionState = Literal[
    "SESSION_CREATED",
    "SESSION_RUNNING",
    "WAITING_FOR_LLM",
    "WAITING_FOR_TOOL",
    "WAITING_FOR_APPROVAL",
    "SESSION_PAUSED",
    "SESSION_COMPLETED",
    "SESSION_FAILED",
    "SESSION_CANCELLED",
]

# Valid state transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "SESSION_CREATED": {"SESSION_RUNNING", "SESSION_FAILED", "SESSION_CANCELLED"},
    "SESSION_RUNNING": {
        "WAITING_FOR_LLM",
        "WAITING_FOR_TOOL",
        "WAITING_FOR_APPROVAL",
        "SESSION_PAUSED",
        "SESSION_COMPLETED",
        "SESSION_FAILED",
        "SESSION_CANCELLED",
    },
    "WAITING_FOR_LLM": {
        "SESSION_RUNNING",
        "SESSION_FAILED",
        "SESSION_CANCELLED",
    },
    "WAITING_FOR_TOOL": {
        "SESSION_RUNNING",
        "SESSION_FAILED",
        "SESSION_CANCELLED",
    },
    "WAITING_FOR_APPROVAL": {
        "SESSION_RUNNING",
        "SESSION_FAILED",
        "SESSION_CANCELLED",
    },
    "SESSION_PAUSED": {
        "SESSION_RUNNING",
        "SESSION_FAILED",
        "SESSION_CANCELLED",
    },
    "SESSION_COMPLETED": {"SESSION_RUNNING"},  # Allow resume
    "SESSION_FAILED": {"SESSION_RUNNING"},  # Allow resume after failure
    "SESSION_CANCELLED": set(),
}


class SessionDomain(BaseModel):
    """Internal session representation."""

    session_id: str
    workspace_id: str
    tenant_id: str
    user_id: str
    execution_environment: Literal["desktop", "cloud_sandbox"]
    status: SessionState
    desktop_app_version: str | None = None
    agent_host_version: str | None = None
    supported_capabilities: list[str] = []
    name: str = ""
    auto_named: bool = True
    created_at: datetime
    expires_at: datetime
    updated_at: datetime | None = None
    ttl: int | None = None

    def can_transition_to(self, new_status: str) -> bool:
        """Check if the given transition is valid."""
        allowed = VALID_TRANSITIONS.get(self.status, set())
        return new_status in allowed


TaskState = Literal["running", "completed", "failed", "cancelled"]


class TaskDomain(BaseModel):
    """Internal task representation."""

    task_id: str
    session_id: str
    workspace_id: str
    tenant_id: str
    user_id: str
    prompt: str
    status: TaskState
    step_count: int = 0
    max_steps: int = 50
    completion_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    ttl: int | None = None
