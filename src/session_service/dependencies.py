"""FastAPI dependency providers."""

from __future__ import annotations

import httpx
from fastapi import Request

from session_service.services.file_upload_service import FileUploadService
from session_service.services.proxy_service import ProxyService
from session_service.services.sandbox_service import SandboxService
from session_service.services.session_service import SessionService
from session_service.services.task_service import TaskService


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service  # type: ignore[no-any-return]


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service  # type: ignore[no-any-return]


def get_sandbox_service(request: Request) -> SandboxService | None:
    return request.app.state.sandbox_service  # type: ignore[no-any-return]


def get_proxy_service(request: Request) -> ProxyService:
    return request.app.state.proxy_service  # type: ignore[no-any-return]


def get_proxy_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.proxy_http  # type: ignore[no-any-return]


def get_workspace_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.workspace_http  # type: ignore[no-any-return]


def get_file_upload_service(request: Request) -> FileUploadService:
    return request.app.state.file_upload_service  # type: ignore[no-any-return]
