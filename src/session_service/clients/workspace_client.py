"""HTTP client for the Workspace Service."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from session_service.exceptions import DownstreamError

logger = structlog.get_logger()


class WorkspaceClient:
    """Calls POST /workspaces on the Workspace Service."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    async def create_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_scope: str,
        local_path: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "tenantId": tenant_id,
            "userId": user_id,
            "workspaceScope": workspace_scope,
        }
        if local_path:
            body["localPath"] = local_path

        try:
            resp = await self._client.post("/workspaces", json=body)
            if resp.status_code >= 400:
                logger.error(
                    "workspace_service_error",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                raise DownstreamError("WorkspaceService", f"returned {resp.status_code}")
            try:
                data: dict[str, Any] = resp.json()
            except ValueError as exc:
                raise DownstreamError("WorkspaceService", "invalid JSON response") from exc
            return data
        except DownstreamError:
            raise
        except httpx.HTTPError as exc:
            raise DownstreamError("WorkspaceService", str(exc)) from exc
