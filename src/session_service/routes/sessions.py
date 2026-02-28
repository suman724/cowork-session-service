"""Session CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from starlette.responses import Response

from session_service.dependencies import get_session_service
from session_service.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def create_session(
    body: dict[str, Any],
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.create_session(
        tenant_id=body["tenantId"],
        user_id=body["userId"],
        execution_environment=body.get("executionEnvironment", "desktop"),
        workspace_hint=body.get("workspaceHint"),
        client_info=body.get("clientInfo", {}),
        supported_capabilities=body.get("supportedCapabilities", []),
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


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    service: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    return await service.get_session(session_id)
