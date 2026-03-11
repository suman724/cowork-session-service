"""Sandbox orchestration: provision and terminate sandboxes."""

from __future__ import annotations

import structlog

from session_service.config import Settings
from session_service.exceptions import ConcurrentSessionLimitError, SandboxProvisionError
from session_service.models.domain import SessionDomain
from session_service.repositories.base import SessionRepository
from session_service.services.proxy_service import ProxyService
from session_service.services.sandbox_launcher import SandboxLauncher

logger = structlog.get_logger()


class SandboxService:
    """Orchestrates sandbox provisioning and teardown."""

    def __init__(
        self,
        launcher: SandboxLauncher,
        repo: SessionRepository,
        settings: Settings,
        proxy_service: ProxyService | None = None,
    ) -> None:
        self._launcher = launcher
        self._repo = repo
        self._settings = settings
        self._proxy_service = proxy_service

    async def provision_sandbox(self, session: SessionDomain) -> str:
        """Launch a sandbox for a session. Returns the task identifier.

        Steps:
        1. Check concurrent session limit
        2. Call launcher.launch() — registration_token is already on the
           session record (set at creation), so fast-starting containers
           can validate immediately without waiting for expected_task_arn
        3. Store expected_task_arn on session record (post-launch, since
           ECS task ARN is only known after RunTask returns)

        Raises ConcurrentSessionLimitError or SandboxProvisionError on failure.
        """
        # Check concurrent session limit.
        # Uses > (not >=) because the new session is already persisted in
        # SANDBOX_PROVISIONING state before this method is called, so
        # count_active_sandboxes includes it. With limit=N and N-1 existing
        # sessions, count = N (existing + this one), and N > N is false —
        # correctly allowing the Nth session.
        active_count = await self._repo.count_active_sandboxes(session.tenant_id, session.user_id)
        if active_count > self._settings.sandbox_max_concurrent_sessions:
            raise ConcurrentSessionLimitError(
                f"User has {active_count} active sandbox sessions "
                f"(limit: {self._settings.sandbox_max_concurrent_sessions})"
            )

        # Build env vars for the sandbox
        env_vars = {
            "SESSION_ID": session.session_id,
            "SESSION_SERVICE_URL": self._settings.session_service_url,
            "WORKSPACE_SERVICE_URL": self._settings.workspace_service_url,
        }
        # Registration token was generated pre-launch and stored on the session
        # record, so the container can present it at registration time with no
        # race condition (unlike expected_task_arn which is stored post-launch).
        if session.registration_token:
            env_vars["REGISTRATION_TOKEN"] = session.registration_token
        if self._settings.aws_endpoint_url:
            env_vars["AWS_ENDPOINT_URL"] = self._settings.aws_endpoint_url

        try:
            result = await self._launcher.launch(session.session_id, env_vars)
        except SandboxProvisionError:
            await self._fail_session(session.session_id)
            raise
        except Exception as exc:
            await self._fail_session(session.session_id)
            raise SandboxProvisionError(f"Unexpected launch error: {exc}") from exc

        # Store expected_task_arn so registration can validate it
        await self._repo.store_expected_task_arn(session.session_id, result.task_id)

        logger.info(
            "sandbox_provisioned",
            session_id=session.session_id,
            task_id=result.task_id,
            endpoint_hint=result.endpoint_hint,
        )

        return result.task_id

    async def terminate_sandbox(self, session: SessionDomain) -> None:
        """Terminate a sandbox: stop launcher, update session status.

        Best-effort — if shutdown of the sandbox fails, we still transition
        the session to SANDBOX_TERMINATED.
        """
        task_id = session.expected_task_arn or session.task_arn
        if task_id:
            try:
                await self._launcher.stop(task_id)
            except Exception as exc:
                logger.warning(
                    "sandbox_stop_failed",
                    session_id=session.session_id,
                    task_id=task_id,
                    error=str(exc),
                )

        await self._repo.update_status(session.session_id, "SANDBOX_TERMINATED")
        if self._proxy_service:
            self._proxy_service.invalidate_cache(session.session_id)
        logger.info("sandbox_terminated", session_id=session.session_id)

    async def _fail_session(self, session_id: str) -> None:
        """Best-effort transition to SESSION_FAILED — retry once on failure."""
        try:
            await self._repo.update_status(session_id, "SESSION_FAILED")
        except Exception:
            logger.warning("fail_session_retry", session_id=session_id)
            try:
                await self._repo.update_status(session_id, "SESSION_FAILED")
            except Exception:
                logger.error(
                    "fail_session_stuck",
                    session_id=session_id,
                )
