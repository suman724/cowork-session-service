"""DynamoDB session repository.

Table: {env}-sessions
  PK: sessionId
  GSI: tenantId-userId-index (PK=tenantId, SK=userId)
  TTL attribute: ttl
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from session_service.models.domain import SessionDomain


class DynamoSessionRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def create(self, session: SessionDomain) -> None:
        item = _to_item(session)
        await self._table.put_item(Item=item)

    async def get(self, session_id: str) -> SessionDomain | None:
        resp = await self._table.get_item(Key={"sessionId": session_id})
        item = resp.get("Item")
        return _from_item(item) if item else None

    async def update_status(self, session_id: str, status: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET #s = :status, updatedAt = :ua",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": status, ":ua": now},
        )

    async def list_by_tenant_user(self, tenant_id: str, user_id: str) -> list[SessionDomain]:
        resp = await self._table.query(
            IndexName="tenantId-userId-index",
            KeyConditionExpression="tenantId = :tid AND userId = :uid",
            ExpressionAttributeValues={":tid": tenant_id, ":uid": user_id},
        )
        return [_from_item(item) for item in resp.get("Items", [])]

    async def delete(self, session_id: str) -> None:
        await self._table.delete_item(Key={"sessionId": session_id})


def _to_item(s: SessionDomain) -> dict[str, Any]:
    item: dict[str, Any] = {
        "sessionId": s.session_id,
        "workspaceId": s.workspace_id,
        "tenantId": s.tenant_id,
        "userId": s.user_id,
        "executionEnvironment": s.execution_environment,
        "status": s.status,
        "createdAt": s.created_at.isoformat(),
        "expiresAt": s.expires_at.isoformat(),
        "updatedAt": (s.updated_at or s.created_at).isoformat(),
    }
    if s.desktop_app_version:
        item["desktopAppVersion"] = s.desktop_app_version
    if s.agent_host_version:
        item["agentHostVersion"] = s.agent_host_version
    if s.supported_capabilities:
        item["supportedCapabilities"] = s.supported_capabilities
    if s.ttl is not None:
        item["ttl"] = s.ttl
    return item


def _from_item(item: dict[str, Any]) -> SessionDomain:
    return SessionDomain(
        session_id=item["sessionId"],
        workspace_id=item["workspaceId"],
        tenant_id=item["tenantId"],
        user_id=item["userId"],
        execution_environment=item["executionEnvironment"],
        status=item["status"],
        desktop_app_version=item.get("desktopAppVersion"),
        agent_host_version=item.get("agentHostVersion"),
        supported_capabilities=item.get("supportedCapabilities", []),
        created_at=datetime.fromisoformat(item["createdAt"]),
        expires_at=datetime.fromisoformat(item["expiresAt"]),
        updated_at=datetime.fromisoformat(item["updatedAt"]) if item.get("updatedAt") else None,
        ttl=item.get("ttl"),
    )
