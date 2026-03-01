"""Tests for HTTP clients (PolicyClient, WorkspaceClient)."""

from __future__ import annotations

import httpx
import pytest

from session_service.clients.policy_client import PolicyClient
from session_service.clients.workspace_client import WorkspaceClient
from session_service.exceptions import DownstreamError, PolicyBundleError


@pytest.mark.unit
class TestPolicyClient:
    async def test_successful_fetch(self) -> None:
        bundle = {"policyBundleVersion": "1.0", "capabilities": []}

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=bundle)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = PolicyClient(http)
            result = await client.get_policy_bundle(
                tenant_id="t1",
                user_id="u1",
                session_id="s1",
                capabilities=["File.Read"],
            )
            assert result == bundle

    async def test_server_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = PolicyClient(http)
            with pytest.raises(PolicyBundleError):
                await client.get_policy_bundle(
                    tenant_id="t1",
                    user_id="u1",
                    session_id="s1",
                    capabilities=[],
                )

    async def test_connection_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = PolicyClient(http)
            with pytest.raises(DownstreamError):
                await client.get_policy_bundle(
                    tenant_id="t1",
                    user_id="u1",
                    session_id="s1",
                    capabilities=[],
                )


@pytest.mark.unit
class TestWorkspaceClient:
    async def test_successful_create(self) -> None:
        ws = {"workspaceId": "ws-1", "workspaceScope": "general"}

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=ws)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = WorkspaceClient(http)
            result = await client.create_workspace(
                tenant_id="t1",
                user_id="u1",
                workspace_scope="general",
            )
            assert result["workspaceId"] == "ws-1"

    async def test_server_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = WorkspaceClient(http)
            with pytest.raises(DownstreamError):
                await client.create_workspace(
                    tenant_id="t1",
                    user_id="u1",
                    workspace_scope="general",
                )

    async def test_connection_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = WorkspaceClient(http)
            with pytest.raises(DownstreamError):
                await client.create_workspace(
                    tenant_id="t1",
                    user_id="u1",
                    workspace_scope="general",
                )
