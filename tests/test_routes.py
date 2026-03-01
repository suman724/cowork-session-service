"""Tests for HTTP endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import make_create_request


@pytest.mark.unit
class TestHealthRoutes:
    async def test_health(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_ready(self, client: AsyncClient) -> None:
        resp = await client.get("/ready")
        assert resp.status_code == 200


@pytest.mark.unit
class TestSessionRoutes:
    async def test_create_session(self, client: AsyncClient) -> None:
        resp = await client.post("/sessions", json=make_create_request())
        assert resp.status_code == 201
        data = resp.json()
        assert "sessionId" in data
        assert data["compatibilityStatus"] == "compatible"

    async def test_get_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/sessions", json=make_create_request())
        session_id = create_resp.json()["sessionId"]

        resp = await client.get(f"/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["sessionId"] == session_id

    async def test_resume_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/sessions", json=make_create_request())
        session_id = create_resp.json()["sessionId"]

        resp = await client.post(f"/sessions/{session_id}/resume")
        assert resp.status_code == 200
        assert resp.json()["policyBundle"] is not None

    async def test_cancel_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/sessions", json=make_create_request())
        session_id = create_resp.json()["sessionId"]

        resp = await client.post(f"/sessions/{session_id}/cancel")
        assert resp.status_code == 204

    async def test_create_missing_tenant_id(self, client: AsyncClient) -> None:
        body = make_create_request()
        del body["tenantId"]
        resp = await client.post("/sessions", json=body)
        assert resp.status_code == 422

    async def test_create_empty_tenant_id(self, client: AsyncClient) -> None:
        resp = await client.post("/sessions", json=make_create_request(tenantId=""))
        assert resp.status_code == 422

    async def test_get_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/sessions/nonexistent")
        assert resp.status_code == 404
