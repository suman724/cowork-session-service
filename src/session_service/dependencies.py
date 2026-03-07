"""FastAPI dependency providers."""

from __future__ import annotations

from fastapi import Request

from session_service.services.session_service import SessionService
from session_service.services.task_service import TaskService


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service  # type: ignore[no-any-return]


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service  # type: ignore[no-any-return]
