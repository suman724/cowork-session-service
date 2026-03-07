"""In-memory task repository for unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from session_service.models.domain import TaskDomain


class InMemoryTaskRepository:
    """Dict-backed task repository for testing."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDomain] = {}

    async def create(self, task: TaskDomain) -> None:
        self._tasks[task.task_id] = task

    async def get(self, task_id: str) -> TaskDomain | None:
        return self._tasks.get(task_id)

    async def update_completion(
        self,
        task_id: str,
        status: str,
        step_count: int,
        completion_reason: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = status  # type: ignore[assignment]
            task.step_count = step_count
            task.completion_reason = completion_reason
            task.completed_at = datetime.now(UTC)
            task.updated_at = datetime.now(UTC)

    async def list_by_session(self, session_id: str) -> list[TaskDomain]:
        return [t for t in self._tasks.values() if t.session_id == session_id]
