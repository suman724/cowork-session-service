"""Repository protocol for session data access."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from session_service.models.domain import SessionDomain


class SessionRepository(Protocol):
    """Interface for session persistence."""

    async def create(self, session: SessionDomain) -> None: ...

    async def get(self, session_id: str) -> SessionDomain | None: ...

    async def update_status(self, session_id: str, status: str) -> None: ...

    async def update_expiry(self, session_id: str, expires_at: datetime) -> None: ...

    async def list_by_tenant_user(self, tenant_id: str, user_id: str) -> list[SessionDomain]: ...

    async def delete(self, session_id: str) -> None: ...
