"""HTTP client for the Policy Service."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from session_service.exceptions import DownstreamError, PolicyBundleError

logger = structlog.get_logger()


class PolicyClient:
    """Calls GET /policy-bundles on the Policy Service."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    async def get_policy_bundle(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        capabilities: list[str],
    ) -> dict[str, Any]:
        try:
            resp = await self._client.get(
                "/policy-bundles",
                params={
                    "tenantId": tenant_id,
                    "userId": user_id,
                    "sessionId": session_id,
                    "capabilities": ",".join(capabilities),
                },
            )
            if resp.status_code >= 400:
                logger.error(
                    "policy_service_error",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                raise PolicyBundleError(f"Policy Service returned {resp.status_code}")
            return resp.json()  # type: ignore[no-any-return]
        except PolicyBundleError:
            raise
        except httpx.HTTPError as exc:
            raise DownstreamError("PolicyService", str(exc)) from exc
