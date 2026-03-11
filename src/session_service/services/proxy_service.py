"""Proxy service: resolve sandbox endpoints, validate requests, track activity."""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import UTC, datetime

import structlog

from session_service.exceptions import (
    ForbiddenError,
    SandboxUnavailableError,
    SessionInactiveError,
    SessionNotFoundError,
)
from session_service.models.domain import SANDBOX_ACTIVE_STATUSES
from session_service.repositories.base import SessionRepository

logger = structlog.get_logger()

# Max cached sessions to prevent unbounded growth
_MAX_CACHE_ENTRIES = 10_000


class ProxyService:
    """Resolves sandbox endpoints with caching and tracks activity."""

    def __init__(
        self,
        repo: SessionRepository,
        *,
        endpoint_cache_ttl: float = 30.0,
        activity_batch_seconds: float = 60.0,
    ) -> None:
        self._repo = repo
        self._cache_ttl = endpoint_cache_ttl
        self._activity_batch_seconds = activity_batch_seconds
        # LRU cache: session_id -> (endpoint, user_id, fetched_at_monotonic)
        self._endpoint_cache: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        # Activity batching: session_id -> last_write_monotonic (bounded)
        self._last_activity_write: OrderedDict[str, float] = OrderedDict()

    async def resolve_sandbox(self, session_id: str, user_id: str) -> str:
        """Look up sandbox endpoint, validate ownership and state. Returns endpoint URL."""
        now_mono = time.monotonic()

        # Check cache — also validate user_id from cached data
        cached = self._endpoint_cache.get(session_id)
        if cached and (now_mono - cached[2]) < self._cache_ttl:
            if cached[1] != user_id:
                raise ForbiddenError("Not the session owner")
            self._endpoint_cache.move_to_end(session_id)
            return cached[0]

        session = await self._repo.get(session_id)
        if not session:
            raise SessionNotFoundError(session_id)

        if session.user_id != user_id:
            raise ForbiddenError("Not the session owner")

        if session.status not in SANDBOX_ACTIVE_STATUSES:
            raise SessionInactiveError(session.status)

        if not session.sandbox_endpoint:
            raise SandboxUnavailableError("Sandbox endpoint not available")

        # Cache the endpoint with user_id for ownership checks on cache hit
        self._endpoint_cache[session_id] = (
            session.sandbox_endpoint,
            session.user_id,
            now_mono,
        )
        self._endpoint_cache.move_to_end(session_id)
        # Evict oldest entries if over limit
        while len(self._endpoint_cache) > _MAX_CACHE_ENTRIES:
            self._endpoint_cache.popitem(last=False)

        return session.sandbox_endpoint

    async def update_activity(self, session_id: str) -> None:
        """Batch-update lastActivityAt — writes at most once per batch window."""
        now_mono = time.monotonic()
        last_write = self._last_activity_write.get(session_id, 0.0)
        if (now_mono - last_write) < self._activity_batch_seconds:
            return

        now = datetime.now(UTC)
        try:
            await self._repo.update_last_activity(session_id, now)
        except Exception:
            logger.warning("activity_update_failed", session_id=session_id)
            return
        self._last_activity_write[session_id] = now_mono
        self._last_activity_write.move_to_end(session_id)
        while len(self._last_activity_write) > _MAX_CACHE_ENTRIES:
            self._last_activity_write.popitem(last=False)
        logger.debug("activity_updated", session_id=session_id)

    def invalidate_cache(self, session_id: str) -> None:
        """Remove cached endpoint for a session (e.g. on connection error)."""
        self._endpoint_cache.pop(session_id, None)
