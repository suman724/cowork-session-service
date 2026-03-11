"""Background lifecycle manager for sandbox sessions.

Periodically checks for:
- Idle sandbox sessions (no running task + no user activity beyond timeout)
- Provisioning timeouts (stuck in SANDBOX_PROVISIONING beyond threshold)
- Max duration enforcement (session exceeded absolute time limit)
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog

from session_service.config import Settings
from session_service.models.domain import SessionDomain
from session_service.repositories.base import SessionRepository, TaskRepository
from session_service.services.sandbox_service import SandboxService

logger = structlog.get_logger()

# Sandbox statuses that are considered "active" for lifecycle checks
_ACTIVE_STATUSES = {
    "SANDBOX_PROVISIONING",
    "SANDBOX_READY",
    "SESSION_RUNNING",
    "WAITING_FOR_LLM",
    "WAITING_FOR_TOOL",
    "WAITING_FOR_APPROVAL",
    "SESSION_PAUSED",
}


class SandboxLifecycleManager:
    """Periodically checks sandbox sessions for idle timeout, provisioning
    timeout, and max duration enforcement.

    Safe for multiple instances — uses conditional DynamoDB updates to prevent
    double-termination.
    """

    def __init__(
        self,
        session_repo: SessionRepository,
        task_repo: TaskRepository,
        sandbox_service: SandboxService,
        settings: Settings,
    ) -> None:
        self._session_repo = session_repo
        self._task_repo = task_repo
        self._sandbox_service = sandbox_service
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background lifecycle check loop."""
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "sandbox_lifecycle_started",
            interval=self._settings.sandbox_lifecycle_check_interval_seconds,
            idle_timeout=self._settings.sandbox_idle_timeout_seconds,
            max_duration=self._settings.sandbox_max_duration_seconds,
            provision_timeout=self._settings.sandbox_provision_timeout_seconds,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("sandbox_lifecycle_stopped")

    async def _run_loop(self) -> None:
        """Main loop: sleep → check → repeat."""
        while True:
            try:
                await asyncio.sleep(self._settings.sandbox_lifecycle_check_interval_seconds)
                await self.run_checks()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sandbox_lifecycle_check_error")

    async def run_checks(self) -> None:
        """Run all lifecycle checks once. Public for testing."""
        sessions = await self._session_repo.list_sandbox_sessions_by_status(_ACTIVE_STATUSES)
        now = datetime.now(UTC)

        for session in sessions:
            try:
                if session.status == "SANDBOX_PROVISIONING":
                    await self._check_provisioning_timeout(session, now)
                else:
                    await self._check_max_duration(session, now)
                    await self._check_idle_timeout(session, now)
            except Exception:
                logger.exception(
                    "sandbox_lifecycle_session_error",
                    session_id=session.session_id,
                    status=session.status,
                )

    async def _check_provisioning_timeout(self, session: SessionDomain, now: datetime) -> None:
        """Fail sessions stuck in SANDBOX_PROVISIONING beyond threshold."""
        elapsed = (now - session.created_at).total_seconds()
        if elapsed <= self._settings.sandbox_provision_timeout_seconds:
            return

        updated = await self._session_repo.conditional_update_status(
            session.session_id, "SESSION_FAILED", "SANDBOX_PROVISIONING"
        )
        if updated:
            logger.warning(
                "sandbox_provisioning_timeout",
                session_id=session.session_id,
                elapsed_seconds=int(elapsed),
                threshold=self._settings.sandbox_provision_timeout_seconds,
            )

    async def _check_max_duration(self, session: SessionDomain, now: datetime) -> None:
        """Terminate sessions that exceeded maximum duration."""
        elapsed = (now - session.created_at).total_seconds()
        if elapsed <= self._settings.sandbox_max_duration_seconds:
            return

        await self._terminate_session(session, "max_duration_exceeded", int(elapsed))

    async def _check_idle_timeout(self, session: SessionDomain, now: datetime) -> None:
        """Terminate idle sessions with no running tasks."""
        # Determine last activity time — use lastActivityAt if available, else createdAt
        last_active = session.last_activity_at or session.created_at
        idle_seconds = (now - last_active).total_seconds()

        if idle_seconds <= self._settings.sandbox_idle_timeout_seconds:
            return

        # Check for running tasks — busy sandbox is never idle
        tasks = await self._task_repo.list_by_session(session.session_id)
        has_running_task = any(t.status == "running" for t in tasks)
        if has_running_task:
            return

        await self._terminate_session(session, "idle_timeout", int(idle_seconds))

    async def _terminate_session(
        self, session: SessionDomain, reason: str, elapsed_seconds: int
    ) -> None:
        """Terminate a sandbox session via SandboxService.

        Uses conditional update to prevent double-termination when multiple
        lifecycle managers run concurrently.
        """
        # Try to claim this termination with a conditional update
        updated = await self._session_repo.conditional_update_status(
            session.session_id, "SANDBOX_TERMINATED", session.status
        )
        if not updated:
            # Another instance already transitioned this session
            return

        # Stop the sandbox container (best-effort, status already updated above)
        try:
            await self._sandbox_service.stop_sandbox_container(session)
        except Exception:
            logger.warning(
                "sandbox_lifecycle_terminate_failed",
                session_id=session.session_id,
                reason=reason,
            )

        logger.info(
            "sandbox_lifecycle_terminated",
            session_id=session.session_id,
            reason=reason,
            elapsed_seconds=elapsed_seconds,
        )
