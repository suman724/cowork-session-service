"""DynamoDB task repository.

Table: {env}-tasks
  PK: taskId
  GSI: sessionId-index (PK=sessionId, SK=createdAt)
  TTL attribute: ttl
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from session_service.models.domain import TaskDomain


class DynamoTaskRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def create(self, task: TaskDomain) -> None:
        item = _to_item(task)
        await self._table.put_item(Item=item)

    async def get(self, task_id: str) -> TaskDomain | None:
        resp = await self._table.get_item(Key={"taskId": task_id})
        item = resp.get("Item")
        return _from_item(item) if item else None

    async def update_completion(
        self,
        task_id: str,
        status: str,
        step_count: int,
        completion_reason: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        expr_values: dict[str, Any] = {
            ":status": status,
            ":sc": step_count,
            ":ca": now,
            ":ua": now,
        }
        update_expr = "SET #s = :status, stepCount = :sc, completedAt = :ca, updatedAt = :ua"
        if completion_reason is not None:
            update_expr += ", completionReason = :cr"
            expr_values[":cr"] = completion_reason

        await self._table.update_item(
            Key={"taskId": task_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=expr_values,
        )

    async def list_by_session(self, session_id: str) -> list[TaskDomain]:
        resp = await self._table.query(
            IndexName="sessionId-index",
            KeyConditionExpression="sessionId = :sid",
            ExpressionAttributeValues={":sid": session_id},
        )
        return [_from_item(item) for item in resp.get("Items", [])]


def _to_item(t: TaskDomain) -> dict[str, Any]:
    item: dict[str, Any] = {
        "taskId": t.task_id,
        "sessionId": t.session_id,
        "workspaceId": t.workspace_id,
        "tenantId": t.tenant_id,
        "userId": t.user_id,
        "prompt": t.prompt,
        "status": t.status,
        "stepCount": t.step_count,
        "maxSteps": t.max_steps,
        "createdAt": t.created_at.isoformat(),
        "updatedAt": (t.updated_at or t.created_at).isoformat(),
    }
    if t.completion_reason:
        item["completionReason"] = t.completion_reason
    if t.completed_at:
        item["completedAt"] = t.completed_at.isoformat()
    if t.ttl is not None:
        item["ttl"] = t.ttl
    return item


def _from_item(item: dict[str, Any]) -> TaskDomain:
    return TaskDomain(
        task_id=item["taskId"],
        session_id=item["sessionId"],
        workspace_id=item["workspaceId"],
        tenant_id=item["tenantId"],
        user_id=item["userId"],
        prompt=item["prompt"],
        status=item["status"],
        step_count=item.get("stepCount", 0),
        max_steps=item.get("maxSteps", 50),
        completion_reason=item.get("completionReason"),
        created_at=datetime.fromisoformat(item["createdAt"]),
        completed_at=(
            datetime.fromisoformat(item["completedAt"]) if item.get("completedAt") else None
        ),
        updated_at=(datetime.fromisoformat(item["updatedAt"]) if item.get("updatedAt") else None),
        ttl=item.get("ttl"),
    )
