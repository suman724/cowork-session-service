"""Tests for SandboxLifecycleManager — idle timeout, provisioning timeout,
max duration enforcement, and concurrent-instance safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from session_service.config import Settings
from session_service.models.domain import SessionDomain, TaskDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.repositories.memory_task import InMemoryTaskRepository
from session_service.services.sandbox_lifecycle import SandboxLifecycleManager
from session_service.services.sandbox_service import SandboxService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "env": "test",
        "sandbox_launcher_type": "local",
        "sandbox_max_concurrent_sessions": 5,
        "session_service_url": "http://localhost:8000",
        "workspace_service_url": "http://localhost:8002",
        "agent_runtime_path": "../cowork-agent-runtime",
        # Short timeouts for tests
        "sandbox_idle_timeout_seconds": 60,
        "sandbox_max_duration_seconds": 300,
        "sandbox_provision_timeout_seconds": 30,
        "sandbox_lifecycle_check_interval_seconds": 1,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_session(
    session_id: str = "sess-1",
    status: str = "SESSION_RUNNING",
    created_at: datetime | None = None,
    last_activity_at: datetime | None = None,
) -> SessionDomain:
    now = datetime.now(UTC)
    return SessionDomain(
        session_id=session_id,
        workspace_id="ws-1",
        tenant_id="t1",
        user_id="u1",
        execution_environment="cloud_sandbox",
        status=status,
        created_at=created_at or now,
        expires_at=now + timedelta(hours=24),
        ttl=int((now + timedelta(hours=24)).timestamp()),
        last_activity_at=last_activity_at,
        expected_task_arn="local:1234",
    )


def _make_task(
    session_id: str = "sess-1",
    task_id: str = "task-1",
    status: str = "running",
) -> TaskDomain:
    now = datetime.now(UTC)
    return TaskDomain(
        task_id=task_id,
        session_id=session_id,
        workspace_id="ws-1",
        tenant_id="t1",
        user_id="u1",
        prompt="test",
        status=status,
        created_at=now,
        ttl=int((now + timedelta(hours=24)).timestamp()),
    )


@pytest.fixture
def session_repo() -> InMemorySessionRepository:
    return InMemorySessionRepository()


@pytest.fixture
def task_repo() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def mock_sandbox_service() -> SandboxService:
    svc = AsyncMock(spec=SandboxService)
    svc.stop_sandbox_container = AsyncMock()
    return svc


# ---------------------------------------------------------------------------
# Idle timeout tests
# ---------------------------------------------------------------------------


class TestIdleTimeout:
    @pytest.mark.asyncio
    async def test_idle_session_no_running_task_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session idle beyond timeout with no running tasks → terminated."""
        session = _make_session(
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(minutes=2),  # within max_duration
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_idle_timeout_seconds=60, sandbox_max_duration_seconds=14400)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"
        mock_sandbox_service.stop_sandbox_container.assert_called_once()

    @pytest.mark.asyncio
    async def test_idle_session_with_running_task_not_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session idle beyond timeout but has running task → NOT terminated."""
        session = _make_session(
            status="SESSION_RUNNING",
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)
        await task_repo.create(_make_task(session_id="sess-1", status="running"))

        cfg = _settings(sandbox_idle_timeout_seconds=60)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SESSION_RUNNING"
        mock_sandbox_service.stop_sandbox_container.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_session_with_completed_task_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session idle with only completed tasks → terminated (completed tasks don't protect)."""
        session = _make_session(
            status="SESSION_RUNNING",
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)
        await task_repo.create(_make_task(session_id="sess-1", status="completed"))

        cfg = _settings(sandbox_idle_timeout_seconds=60)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"

    @pytest.mark.asyncio
    async def test_recently_active_session_not_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session with recent activity → NOT terminated."""
        session = _make_session(
            status="SESSION_RUNNING",
            last_activity_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_idle_timeout_seconds=60)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SESSION_RUNNING"
        mock_sandbox_service.stop_sandbox_container.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_uses_created_at_when_no_last_activity(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """No lastActivityAt → uses createdAt for idle calculation."""
        session = _make_session(
            status="SANDBOX_READY",
            created_at=datetime.now(UTC) - timedelta(minutes=5),
            last_activity_at=None,
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_idle_timeout_seconds=60)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"


# ---------------------------------------------------------------------------
# Provisioning timeout tests
# ---------------------------------------------------------------------------


class TestProvisioningTimeout:
    @pytest.mark.asyncio
    async def test_stuck_provisioning_failed(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session stuck in SANDBOX_PROVISIONING beyond threshold → SESSION_FAILED."""
        session = _make_session(
            status="SANDBOX_PROVISIONING",
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_provision_timeout_seconds=30)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SESSION_FAILED"
        # Provisioning timeout does NOT call terminate_sandbox (no container to stop)
        mock_sandbox_service.stop_sandbox_container.assert_not_called()

    @pytest.mark.asyncio
    async def test_recent_provisioning_not_failed(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Recently created SANDBOX_PROVISIONING session → NOT failed."""
        session = _make_session(
            status="SANDBOX_PROVISIONING",
            created_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_provision_timeout_seconds=30)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_PROVISIONING"


# ---------------------------------------------------------------------------
# Max duration tests
# ---------------------------------------------------------------------------


class TestMaxDuration:
    @pytest.mark.asyncio
    async def test_exceeded_max_duration_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session exceeding max duration → terminated."""
        session = _make_session(
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(hours=5),
            last_activity_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_max_duration_seconds=300)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"
        mock_sandbox_service.stop_sandbox_container.assert_called_once()

    @pytest.mark.asyncio
    async def test_within_max_duration_not_terminated(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Session within max duration → NOT terminated."""
        session = _make_session(
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(minutes=1),
            last_activity_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await session_repo.create(session)

        cfg = _settings(sandbox_max_duration_seconds=300)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SESSION_RUNNING"


# ---------------------------------------------------------------------------
# Concurrent instance safety (conditional updates)
# ---------------------------------------------------------------------------


class TestConditionalUpdate:
    @pytest.mark.asyncio
    async def test_conditional_update_conflict(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """If another instance already transitioned the session, no double-termination."""
        session = _make_session(
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(hours=5),
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)

        # Simulate concurrent transition: change status before lifecycle runs
        await session_repo.update_status("sess-1", "SANDBOX_TERMINATED")

        # Test conditional_update_status directly: once status has changed,
        # a second conditional update with the old expected status returns False.
        result = await session_repo.conditional_update_status(
            "sess-1", "SANDBOX_TERMINATED", "SESSION_RUNNING"
        )
        assert result is False  # Status is already SANDBOX_TERMINATED, not SESSION_RUNNING

    @pytest.mark.asyncio
    async def test_conditional_update_success(
        self,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Conditional update succeeds when expected status matches."""
        session = _make_session(status="SESSION_RUNNING")
        await session_repo.create(session)

        result = await session_repo.conditional_update_status(
            "sess-1", "SANDBOX_TERMINATED", "SESSION_RUNNING"
        )
        assert result is True
        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"


# ---------------------------------------------------------------------------
# Lifecycle manager start/stop
# ---------------------------------------------------------------------------


class TestLifecycleManagerStartStop:
    @pytest.mark.asyncio
    async def test_start_and_stop(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Lifecycle manager starts and stops cleanly."""
        cfg = _settings(sandbox_lifecycle_check_interval_seconds=100)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)

        await mgr.start()
        assert mgr._task is not None
        assert not mgr._task.done()

        await mgr.stop()
        assert mgr._task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Stopping a manager that was never started is a no-op."""
        cfg = _settings()
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    @pytest.mark.asyncio
    async def test_single_session_error_does_not_crash_loop(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Error processing one session doesn't prevent checking others."""
        # Create two sessions: one will cause an error, one should be processed
        session1 = _make_session(
            session_id="sess-err",
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(hours=5),
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session2 = _make_session(
            session_id="sess-ok",
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(hours=5),
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session1)
        await session_repo.create(session2)

        # Make conditional_update raise for the first session only
        original_conditional = session_repo.conditional_update_status
        call_count = 0

        async def _flaky_conditional(
            session_id: str, new_status: str, expected_status: str
        ) -> bool:
            nonlocal call_count
            call_count += 1
            if session_id == "sess-err":
                raise RuntimeError("DynamoDB error")
            return await original_conditional(session_id, new_status, expected_status)

        session_repo.conditional_update_status = _flaky_conditional  # type: ignore[assignment]

        cfg = _settings(sandbox_max_duration_seconds=300)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()  # Should not raise

        # Second session should still be processed
        s2 = await session_repo.get("sess-ok")
        assert s2 is not None
        assert s2.status == "SANDBOX_TERMINATED"

    @pytest.mark.asyncio
    async def test_terminate_sandbox_failure_does_not_crash(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """If stop_sandbox_container raises, lifecycle manager continues."""
        session = _make_session(
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC) - timedelta(hours=5),
            last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session_repo.create(session)

        mock_sandbox_service.stop_sandbox_container.side_effect = RuntimeError("connection refused")

        cfg = _settings(sandbox_max_duration_seconds=300)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()  # Should not raise

        # Status was already updated by conditional_update_status before terminate was called
        s = await session_repo.get("sess-1")
        assert s is not None
        assert s.status == "SANDBOX_TERMINATED"


# ---------------------------------------------------------------------------
# Desktop sessions are ignored
# ---------------------------------------------------------------------------


class TestDesktopSessionsIgnored:
    @pytest.mark.asyncio
    async def test_desktop_sessions_not_affected(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Desktop sessions are never touched by lifecycle checks."""
        now = datetime.now(UTC)
        desktop_session = SessionDomain(
            session_id="sess-desktop",
            workspace_id="ws-1",
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            status="SESSION_RUNNING",
            created_at=now - timedelta(hours=24),
            expires_at=now + timedelta(hours=24),
            ttl=int((now + timedelta(hours=24)).timestamp()),
        )
        await session_repo.create(desktop_session)

        cfg = _settings(sandbox_max_duration_seconds=300, sandbox_idle_timeout_seconds=60)
        mgr = SandboxLifecycleManager(session_repo, task_repo, mock_sandbox_service, cfg)
        await mgr.run_checks()

        s = await session_repo.get("sess-desktop")
        assert s is not None
        assert s.status == "SESSION_RUNNING"
        mock_sandbox_service.stop_sandbox_container.assert_not_called()


# ---------------------------------------------------------------------------
# Multiple statuses in _ACTIVE_STATUSES
# ---------------------------------------------------------------------------


class TestMultipleStatuses:
    @pytest.mark.asyncio
    async def test_waiting_states_checked_for_idle(
        self,
        session_repo: InMemorySessionRepository,
        task_repo: InMemoryTaskRepository,
        mock_sandbox_service: SandboxService,
    ) -> None:
        """Sessions in WAITING_FOR_* states are also checked for idle/max duration."""
        for status in ["WAITING_FOR_LLM", "WAITING_FOR_TOOL", "WAITING_FOR_APPROVAL"]:
            repo = InMemorySessionRepository()
            t_repo = InMemoryTaskRepository()
            mock_svc = AsyncMock(spec=SandboxService)
            mock_svc.stop_sandbox_container = AsyncMock()

            session = _make_session(
                session_id=f"sess-{status}",
                status=status,
                created_at=datetime.now(UTC) - timedelta(hours=5),
                last_activity_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            await repo.create(session)

            cfg = _settings(sandbox_max_duration_seconds=300)
            mgr = SandboxLifecycleManager(repo, t_repo, mock_svc, cfg)
            await mgr.run_checks()

            s = await repo.get(f"sess-{status}")
            assert s is not None
            assert s.status == "SANDBOX_TERMINATED", f"Expected SANDBOX_TERMINATED for {status}"
