"""Tests for sandbox launcher, sandbox service, and sandbox session lifecycle."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from session_service.config import Settings
from session_service.exceptions import (
    ConcurrentSessionLimitError,
    SandboxProvisionError,
)
from session_service.models.domain import SessionDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.services.sandbox_launcher import LaunchResult
from session_service.services.sandbox_service import SandboxService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_sandbox_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "env": "test",
        "sandbox_launcher_type": "local",
        "sandbox_max_concurrent_sessions": 3,
        "session_service_url": "http://localhost:8000",
        "workspace_service_url": "http://localhost:8002",
        "agent_runtime_path": "../cowork-agent-runtime",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_sandbox_session(
    repo: InMemorySessionRepository,
    session_id: str = "sess-sandbox-1",
    status: str = "SANDBOX_PROVISIONING",
    tenant_id: str = "t1",
    user_id: str = "u1",
) -> SessionDomain:
    from datetime import UTC, datetime, timedelta

    session = SessionDomain(
        session_id=session_id,
        workspace_id="ws-1",
        tenant_id=tenant_id,
        user_id=user_id,
        execution_environment="cloud_sandbox",
        status=status,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        ttl=int((datetime.now(UTC) + timedelta(hours=24)).timestamp()),
        network_access="enabled",
    )
    return session


@pytest.fixture
def sandbox_settings() -> Settings:
    return _make_sandbox_settings()


@pytest.fixture
def sandbox_repo() -> InMemorySessionRepository:
    return InMemorySessionRepository()


@pytest.fixture
def mock_launcher() -> AsyncMock:
    launcher = AsyncMock()
    launcher.launch = AsyncMock(
        return_value=LaunchResult(task_id="local:12345", endpoint_hint="http://localhost:9000")
    )
    launcher.stop = AsyncMock()
    launcher.is_healthy = AsyncMock(return_value=True)
    return launcher


@pytest.fixture
def sandbox_service(
    mock_launcher: AsyncMock,
    sandbox_repo: InMemorySessionRepository,
    sandbox_settings: Settings,
) -> SandboxService:
    return SandboxService(mock_launcher, sandbox_repo, sandbox_settings)


# ---------------------------------------------------------------------------
# SandboxService.provision_sandbox
# ---------------------------------------------------------------------------


class TestProvisionSandbox:
    @pytest.mark.unit
    async def test_provision_success(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo)
        await sandbox_repo.create(session)

        task_id = await sandbox_service.provision_sandbox(session)

        assert task_id == "local:12345"
        mock_launcher.launch.assert_called_once()

        # expected_task_arn should be stored on session
        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.expected_task_arn == "local:12345"

    @pytest.mark.unit
    async def test_provision_launch_failure_transitions_to_failed(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo)
        await sandbox_repo.create(session)

        mock_launcher.launch.side_effect = SandboxProvisionError("ECS RunTask failed")

        with pytest.raises(SandboxProvisionError):
            await sandbox_service.provision_sandbox(session)

        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SESSION_FAILED"

    @pytest.mark.unit
    async def test_provision_unexpected_error_transitions_to_failed(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo)
        await sandbox_repo.create(session)

        mock_launcher.launch.side_effect = RuntimeError("unexpected")

        with pytest.raises(SandboxProvisionError, match="Unexpected launch error"):
            await sandbox_service.provision_sandbox(session)

        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SESSION_FAILED"

    @pytest.mark.unit
    async def test_concurrent_limit_rejects(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        # Create 4 active sandbox sessions (limit is 3)
        for i in range(4):
            s = _make_sandbox_session(
                sandbox_repo,
                session_id=f"sess-active-{i}",
                status="SANDBOX_READY",
            )
            await sandbox_repo.create(s)

        new_session = _make_sandbox_session(sandbox_repo, session_id="sess-new")
        await sandbox_repo.create(new_session)

        with pytest.raises(ConcurrentSessionLimitError):
            await sandbox_service.provision_sandbox(new_session)

    @pytest.mark.unit
    async def test_concurrent_limit_allows_under_limit(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        # Create 2 active sessions (limit is 3)
        for i in range(2):
            s = _make_sandbox_session(
                sandbox_repo,
                session_id=f"sess-active-{i}",
                status="SANDBOX_READY",
            )
            await sandbox_repo.create(s)

        new_session = _make_sandbox_session(sandbox_repo, session_id="sess-new")
        await sandbox_repo.create(new_session)

        task_id = await sandbox_service.provision_sandbox(new_session)
        assert task_id == "local:12345"

    @pytest.mark.unit
    async def test_concurrent_limit_ignores_terminated_sessions(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        # Terminated sessions should not count toward limit
        for i in range(5):
            s = _make_sandbox_session(
                sandbox_repo,
                session_id=f"sess-term-{i}",
                status="SANDBOX_TERMINATED",
            )
            await sandbox_repo.create(s)

        new_session = _make_sandbox_session(sandbox_repo, session_id="sess-new")
        await sandbox_repo.create(new_session)

        task_id = await sandbox_service.provision_sandbox(new_session)
        assert task_id == "local:12345"


# ---------------------------------------------------------------------------
# SandboxService.terminate_sandbox
# ---------------------------------------------------------------------------


class TestTerminateSandbox:
    @pytest.mark.unit
    async def test_terminate_success(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_READY")
        session.expected_task_arn = "local:12345"
        await sandbox_repo.create(session)

        await sandbox_service.terminate_sandbox(session)

        mock_launcher.stop.assert_called_once_with("local:12345")
        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_TERMINATED"

    @pytest.mark.unit
    async def test_terminate_stop_failure_still_transitions(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_READY")
        session.expected_task_arn = "local:12345"
        await sandbox_repo.create(session)

        mock_launcher.stop.side_effect = RuntimeError("stop failed")

        # Should not raise — best-effort
        await sandbox_service.terminate_sandbox(session)

        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_TERMINATED"

    @pytest.mark.unit
    async def test_terminate_no_task_id(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
    ) -> None:
        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_READY")
        await sandbox_repo.create(session)

        await sandbox_service.terminate_sandbox(session)

        mock_launcher.stop.assert_not_called()
        updated = await sandbox_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_TERMINATED"


# ---------------------------------------------------------------------------
# EcsSandboxLauncher
# ---------------------------------------------------------------------------


class TestEcsSandboxLauncher:
    @pytest.mark.unit
    async def test_launch_success(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
            ecs_subnets=["subnet-1"],
            ecs_security_groups=["sg-1"],
        )

        mock_ecs = AsyncMock()
        mock_ecs.run_task = AsyncMock(
            return_value={
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/abc"}],
                "failures": [],
            }
        )

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        result = await launcher.launch("sess-1", {"SESSION_ID": "sess-1"})

        assert result.task_id == "arn:aws:ecs:us-east-1:123:task/abc"
        assert result.endpoint_hint == ""
        mock_ecs.run_task.assert_called_once()

    @pytest.mark.unit
    async def test_launch_no_tasks_returned(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.run_task = AsyncMock(
            return_value={
                "tasks": [],
                "failures": [{"reason": "capacity exceeded"}],
            }
        )

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        with pytest.raises(SandboxProvisionError, match="capacity exceeded"):
            await launcher.launch("sess-1", {})

    @pytest.mark.unit
    async def test_launch_client_error(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.run_task = AsyncMock(side_effect=Exception("AccessDenied"))

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        with pytest.raises(SandboxProvisionError, match="ECS RunTask failed"):
            await launcher.launch("sess-1", {})

    @pytest.mark.unit
    async def test_launch_throttle_error(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.run_task = AsyncMock(side_effect=Exception("Throttling"))

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        with pytest.raises(SandboxProvisionError, match="ECS RunTask throttled"):
            await launcher.launch("sess-1", {})

    @pytest.mark.unit
    async def test_stop_success(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.stop_task = AsyncMock(return_value={})

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        await launcher.stop("arn:aws:ecs:us-east-1:123:task/abc")

        mock_ecs.stop_task.assert_called_once()

    @pytest.mark.unit
    async def test_is_healthy_running(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.describe_tasks = AsyncMock(return_value={"tasks": [{"lastStatus": "RUNNING"}]})

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        assert await launcher.is_healthy("arn:task/abc") is True

    @pytest.mark.unit
    async def test_is_healthy_stopped(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.describe_tasks = AsyncMock(return_value={"tasks": [{"lastStatus": "STOPPED"}]})

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        assert await launcher.is_healthy("arn:task/abc") is False


# ---------------------------------------------------------------------------
# LocalSandboxLauncher
# ---------------------------------------------------------------------------


class TestLocalSandboxLauncher:
    @pytest.mark.unit
    async def test_launch_success(self) -> None:
        from session_service.clients.local_launcher import LocalSandboxLauncher

        settings = _make_sandbox_settings(agent_runtime_path="/var/tmp/fake-runtime")  # noqa: S108
        launcher = LocalSandboxLauncher(settings)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.stdout = None
        mock_proc.stderr = None

        popen_path = "session_service.clients.local_launcher.subprocess.Popen"
        port_path = "session_service.clients.local_launcher._find_free_port"
        with patch(popen_path, return_value=mock_proc), patch(port_path, return_value=9999):
            result = await launcher.launch("sess-1", {"SESSION_ID": "sess-1"})

        assert result.task_id == "local:42"
        assert result.endpoint_hint == "http://localhost:9999"

    @pytest.mark.unit
    async def test_launch_spawn_failure(self) -> None:
        from session_service.clients.local_launcher import LocalSandboxLauncher

        settings = _make_sandbox_settings(agent_runtime_path="/nonexistent")
        launcher = LocalSandboxLauncher(settings)

        popen_path = "session_service.clients.local_launcher.subprocess.Popen"
        port_path = "session_service.clients.local_launcher._find_free_port"
        with (
            patch(popen_path, side_effect=FileNotFoundError("not found")),
            patch(port_path, return_value=9999),
            pytest.raises(SandboxProvisionError, match="Failed to spawn"),
        ):
            await launcher.launch("sess-1", {})

    @pytest.mark.unit
    async def test_stop_terminates_process(self) -> None:
        from session_service.clients.local_launcher import (
            LocalSandboxLauncher,
            _processes,
        )

        settings = _make_sandbox_settings()
        launcher = LocalSandboxLauncher(settings)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.stdout = None
        mock_proc.stderr = None

        task_id = "local:999"
        _processes[task_id] = mock_proc

        await launcher.stop(task_id)

        mock_proc.terminate.assert_called_once()
        assert task_id not in _processes

    @pytest.mark.unit
    async def test_is_healthy_alive(self) -> None:
        from session_service.clients.local_launcher import (
            LocalSandboxLauncher,
            _processes,
        )

        settings = _make_sandbox_settings()
        launcher = LocalSandboxLauncher(settings)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running

        task_id = "local:111"
        _processes[task_id] = mock_proc

        assert await launcher.is_healthy(task_id) is True

        # Cleanup
        del _processes[task_id]

    @pytest.mark.unit
    async def test_is_healthy_dead(self) -> None:
        from session_service.clients.local_launcher import (
            LocalSandboxLauncher,
            _processes,
        )

        settings = _make_sandbox_settings()
        launcher = LocalSandboxLauncher(settings)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # exited

        task_id = "local:222"
        _processes[task_id] = mock_proc

        assert await launcher.is_healthy(task_id) is False

        # Cleanup
        del _processes[task_id]

    @pytest.mark.unit
    async def test_is_healthy_missing(self) -> None:
        from session_service.clients.local_launcher import LocalSandboxLauncher

        settings = _make_sandbox_settings()
        launcher = LocalSandboxLauncher(settings)

        assert await launcher.is_healthy("local:nonexistent") is False


# ---------------------------------------------------------------------------
# Session creation with sandbox provisioning (integration with SessionService)
# ---------------------------------------------------------------------------


class TestSandboxSessionCreation:
    @pytest.mark.unit
    async def test_create_sandbox_session_provisions(
        self,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.services.session_service import SessionService

        sandbox_svc = SandboxService(mock_launcher, sandbox_repo, sandbox_settings)

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_policy.get_policy_bundle = AsyncMock(return_value={"test": "bundle"})

        mock_workspace = AsyncMock(spec=WorkspaceClient)
        mock_workspace.create_workspace = AsyncMock(
            return_value={"workspaceId": "ws-cloud-1", "workspaceScope": "cloud"}
        )

        session_svc = SessionService(
            sandbox_repo, mock_policy, mock_workspace, sandbox_settings, sandbox_svc
        )

        result = await session_svc.create_session(
            tenant_id="t1",
            user_id="u1",
            execution_environment="cloud_sandbox",
            client_info={},
            supported_capabilities=["File.Read"],
        )

        assert result["status"] == "SANDBOX_PROVISIONING"
        mock_launcher.launch.assert_called_once()

        # Session should have expected_task_arn stored
        session = await sandbox_repo.get(result["sessionId"])
        assert session is not None
        assert session.expected_task_arn == "local:12345"

    @pytest.mark.unit
    async def test_create_sandbox_session_launch_failure(
        self,
        sandbox_repo: InMemorySessionRepository,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.services.session_service import SessionService

        failing_launcher = AsyncMock()
        failing_launcher.launch = AsyncMock(side_effect=SandboxProvisionError("launch failed"))

        sandbox_svc = SandboxService(failing_launcher, sandbox_repo, sandbox_settings)

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_workspace = AsyncMock(spec=WorkspaceClient)
        mock_workspace.create_workspace = AsyncMock(return_value={"workspaceId": "ws-cloud-1"})

        session_svc = SessionService(
            sandbox_repo, mock_policy, mock_workspace, sandbox_settings, sandbox_svc
        )

        with pytest.raises(SandboxProvisionError):
            await session_svc.create_session(
                tenant_id="t1",
                user_id="u1",
                execution_environment="cloud_sandbox",
                client_info={},
                supported_capabilities=[],
            )

        # Session should exist in FAILED state
        sessions = await sandbox_repo.list_by_tenant_user("t1", "u1")
        assert len(sessions) == 1
        assert sessions[0].status == "SESSION_FAILED"

    @pytest.mark.unit
    async def test_desktop_session_no_sandbox_provisioning(
        self,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
        sandbox_settings: Settings,
    ) -> None:
        """Desktop sessions should NOT trigger sandbox provisioning."""
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.services.session_service import SessionService

        sandbox_svc = SandboxService(mock_launcher, sandbox_repo, sandbox_settings)

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_policy.get_policy_bundle = AsyncMock(return_value={"test": "bundle"})

        mock_workspace = AsyncMock(spec=WorkspaceClient)
        mock_workspace.create_workspace = AsyncMock(return_value={"workspaceId": "ws-1"})

        session_svc = SessionService(
            sandbox_repo, mock_policy, mock_workspace, sandbox_settings, sandbox_svc
        )

        result = await session_svc.create_session(
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            client_info={
                "desktopAppVersion": "1.0.0",
                "localAgentHostVersion": "1.0.0",
            },
            supported_capabilities=["File.Read"],
        )

        mock_launcher.launch.assert_not_called()
        assert "status" not in result  # Desktop sessions don't include status


# ---------------------------------------------------------------------------
# Repository: count_active_sandboxes
# ---------------------------------------------------------------------------


class TestCountActiveSandboxes:
    @pytest.mark.unit
    async def test_counts_provisioning_ready_running(
        self,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        for status in ("SANDBOX_PROVISIONING", "SANDBOX_READY", "SESSION_RUNNING"):
            s = _make_sandbox_session(sandbox_repo, session_id=f"sess-{status}", status=status)
            await sandbox_repo.create(s)

        count = await sandbox_repo.count_active_sandboxes("t1", "u1")
        assert count == 3

    @pytest.mark.unit
    async def test_excludes_terminated_and_failed(
        self,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        for status in ("SANDBOX_TERMINATED", "SESSION_FAILED", "SESSION_CANCELLED"):
            s = _make_sandbox_session(sandbox_repo, session_id=f"sess-{status}", status=status)
            await sandbox_repo.create(s)

        count = await sandbox_repo.count_active_sandboxes("t1", "u1")
        assert count == 0

    @pytest.mark.unit
    async def test_scoped_to_user(
        self,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        s1 = _make_sandbox_session(
            sandbox_repo, session_id="sess-u1", user_id="u1", status="SANDBOX_READY"
        )
        s2 = _make_sandbox_session(
            sandbox_repo, session_id="sess-u2", user_id="u2", status="SANDBOX_READY"
        )
        await sandbox_repo.create(s1)
        await sandbox_repo.create(s2)

        assert await sandbox_repo.count_active_sandboxes("t1", "u1") == 1
        assert await sandbox_repo.count_active_sandboxes("t1", "u2") == 1

    @pytest.mark.unit
    async def test_excludes_desktop_sessions(
        self,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        """Desktop sessions with SESSION_RUNNING should not be counted."""
        from datetime import UTC, datetime, timedelta

        desktop = SessionDomain(
            session_id="sess-desktop",
            workspace_id="ws-1",
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            status="SESSION_RUNNING",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        await sandbox_repo.create(desktop)

        count = await sandbox_repo.count_active_sandboxes("t1", "u1")
        assert count == 0


# ---------------------------------------------------------------------------
# Concurrent limit boundary test
# ---------------------------------------------------------------------------


class TestConcurrentLimitBoundary:
    @pytest.mark.unit
    async def test_exactly_at_limit_rejects(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        """With limit=3: 3 existing + 1 new (already in DB) = 4 > 3 → reject."""
        for i in range(3):
            s = _make_sandbox_session(
                sandbox_repo, session_id=f"sess-active-{i}", status="SANDBOX_READY"
            )
            await sandbox_repo.create(s)

        new_session = _make_sandbox_session(sandbox_repo, session_id="sess-new")
        await sandbox_repo.create(new_session)
        # count is now 4 (3 READY + 1 PROVISIONING), limit is 3 → 4 > 3 → reject
        with pytest.raises(ConcurrentSessionLimitError):
            await sandbox_service.provision_sandbox(new_session)

    @pytest.mark.unit
    async def test_one_below_limit_allows(
        self,
        sandbox_service: SandboxService,
        sandbox_repo: InMemorySessionRepository,
    ) -> None:
        """With limit=3: 2 existing + 1 new = 3, and 3 > 3 is False → allow."""
        for i in range(2):
            s = _make_sandbox_session(
                sandbox_repo, session_id=f"sess-active-{i}", status="SANDBOX_READY"
            )
            await sandbox_repo.create(s)

        new_session = _make_sandbox_session(sandbox_repo, session_id="sess-new")
        await sandbox_repo.create(new_session)
        # count is now 3 (2 READY + 1 PROVISIONING), limit is 3 → 3 > 3 is False → allow
        task_id = await sandbox_service.provision_sandbox(new_session)
        assert task_id == "local:12345"


# ---------------------------------------------------------------------------
# Registration token validation
# ---------------------------------------------------------------------------


class TestRegistrationToken:
    @pytest.mark.unit
    async def test_sandbox_session_gets_registration_token(
        self,
        sandbox_repo: InMemorySessionRepository,
        mock_launcher: AsyncMock,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.services.session_service import SessionService

        sandbox_svc = SandboxService(mock_launcher, sandbox_repo, sandbox_settings)
        mock_policy = AsyncMock(spec=PolicyClient)
        mock_policy.get_policy_bundle = AsyncMock(return_value={"test": "bundle"})
        mock_workspace = AsyncMock(spec=WorkspaceClient)
        mock_workspace.create_workspace = AsyncMock(return_value={"workspaceId": "ws-cloud-1"})

        session_svc = SessionService(
            sandbox_repo, mock_policy, mock_workspace, sandbox_settings, sandbox_svc
        )

        result = await session_svc.create_session(
            tenant_id="t1",
            user_id="u1",
            execution_environment="cloud_sandbox",
            client_info={},
            supported_capabilities=[],
        )

        session = await sandbox_repo.get(result["sessionId"])
        assert session is not None
        assert session.registration_token is not None
        assert len(session.registration_token) == 36  # UUID format

        # Verify token was passed as env var to launcher
        call_args = mock_launcher.launch.call_args
        env_vars = call_args[0][1]
        assert env_vars["REGISTRATION_TOKEN"] == session.registration_token

    @pytest.mark.unit
    async def test_register_with_valid_token(
        self,
        sandbox_repo: InMemorySessionRepository,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.services.session_service import SessionService

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_policy.get_policy_bundle = AsyncMock(return_value={"test": "bundle"})
        mock_workspace = AsyncMock(spec=WorkspaceClient)

        session_svc = SessionService(sandbox_repo, mock_policy, mock_workspace, sandbox_settings)

        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_PROVISIONING")
        session.registration_token = "my-token-123"
        session.expected_task_arn = "arn:task/abc"
        await sandbox_repo.create(session)

        result = await session_svc.register_sandbox(
            session.session_id,
            sandbox_endpoint="http://10.0.1.42:8080",
            task_arn="arn:task/abc",
            registration_token="my-token-123",
        )
        assert result["sessionId"] == session.session_id

    @pytest.mark.unit
    async def test_register_with_wrong_token_rejects(
        self,
        sandbox_repo: InMemorySessionRepository,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.exceptions import SandboxRegistrationError
        from session_service.services.session_service import SessionService

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_workspace = AsyncMock(spec=WorkspaceClient)

        session_svc = SessionService(sandbox_repo, mock_policy, mock_workspace, sandbox_settings)

        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_PROVISIONING")
        session.registration_token = "correct-token"
        session.expected_task_arn = "arn:task/abc"
        await sandbox_repo.create(session)

        with pytest.raises(SandboxRegistrationError, match="token mismatch"):
            await session_svc.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:task/abc",
                registration_token="wrong-token",
            )

    @pytest.mark.unit
    async def test_register_missing_token_rejects(
        self,
        sandbox_repo: InMemorySessionRepository,
        sandbox_settings: Settings,
    ) -> None:
        from session_service.clients.policy_client import PolicyClient
        from session_service.clients.workspace_client import WorkspaceClient
        from session_service.exceptions import SandboxRegistrationError
        from session_service.services.session_service import SessionService

        mock_policy = AsyncMock(spec=PolicyClient)
        mock_workspace = AsyncMock(spec=WorkspaceClient)

        session_svc = SessionService(sandbox_repo, mock_policy, mock_workspace, sandbox_settings)

        session = _make_sandbox_session(sandbox_repo, status="SANDBOX_PROVISIONING")
        session.registration_token = "required-token"
        await sandbox_repo.create(session)

        with pytest.raises(SandboxRegistrationError, match="token is required"):
            await session_svc.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:task/abc",
                registration_token=None,
            )


# ---------------------------------------------------------------------------
# ECS launcher: additional error paths
# ---------------------------------------------------------------------------


class TestEcsLauncherErrorPaths:
    @pytest.mark.unit
    async def test_launch_empty_failures_list(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.run_task = AsyncMock(return_value={"tasks": [], "failures": []})

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        with pytest.raises(SandboxProvisionError, match="no tasks started"):
            await launcher.launch("sess-1", {})

    @pytest.mark.unit
    async def test_stop_failure(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.stop_task = AsyncMock(side_effect=Exception("access denied"))

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        with pytest.raises(SandboxProvisionError, match="ECS StopTask failed"):
            await launcher.stop("arn:task/abc")

    @pytest.mark.unit
    async def test_is_healthy_on_error(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.describe_tasks = AsyncMock(side_effect=Exception("network error"))

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        assert await launcher.is_healthy("arn:task/abc") is False

    @pytest.mark.unit
    async def test_is_healthy_no_tasks(self) -> None:
        from session_service.clients.ecs_launcher import EcsSandboxLauncher

        settings = _make_sandbox_settings(
            sandbox_launcher_type="ecs",
            ecs_cluster="test-cluster",
            ecs_task_definition="test-task-def",
        )

        mock_ecs = AsyncMock()
        mock_ecs.describe_tasks = AsyncMock(return_value={"tasks": []})

        launcher = EcsSandboxLauncher(mock_ecs, settings)
        assert await launcher.is_healthy("arn:task/abc") is False
