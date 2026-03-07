"""Tests for TaskService business logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from session_service.exceptions import ConflictError, SessionNotFoundError, TaskNotFoundError
from session_service.models.domain import SessionDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.repositories.memory_task import InMemoryTaskRepository
from session_service.services.task_service import TaskService


def _make_session(session_id: str = "sess-1") -> SessionDomain:
    now = datetime.now(UTC)
    return SessionDomain(
        session_id=session_id,
        workspace_id="ws-1",
        tenant_id="t1",
        user_id="u1",
        execution_environment="desktop",
        status="SESSION_RUNNING",
        created_at=now,
        expires_at=now + timedelta(hours=24),
        ttl=int((now + timedelta(hours=24)).timestamp()),
    )


@pytest.mark.unit
class TestCreateTask:
    async def test_create_task_successfully(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        result = await task_service.create_task(
            session_id="sess-1", task_id="task-1", prompt="Hello", max_steps=10
        )
        assert result["taskId"] == "task-1"
        assert result["sessionId"] == "sess-1"
        assert result["status"] == "running"

    async def test_create_task_denormalizes_session_fields(
        self,
        task_service: TaskService,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        task = await task_repo.get("task-1")
        assert task is not None
        assert task.workspace_id == "ws-1"
        assert task.tenant_id == "t1"
        assert task.user_id == "u1"

    async def test_create_task_session_not_found(self, task_service: TaskService) -> None:
        with pytest.raises(SessionNotFoundError):
            await task_service.create_task(
                session_id="nonexistent", task_id="task-1", prompt="Hello"
            )

    async def test_create_task_session_wrong_status(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        session = _make_session()
        session.status = "SESSION_COMPLETED"  # type: ignore[assignment]
        await session_repo.create(session)

        with pytest.raises(ConflictError, match="Cannot create task"):
            await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")


@pytest.mark.unit
class TestCompleteTask:
    async def test_complete_task_successfully(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        result = await task_service.complete_task(
            session_id="sess-1",
            task_id="task-1",
            status="completed",
            step_count=5,
            completion_reason="completed",
        )
        assert result["status"] == "completed"
        assert result["stepCount"] == 5

    async def test_complete_task_not_found(self, task_service: TaskService) -> None:
        with pytest.raises(TaskNotFoundError):
            await task_service.complete_task(
                session_id="sess-1", task_id="nonexistent", status="completed"
            )

    async def test_complete_task_already_completed(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        await task_service.complete_task(session_id="sess-1", task_id="task-1", status="completed")
        with pytest.raises(ConflictError, match="Cannot complete"):
            await task_service.complete_task(session_id="sess-1", task_id="task-1", status="failed")

    async def test_complete_task_wrong_session(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        with pytest.raises(TaskNotFoundError):
            await task_service.complete_task(
                session_id="sess-other", task_id="task-1", status="completed"
            )


@pytest.mark.unit
class TestListTasks:
    async def test_list_tasks(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        await task_service.create_task(session_id="sess-1", task_id="task-2", prompt="World")
        result = await task_service.list_tasks("sess-1")
        assert len(result) == 2

    async def test_list_tasks_empty(self, task_service: TaskService) -> None:
        result = await task_service.list_tasks("sess-empty")
        assert result == []


@pytest.mark.unit
class TestGetTask:
    async def test_get_task(
        self, task_service: TaskService, session_repo: InMemorySessionRepository
    ) -> None:
        await session_repo.create(_make_session())
        await task_service.create_task(session_id="sess-1", task_id="task-1", prompt="Hello")
        result = await task_service.get_task("sess-1", "task-1")
        assert result["taskId"] == "task-1"
        assert result["prompt"] == "Hello"

    async def test_get_task_not_found(self, task_service: TaskService) -> None:
        with pytest.raises(TaskNotFoundError):
            await task_service.get_task("sess-1", "nonexistent")
