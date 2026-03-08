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

    async def list_by_team(self, team_id: str) -> list[SessionDomain]:
        return [s for s in self._sessions.values() if s.team_id == team_id]

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
