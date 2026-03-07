"""Tests for task HTTP endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import make_create_request, make_create_task_request


@pytest.mark.unit
class TestTaskRoutes:
    async def _create_session(self, client: AsyncClient) -> str:
        resp = await client.post("/sessions", json=make_create_request())
        return resp.json()["sessionId"]

    async def test_create_task(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        resp = await client.post(
            f"/sessions/{session_id}/tasks",
            json=make_create_task_request(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["taskId"] == "task-1"
        assert data["status"] == "running"

    async def test_complete_task(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        await client.post(
            f"/sessions/{session_id}/tasks",
            json=make_create_task_request(),
        )
        resp = await client.post(
            f"/sessions/{session_id}/tasks/task-1/complete",
            json={"status": "completed", "stepCount": 3},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    async def test_list_tasks(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        await client.post(
            f"/sessions/{session_id}/tasks",
            json=make_create_task_request(),
        )
        resp = await client.get(f"/sessions/{session_id}/tasks")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_get_task(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        await client.post(
            f"/sessions/{session_id}/tasks",
            json=make_create_task_request(),
        )
        resp = await client.get(f"/sessions/{session_id}/tasks/task-1")
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "Write a hello world function"

    async def test_get_task_not_found(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        resp = await client.get(f"/sessions/{session_id}/tasks/nonexistent")
        assert resp.status_code == 404

    async def test_create_task_session_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/sessions/nonexistent/tasks",
            json=make_create_task_request(),
        )
        assert resp.status_code == 404

    async def test_create_task_missing_prompt(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        resp = await client.post(
            f"/sessions/{session_id}/tasks",
            json={"taskId": "task-1"},
        )
        assert resp.status_code == 422

    async def test_complete_task_not_found(self, client: AsyncClient) -> None:
        session_id = await self._create_session(client)
        resp = await client.post(
            f"/sessions/{session_id}/tasks/nonexistent/complete",
            json={"status": "completed"},
        )
        assert resp.status_code == 404
