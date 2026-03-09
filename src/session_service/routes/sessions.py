"""Session CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from starlette.responses import Response

from session_service.dependencies import get_session_service
from session_service.models.requests import CreateSessionRequest, UpdateSessionNameRequest
from session_service.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def create_session(
    body: CreateSessionRequest,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.create_session(
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        execution_environment=body.execution_environment,
        workspace_hint=body.workspace_hint,
        client_info=body.client_info,
        supported_capabilities=body.supported_capabilities,
        session_type=body.session_type,
        team_id=body.team_id,
        parent_session_id=body.parent_session_id,
    )


@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.resume_session(session_id)


@router.post("/{session_id}/cancel", status_code=204)
async def cancel_session(
    session_id: str,
    service: SessionService = Depends(get_session_service),
) -> Response:
    await service.cancel_session(session_id)
    return Response(status_code=204)


@router.patch("/{session_id}/name")
async def update_session_name(
    session_id: str,
    body: UpdateSessionNameRequest,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.update_session_name(session_id, body.name, body.auto_named)


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.get_session(session_id)
