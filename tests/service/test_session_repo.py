"""Service-tier tests for DynamoDB session repository.

Requires: LocalStack (port 4566) or DynamoDB Local (set AWS_ENDPOINT_URL)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from session_service.models.domain import SessionDomain
from session_service.repositories.dynamo import DynamoSessionRepository


def _make_session(
    session_id: str = "sess-1",
    tenant_id: str = "t1",
    user_id: str = "u1",
    **overrides: object,
) -> SessionDomain:
    now = datetime.now(UTC)
    defaults = {
        "session_id": session_id,
        "workspace_id": "ws-1",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "execution_environment": "desktop",
        "status": "SESSION_CREATED",
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "ttl": int((now + timedelta(hours=1)).timestamp()),
    }
    defaults.update(overrides)
    return SessionDomain(**defaults)  # type: ignore[arg-type]


@pytest.mark.service
@pytest.mark.asyncio
class TestSessionRepoCRUD:
    async def test_create_and_get_session(self, session_repo: DynamoSessionRepository) -> None:
        """Create a session and retrieve it by ID."""
        session = _make_session()
        await session_repo.create(session)

        result = await session_repo.get("sess-1")
        assert result is not None
        assert result.session_id == "sess-1"
        assert result.tenant_id == "t1"
        assert result.user_id == "u1"
        assert result.status == "SESSION_CREATED"
        assert result.workspace_id == "ws-1"

    async def test_get_nonexistent_returns_none(
        self, session_repo: DynamoSessionRepository
    ) -> None:
        """Getting a non-existent session returns None."""
        result = await session_repo.get("nonexistent")
        assert result is None

    async def test_update_session_status(self, session_repo: DynamoSessionRepository) -> None:
        """Update status of an existing session."""
        session = _make_session()
        await session_repo.create(session)

        await session_repo.update_status("sess-1", "SESSION_RUNNING")

        result = await session_repo.get("sess-1")
        assert result is not None
        assert result.status == "SESSION_RUNNING"

    async def test_delete_session(self, session_repo: DynamoSessionRepository) -> None:
        """Delete a session."""
        session = _make_session()
        await session_repo.create(session)

        await session_repo.delete("sess-1")

        result = await session_repo.get("sess-1")
        assert result is None

    async def test_ttl_field_populated(self, session_repo: DynamoSessionRepository) -> None:
        """Verify DynamoDB TTL attribute is set."""
        session = _make_session()
        await session_repo.create(session)

        result = await session_repo.get("sess-1")
        assert result is not None
        assert result.ttl is not None
        assert result.ttl > 0


@pytest.mark.service
@pytest.mark.asyncio
class TestSessionRepoGSI:
    async def test_list_by_tenant_user(self, session_repo: DynamoSessionRepository) -> None:
        """List sessions for a tenant-user pair via GSI."""
        now = datetime.now(UTC)
        for i in range(3):
            session = _make_session(
                session_id=f"sess-{i}",
                created_at=now + timedelta(seconds=i),
                expires_at=now + timedelta(hours=1),
            )
            await session_repo.create(session)

        # Different user — should not appear
        other = _make_session(session_id="sess-other", user_id="u2")
        await session_repo.create(other)

        results = await session_repo.list_by_tenant_user("t1", "u1")
        assert len(results) == 3
        session_ids = {s.session_id for s in results}
        assert session_ids == {"sess-0", "sess-1", "sess-2"}
