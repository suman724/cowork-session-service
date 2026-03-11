"""Sandbox lifecycle endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from session_service.dependencies import get_session_service
from session_service.models.requests import SandboxRegistrationRequest
from session_service.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sandbox"])


@router.post("/{session_id}/register")
async def register_sandbox(
    session_id: str,
    body: SandboxRegistrationRequest,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.register_sandbox(
        session_id,
        sandbox_endpoint=body.sandbox_endpoint,
        task_arn=body.task_arn,
        registration_token=body.registration_token,
    )
