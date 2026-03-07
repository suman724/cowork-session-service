"""Task CRUD endpoints — sub-resource of sessions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from session_service.dependencies import get_task_service
from session_service.models.requests import CompleteTaskRequest, CreateTaskRequest
from session_service.services.task_service import TaskService

router = APIRouter(prefix="/sessions/{session_id}/tasks", tags=["tasks"])


@router.post("", status_code=201)
async def create_task(
    session_id: str,
    body: CreateTaskRequest,
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    return await service.create_task(
        session_id=session_id,
        task_id=body.task_id,
        prompt=body.prompt,
        max_steps=body.max_steps,
    )


@router.post("/{task_id}/complete")
async def complete_task(
    session_id: str,
    task_id: str,
    body: CompleteTaskRequest,
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    return await service.complete_task(
        session_id=session_id,
        task_id=task_id,
        status=body.status,
        step_count=body.step_count,
        completion_reason=body.completion_reason,
    )


@router.get("")
async def list_tasks(
    session_id: str,
    service: TaskService = Depends(get_task_service),
) -> list[dict[str, Any]]:
    return await service.list_tasks(session_id)


@router.get("/{task_id}")
async def get_task(
    session_id: str,
    task_id: str,
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    return await service.get_task(session_id, task_id)
