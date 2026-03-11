"""Shared fixtures for session service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from session_service.clients.policy_client import PolicyClient
from session_service.clients.workspace_client import WorkspaceClient
from session_service.config import Settings
from session_service.dependencies import get_session_service, get_task_service
from session_service.exceptions import ServiceError
from session_service.repositories.memory import InMemorySessionRepository
from session_service.repositories.memory_task import InMemoryTaskRepository
from session_service.routes import health, sandbox, sessions, tasks
from session_service.services.session_service import SessionService
from session_service.services.task_service import TaskService


def _make_policy_bundle(
    tenant_id: str = "t1", user_id: str = "u1", session_id: str = "sess-1"
) -> dict[str, Any]:
    return {
        "policyBundleVersion": "2026-02-28.1",
        "schemaVersion": "1.0",
        "tenantId": tenant_id,
        "userId": user_id,
        "sessionId": session_id,
        "expiresAt": "2026-03-01T00:00:00+00:00",
        "capabilities": [
            {"name": "File.Read", "allowedPaths": ["."]},
        ],
        "llmPolicy": {
            "allowedModels": ["claude-sonnet-4-20250514"],
            "maxInputTokens": 200000,
            "maxOutputTokens": 16384,
            "maxSessionTokens": 1000000,
        },
        "approvalRules": [],
    }


def _make_workspace_response(workspace_id: str = "ws-1") -> dict[str, Any]:
    return {
        "workspaceId": workspace_id,
        "workspaceScope": "general",
        "createdAt": "2026-02-28T00:00:00+00:00",
    }


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        min_desktop_app_version="0.1.0",
        min_agent_host_version="0.1.0",
        session_expiry_hours=24,
    )


@pytest.fixture
def session_repo() -> InMemorySessionRepository:
    return InMemorySessionRepository()


@pytest.fixture
def mock_policy_client() -> PolicyClient:
    client = AsyncMock(spec=PolicyClient)
    client.get_policy_bundle = AsyncMock(return_value=_make_policy_bundle())
    return client


@pytest.fixture
def mock_workspace_client() -> WorkspaceClient:
    client = AsyncMock(spec=WorkspaceClient)
    client.create_workspace = AsyncMock(return_value=_make_workspace_response())
    return client


@pytest.fixture
def task_repo() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def session_service(
    session_repo: InMemorySessionRepository,
    mock_policy_client: PolicyClient,
    mock_workspace_client: WorkspaceClient,
    settings: Settings,
) -> SessionService:
    return SessionService(session_repo, mock_policy_client, mock_workspace_client, settings)


@pytest.fixture
def task_service(
    task_repo: InMemoryTaskRepository,
    session_repo: InMemorySessionRepository,
) -> TaskService:
    return TaskService(task_repo, session_repo)


@pytest.fixture
async def client(
    session_service: SessionService,
    task_service: TaskService,
) -> AsyncIterator[AsyncClient]:
    async def _service_error_handler(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ServiceError)
        body: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.status_code >= 500,
        }
        return JSONResponse(status_code=exc.status_code, content=body)

    app = FastAPI()
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(sandbox.router)
    app.include_router(tasks.router)
    app.add_exception_handler(ServiceError, _service_error_handler)

    app.dependency_overrides[get_session_service] = lambda: session_service
    app.dependency_overrides[get_task_service] = lambda: task_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_create_request(**overrides: Any) -> dict[str, Any]:
    """Build a standard session create request body (camelCase, for route tests)."""
    base: dict[str, Any] = {
        "tenantId": "t1",
        "userId": "u1",
        "executionEnvironment": "desktop",
        "clientInfo": {
            "desktopAppVersion": "1.0.0",
            "localAgentHostVersion": "1.0.0",
            "osFamily": "macOS",
        },
        "supportedCapabilities": ["File.Read", "Shell.Exec"],
    }
    base.update(overrides)
    return base


def make_create_task_request(**overrides: Any) -> dict[str, Any]:
    """Build a standard task create request body (camelCase, for route tests)."""
    base: dict[str, Any] = {
        "taskId": "task-1",
        "prompt": "Write a hello world function",
        "maxSteps": 50,
    }
    base.update(overrides)
    return base


def make_service_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build kwargs for SessionService.create_session() (snake_case, for service tests)."""
    base: dict[str, Any] = {
        "tenant_id": "t1",
        "user_id": "u1",
        "execution_environment": "desktop",
        "client_info": {
            "desktopAppVersion": "1.0.0",
            "localAgentHostVersion": "1.0.0",
            "osFamily": "macOS",
        },
        "supported_capabilities": ["File.Read", "Shell.Exec"],
    }
    base.update(overrides)
    return base
