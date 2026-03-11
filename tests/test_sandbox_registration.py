"""Tests for sandbox self-registration endpoint and service logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from session_service.exceptions import SandboxRegistrationError, SessionNotFoundError
from session_service.models.domain import SessionDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.services.session_service import SessionService
from tests.conftest import make_service_kwargs


def _make_sandbox_session(
    session_id: str = "sess-sandbox-1",
    status: str = "SANDBOX_PROVISIONING",
    expected_task_arn: str | None = "arn:aws:ecs:us-east-1:123:task/cowork/abc123",
    **overrides: Any,
) -> SessionDomain:
    """Create a sandbox session domain object for testing."""
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "session_id": session_id,
        "workspace_id": "ws-1",
        "tenant_id": "t1",
        "user_id": "u1",
        "execution_environment": "cloud_sandbox",
        "status": status,
        "supported_capabilities": ["File.Read", "Shell.Exec"],
        "created_at": now,
        "expires_at": now + timedelta(hours=24),
        "ttl": int((now + timedelta(hours=24)).timestamp()),
        "expected_task_arn": expected_task_arn,
        "network_access": "enabled",
    }
    defaults.update(overrides)
    return SessionDomain(**defaults)


@pytest.mark.unit
class TestRegisterSandboxService:
    """Unit tests for SessionService.register_sandbox()."""

    async def test_register_success(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Registration with valid state and matching task ARN succeeds."""
        session = _make_sandbox_session()
        await session_repo.create(session)

        result = await session_service.register_sandbox(
            session.session_id,
            sandbox_endpoint="http://10.0.1.42:8080",
            task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
        )

        assert result["sessionId"] == session.session_id
        assert result["workspaceId"] == "ws-1"
        assert "policyBundle" in result
        assert "workspaceServiceUrl" in result

        # Verify session was updated
        updated = await session_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_READY"
        assert updated.sandbox_endpoint == "http://10.0.1.42:8080"

    async def test_register_session_not_found(
        self,
        session_service: SessionService,
    ) -> None:
        """Registration for nonexistent session raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            await session_service.register_sandbox(
                "nonexistent",
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            )

    async def test_register_wrong_state(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Registration rejects sessions not in SANDBOX_PROVISIONING state."""
        session = _make_sandbox_session(status="SANDBOX_READY")
        await session_repo.create(session)

        with pytest.raises(SandboxRegistrationError, match="SANDBOX_READY"):
            await session_service.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            )

    async def test_register_task_arn_mismatch(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Registration rejects mismatched task ARN."""
        session = _make_sandbox_session()
        await session_repo.create(session)

        with pytest.raises(SandboxRegistrationError, match="Task ARN mismatch"):
            await session_service.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:aws:ecs:us-east-1:123:task/cowork/WRONG",
            )

        # Verify session was NOT updated
        updated = await session_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_PROVISIONING"
        assert updated.sandbox_endpoint is None

    async def test_register_no_expected_task_arn(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Registration succeeds when no expected_task_arn was stored (local dev)."""
        session = _make_sandbox_session(expected_task_arn=None)
        await session_repo.create(session)

        result = await session_service.register_sandbox(
            session.session_id,
            sandbox_endpoint="http://localhost:9090",
            task_arn="local:12345",
        )

        assert result["sessionId"] == session.session_id
        updated = await session_repo.get(session.session_id)
        assert updated is not None
        assert updated.status == "SANDBOX_READY"

    async def test_register_idempotent_same_data(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Calling register twice — second call fails because state is SANDBOX_READY."""
        session = _make_sandbox_session()
        await session_repo.create(session)

        # First call succeeds
        await session_service.register_sandbox(
            session.session_id,
            sandbox_endpoint="http://10.0.1.42:8080",
            task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
        )

        # Second call rejects (state is now SANDBOX_READY, not SANDBOX_PROVISIONING)
        with pytest.raises(SandboxRegistrationError, match="SANDBOX_READY"):
            await session_service.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            )

    async def test_register_desktop_session_rejected(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Registration rejects desktop sessions (wrong state)."""
        now = datetime.now(UTC)
        session = SessionDomain(
            session_id="sess-desktop",
            workspace_id="ws-1",
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            status="SESSION_RUNNING",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        await session_repo.create(session)

        with pytest.raises(SandboxRegistrationError, match="SESSION_RUNNING"):
            await session_service.register_sandbox(
                session.session_id,
                sandbox_endpoint="http://10.0.1.42:8080",
                task_arn="arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            )


@pytest.mark.unit
class TestCreateSessionSandbox:
    """Unit tests for sandbox-specific session creation."""

    async def test_create_sandbox_session_status(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Cloud sandbox session starts in SANDBOX_PROVISIONING state."""
        result = await session_service.create_session(
            **make_service_kwargs(execution_environment="cloud_sandbox"),
        )

        assert result["status"] == "SANDBOX_PROVISIONING"
        assert "sessionId" in result
        assert "workspaceId" in result

        # Verify session was persisted with correct status
        session = await session_repo.get(result["sessionId"])
        assert session is not None
        assert session.status == "SANDBOX_PROVISIONING"
        assert session.execution_environment == "cloud_sandbox"

    async def test_create_sandbox_session_no_policy_bundle(
        self,
        session_service: SessionService,
    ) -> None:
        """Cloud sandbox session does not include policyBundle in create response."""
        result = await session_service.create_session(
            **make_service_kwargs(execution_environment="cloud_sandbox"),
        )

        # Policy bundle is fetched at registration, not at creation
        assert "policyBundle" not in result

    async def test_create_sandbox_session_network_access(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Cloud sandbox session stores network_access."""
        result = await session_service.create_session(
            **make_service_kwargs(
                execution_environment="cloud_sandbox",
                network_access="disabled",
            ),
        )

        session = await session_repo.get(result["sessionId"])
        assert session is not None
        assert session.network_access == "disabled"

    async def test_create_desktop_session_unchanged(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """Desktop session creation is not affected by sandbox changes."""
        result = await session_service.create_session(**make_service_kwargs())

        assert "status" not in result  # Desktop sessions don't include status in response
        assert result["compatibilityStatus"] == "compatible"
        assert "policyBundle" in result

        session = await session_repo.get(result["sessionId"])
        assert session is not None
        assert session.status == "SESSION_RUNNING"
        assert session.execution_environment == "desktop"
        assert session.sandbox_endpoint is None
        assert session.network_access is None

    async def test_create_sandbox_session_cloud_workspace(
        self,
        session_service: SessionService,
        mock_workspace_client: Any,
    ) -> None:
        """Cloud sandbox session creates a 'cloud' workspace."""
        await session_service.create_session(
            **make_service_kwargs(execution_environment="cloud_sandbox"),
        )

        # Verify workspace was created with cloud scope
        mock_workspace_client.create_workspace.assert_called_once()
        call_kwargs = mock_workspace_client.create_workspace.call_args[1]
        assert call_kwargs["workspace_scope"] == "cloud"

    async def test_create_sandbox_skips_compatibility_check(
        self,
        session_service: SessionService,
    ) -> None:
        """Cloud sandbox session skips desktop compatibility check."""
        # No client_info versions — would fail compatibility for desktop
        result = await session_service.create_session(
            **make_service_kwargs(
                execution_environment="cloud_sandbox",
                client_info={},
                supported_capabilities=[],
            ),
        )

        # Should succeed — sandbox sessions skip compatibility
        assert result["compatibilityStatus"] == "compatible"


@pytest.mark.unit
class TestSandboxStateTransitions:
    """Unit tests for sandbox-specific state machine transitions."""

    def test_provisioning_to_ready(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_PROVISIONING")
        assert session.can_transition_to("SANDBOX_READY") is True

    def test_provisioning_to_failed(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_PROVISIONING")
        assert session.can_transition_to("SESSION_FAILED") is True

    def test_provisioning_to_cancelled(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_PROVISIONING")
        assert session.can_transition_to("SESSION_CANCELLED") is True

    def test_provisioning_to_running_invalid(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_PROVISIONING")
        assert session.can_transition_to("SESSION_RUNNING") is False

    def test_ready_to_running(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_READY")
        assert session.can_transition_to("SESSION_RUNNING") is True

    def test_ready_to_terminated(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_READY")
        assert session.can_transition_to("SANDBOX_TERMINATED") is True

    def test_ready_to_failed(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_READY")
        assert session.can_transition_to("SESSION_FAILED") is True

    def test_terminated_is_terminal(self) -> None:
        session = _make_sandbox_session(status="SANDBOX_TERMINATED")
        assert session.can_transition_to("SESSION_RUNNING") is False
        assert session.can_transition_to("SANDBOX_READY") is False
        assert session.can_transition_to("SANDBOX_PROVISIONING") is False

    def test_desktop_session_cannot_enter_sandbox_states(self) -> None:
        now = datetime.now(UTC)
        session = SessionDomain(
            session_id="s1",
            workspace_id="ws-1",
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            status="SESSION_CREATED",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        # Desktop sessions start in SESSION_CREATED, which cannot transition to sandbox states
        assert session.can_transition_to("SANDBOX_PROVISIONING") is False
        assert session.can_transition_to("SANDBOX_READY") is False


@pytest.mark.unit
class TestSandboxRegistrationRoute:
    """HTTP endpoint tests for POST /sessions/{sessionId}/register."""

    async def test_register_endpoint_success(
        self,
        client: Any,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """POST /sessions/{id}/register returns registration response."""
        session = _make_sandbox_session()
        await session_repo.create(session)

        resp = await client.post(
            f"/sessions/{session.session_id}/register",
            json={
                "sandboxEndpoint": "http://10.0.1.42:8080",
                "taskArn": "arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["sessionId"] == session.session_id
        assert data["workspaceId"] == "ws-1"

    async def test_register_endpoint_wrong_state(
        self,
        client: Any,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """POST /sessions/{id}/register returns 409 for wrong state."""
        session = _make_sandbox_session(status="SANDBOX_READY")
        await session_repo.create(session)

        resp = await client.post(
            f"/sessions/{session.session_id}/register",
            json={
                "sandboxEndpoint": "http://10.0.1.42:8080",
                "taskArn": "arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            },
        )

        assert resp.status_code == 409
        assert resp.json()["code"] == "SANDBOX_REGISTRATION_FAILED"

    async def test_register_endpoint_not_found(self, client: Any) -> None:
        """POST /sessions/{id}/register returns 404 for unknown session."""
        resp = await client.post(
            "/sessions/nonexistent/register",
            json={
                "sandboxEndpoint": "http://10.0.1.42:8080",
                "taskArn": "arn:aws:ecs:us-east-1:123:task/cowork/abc123",
            },
        )

        assert resp.status_code == 404

    async def test_register_endpoint_validation_error(self, client: Any) -> None:
        """POST /sessions/{id}/register returns 422 for invalid body."""
        resp = await client.post(
            "/sessions/sess-1/register",
            json={"sandboxEndpoint": ""},  # Missing taskArn, empty endpoint
        )

        assert resp.status_code == 422

    async def test_register_endpoint_task_arn_mismatch(
        self,
        client: Any,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """POST /sessions/{id}/register returns 409 for task ARN mismatch."""
        session = _make_sandbox_session()
        await session_repo.create(session)

        resp = await client.post(
            f"/sessions/{session.session_id}/register",
            json={
                "sandboxEndpoint": "http://10.0.1.42:8080",
                "taskArn": "arn:aws:ecs:us-east-1:123:task/cowork/WRONG",
            },
        )

        assert resp.status_code == 409
        assert "mismatch" in resp.json()["message"].lower()


@pytest.mark.unit
class TestGetSessionSandboxFields:
    """Verify GET /sessions/{id} includes sandbox fields when present."""

    async def test_get_session_with_sandbox_fields(
        self,
        client: Any,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """GET /sessions/{id} returns sandbox fields for cloud_sandbox sessions."""
        session = _make_sandbox_session(status="SANDBOX_READY")
        session.sandbox_endpoint = "http://10.0.1.42:8080"
        await session_repo.create(session)

        resp = await client.get(f"/sessions/{session.session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sandboxEndpoint"] == "http://10.0.1.42:8080"
        assert data["networkAccess"] == "enabled"

    async def test_get_session_desktop_no_sandbox_fields(
        self,
        client: Any,
        session_repo: InMemorySessionRepository,
    ) -> None:
        """GET /sessions/{id} omits sandbox fields for desktop sessions."""
        now = datetime.now(UTC)
        session = SessionDomain(
            session_id="sess-desktop",
            workspace_id="ws-1",
            tenant_id="t1",
            user_id="u1",
            execution_environment="desktop",
            status="SESSION_RUNNING",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        await session_repo.create(session)

        resp = await client.get(f"/sessions/{session.session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "sandboxEndpoint" not in data
        assert "networkAccess" not in data
        assert "lastActivityAt" not in data
