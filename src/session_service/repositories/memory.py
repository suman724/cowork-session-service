"""In-memory session repository for unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from session_service.models.domain import SessionDomain


class InMemorySessionRepository:
    """Dict-backed session repository for testing."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionDomain] = {}

    async def create(self, session: SessionDomain) -> None:
        self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> SessionDomain | None:
        return self._sessions.get(session_id)

    async def update_status(self, session_id: str, status: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.status = status  # type: ignore[assignment]
            session.updated_at = datetime.now(UTC)

    async def update_expiry(self, session_id: str, expires_at: datetime) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.expires_at = expires_at
            session.ttl = int(expires_at.timestamp())
            session.updated_at = datetime.now(UTC)

    async def list_by_tenant_user(self, tenant_id: str, user_id: str) -> list[SessionDomain]:
        return [
            s for s in self._sessions.values() if s.tenant_id == tenant_id and s.user_id == user_id
        ]

    async def update_name(self, session_id: str, name: str, auto_named: bool) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.name = name
            session.auto_named = auto_named
            session.updated_at = datetime.now(UTC)

    async def register_sandbox(self, session_id: str, sandbox_endpoint: str, status: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.sandbox_endpoint = sandbox_endpoint
            session.status = status  # type: ignore[assignment]
            session.updated_at = datetime.now(UTC)

    async def count_active_sandboxes(self, tenant_id: str, user_id: str) -> int:
        active_statuses = {"SANDBOX_PROVISIONING", "SANDBOX_READY", "SESSION_RUNNING"}
        return sum(
            1
            for s in self._sessions.values()
            if s.tenant_id == tenant_id
            and s.user_id == user_id
            and s.execution_environment == "cloud_sandbox"
            and s.status in active_statuses
        )

    async def store_expected_task_arn(self, session_id: str, expected_task_arn: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.expected_task_arn = expected_task_arn
            session.updated_at = datetime.now(UTC)

    async def update_last_activity(self, session_id: str, last_activity_at: datetime) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.last_activity_at = last_activity_at
            session.updated_at = datetime.now(UTC)

    async def list_sandbox_sessions_by_status(self, statuses: set[str]) -> list[SessionDomain]:
        return [
            s
            for s in self._sessions.values()
            if s.execution_environment == "cloud_sandbox" and s.status in statuses
        ]

    async def conditional_update_status(
        self, session_id: str, new_status: str, expected_status: str
    ) -> bool:
        session = self._sessions.get(session_id)
        if session and session.status == expected_status:
            session.status = new_status  # type: ignore[assignment]
            session.updated_at = datetime.now(UTC)
            return True
        return False

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
