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

    async def update_expiry(self, session_id: str, expires_at: datetime) -> None:
        now = datetime.now(UTC).isoformat()
        ttl = int(expires_at.timestamp())
        await self._table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET expiresAt = :ea, #ttl = :ttl, updatedAt = :ua",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":ea": expires_at.isoformat(),
                ":ttl": ttl,
                ":ua": now,
            },
        )

    async def list_by_tenant_user(self, tenant_id: str, user_id: str) -> list[SessionDomain]:
        resp = await self._table.query(
            IndexName="tenantId-userId-index",
            KeyConditionExpression="tenantId = :tid",
            FilterExpression="userId = :uid",
            ExpressionAttributeValues={":tid": tenant_id, ":uid": user_id},
        )
        return [_from_item(item) for item in resp.get("Items", [])]

    async def update_name(self, session_id: str, name: str, auto_named: bool) -> None:
        now = datetime.now(UTC).isoformat()
        await self._table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET #n = :name, autoNamed = :an, updatedAt = :ua",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":name": name, ":an": auto_named, ":ua": now},
        )

    async def register_sandbox(self, session_id: str, sandbox_endpoint: str, status: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            await self._table.update_item(
                Key={"sessionId": session_id},
                UpdateExpression="SET sandboxEndpoint = :ep, #s = :status, updatedAt = :ua",
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":ep": sandbox_endpoint,
                    ":status": status,
                    ":expected": "SANDBOX_PROVISIONING",
                    ":ua": now,
                },
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException as exc:
            from session_service.exceptions import SandboxRegistrationError

            raise SandboxRegistrationError(
                "Sandbox registration failed: session status changed concurrently"
            ) from exc

    async def count_active_sandboxes(self, tenant_id: str, user_id: str) -> int:
        active_statuses = {"SANDBOX_PROVISIONING", "SANDBOX_READY", "SESSION_RUNNING"}
        resp = await self._table.query(
            IndexName="tenantId-userId-index",
            KeyConditionExpression="tenantId = :tid",
            FilterExpression=(
                "userId = :uid AND executionEnvironment = :env AND #s IN (:s1, :s2, :s3)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":tid": tenant_id,
                ":uid": user_id,
                ":env": "cloud_sandbox",
                ":s1": "SANDBOX_PROVISIONING",
                ":s2": "SANDBOX_READY",
                ":s3": "SESSION_RUNNING",
            },
            Select="COUNT",
        )
        _ = active_statuses  # used to document intent
        return int(resp.get("Count", 0))

    async def store_expected_task_arn(self, session_id: str, expected_task_arn: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET expectedTaskArn = :arn, updatedAt = :ua",
            ExpressionAttributeValues={":arn": expected_task_arn, ":ua": now},
        )

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
    item["name"] = s.name
    item["autoNamed"] = s.auto_named
    if s.ttl is not None:
        item["ttl"] = s.ttl
    # Sandbox-specific fields (cloud_sandbox sessions only)
    if s.sandbox_endpoint is not None:
        item["sandboxEndpoint"] = s.sandbox_endpoint
    if s.task_arn is not None:
        item["taskArn"] = s.task_arn
    if s.expected_task_arn is not None:
        item["expectedTaskArn"] = s.expected_task_arn
    if s.registration_token is not None:
        item["registrationToken"] = s.registration_token
    if s.network_access is not None:
        item["networkAccess"] = s.network_access
    if s.last_activity_at is not None:
        item["lastActivityAt"] = s.last_activity_at.isoformat()
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
        name=item.get("name", ""),
        auto_named=item.get("autoNamed", True),
        created_at=datetime.fromisoformat(item["createdAt"]),
        expires_at=datetime.fromisoformat(item["expiresAt"]),
        updated_at=datetime.fromisoformat(item["updatedAt"]) if item.get("updatedAt") else None,
        ttl=item.get("ttl"),
        # Sandbox-specific fields
        sandbox_endpoint=item.get("sandboxEndpoint"),
        task_arn=item.get("taskArn"),
        expected_task_arn=item.get("expectedTaskArn"),
        registration_token=item.get("registrationToken"),
        network_access=item.get("networkAccess"),
        last_activity_at=(
            datetime.fromisoformat(item["lastActivityAt"]) if item.get("lastActivityAt") else None
        ),
    )
