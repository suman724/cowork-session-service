"""Task lifecycle business logic."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from session_service.exceptions import ConflictError, SessionNotFoundError, TaskNotFoundError
from session_service.models.domain import TaskDomain
from session_service.repositories.base import SessionRepository, TaskRepository

logger = structlog.get_logger()


class TaskService:
    def __init__(self, task_repo: TaskRepository, session_repo: SessionRepository) -> None:
        self._task_repo = task_repo
        self._session_repo = session_repo

    async def create_task(
        self,
        *,
        session_id: str,
        task_id: str,
        prompt: str,
        max_steps: int = 50,
    ) -> dict[str, Any]:
        """Create a new task within a session."""
        session = await self._session_repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        if session.status not in ("SESSION_RUNNING", "WAITING_FOR_LLM", "WAITING_FOR_TOOL"):
            raise ConflictError(f"Cannot create task in session with status {session.status}")

        now = datetime.now(UTC)
        task = TaskDomain(
            task_id=task_id,
            session_id=session_id,
            workspace_id=session.workspace_id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            prompt=prompt,
            status="running",
            max_steps=max_steps,
            created_at=now,
            ttl=int(session.expires_at.timestamp()),
        )
        await self._task_repo.create(task)

        logger.info("task_created", task_id=task_id, session_id=session_id)

        return {
            "taskId": task.task_id,
            "sessionId": task.session_id,
            "status": task.status,
            "createdAt": task.created_at.isoformat(),
        }

    async def complete_task(
        self,
        *,
        session_id: str,
        task_id: str,
        status: str,
        step_count: int = 0,
        completion_reason: str | None = None,
    ) -> dict[str, Any]:
        """Mark a task as completed/failed/cancelled."""
        task = await self._task_repo.get(task_id)
        if task is None or task.session_id != session_id:
            raise TaskNotFoundError(task_id)

        if task.status != "running":
            raise ConflictError(f"Cannot complete task in {task.status} state")

        await self._task_repo.update_completion(task_id, status, step_count, completion_reason)

        logger.info("task_completed", task_id=task_id, status=status)

        return {
            "taskId": task_id,
            "status": status,
            "stepCount": step_count,
            "completionReason": completion_reason,
        }

    async def list_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """List all tasks for a session."""
        tasks = await self._task_repo.list_by_session(session_id)
        return [_task_to_dict(t) for t in tasks]

    async def get_task(self, session_id: str, task_id: str) -> dict[str, Any]:
        """Get a single task."""
        task = await self._task_repo.get(task_id)
        if task is None or task.session_id != session_id:
            raise TaskNotFoundError(task_id)
        return _task_to_dict(task)


def _task_to_dict(t: TaskDomain) -> dict[str, Any]:
    result: dict[str, Any] = {
        "taskId": t.task_id,
        "sessionId": t.session_id,
        "workspaceId": t.workspace_id,
        "prompt": t.prompt,
        "status": t.status,
        "stepCount": t.step_count,
        "maxSteps": t.max_steps,
        "createdAt": t.created_at.isoformat(),
    }
    if t.completion_reason:
        result["completionReason"] = t.completion_reason
    if t.completed_at:
        result["completedAt"] = t.completed_at.isoformat()
    return result
