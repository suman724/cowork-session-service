"""Tests for SessionService handshake, resume, cancel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from session_service.exceptions import (
    ConflictError,
    IncompatibleError,
    SessionNotFoundError,
    ValidationError,
)
from session_service.repositories.memory import InMemorySessionRepository
from session_service.services.session_service import SessionService
from tests.conftest import make_service_kwargs


@pytest.mark.unit
class TestCreateSession:
    async def test_creates_session_successfully(self, session_service: SessionService) -> None:
        result = await session_service.create_session(**make_service_kwargs())
        assert result["sessionId"]
        assert result["workspaceId"] == "ws-1"
        assert result["compatibilityStatus"] == "compatible"
        assert result["policyBundle"] is not None

    async def test_creates_with_workspace_hint(
        self,
        session_service: SessionService,
        mock_workspace_client: Any,
    ) -> None:
        req = make_service_kwargs(workspace_hint={"localPaths": ["/home/user/project"]})
        await session_service.create_session(**req)
        mock_workspace_client.create_workspace.assert_called_once()
        call_kwargs = mock_workspace_client.create_workspace.call_args[1]
        assert call_kwargs["workspace_scope"] == "local"
        assert call_kwargs["local_path"] == "/home/user/project"

    async def test_creates_with_workspace_id_hint(
        self,
        session_service: SessionService,
        mock_workspace_client: Any,
    ) -> None:
        """When workspaceId is provided, reuse existing workspace (no create_workspace call)."""
        req = make_service_kwargs(workspace_hint={"workspaceId": "ws-existing"})
        result = await session_service.create_session(**req)
        mock_workspace_client.create_workspace.assert_not_called()
        assert result["workspaceId"] == "ws-existing"

    async def test_incompatible_desktop_version(self, session_service: SessionService) -> None:
        req = make_service_kwargs(
            client_info={
                "desktopAppVersion": "0.0.1",
                "localAgentHostVersion": "1.0.0",
                "osFamily": "macOS",
            }
        )
        result = await session_service.create_session(**req)
        assert result["compatibilityStatus"] == "incompatible"
        assert result.get("policyBundle") is None

    async def test_incompatible_agent_version(self, session_service: SessionService) -> None:
        req = make_service_kwargs(
            client_info={
                "desktopAppVersion": "1.0.0",
                "localAgentHostVersion": "0.0.1",
                "osFamily": "macOS",
            }
        )
        result = await session_service.create_session(**req)
        assert result["compatibilityStatus"] == "incompatible"

    async def test_incompatible_no_capabilities(self, session_service: SessionService) -> None:
        req = make_service_kwargs(supported_capabilities=[])
        result = await session_service.create_session(**req)
        assert result["compatibilityStatus"] == "incompatible"

    async def test_validation_error_missing_tenant(self, session_service: SessionService) -> None:
        req = make_service_kwargs(tenant_id="")
        with pytest.raises(ValidationError, match="required"):
            await session_service.create_session(**req)

    async def test_feature_flags_present(self, session_service: SessionService) -> None:
        result = await session_service.create_session(**make_service_kwargs())
        assert "featureFlags" in result
        assert result["featureFlags"]["approvalUiEnabled"] is False

    async def test_create_returns_empty_name(self, session_service: SessionService) -> None:
        result = await session_service.create_session(**make_service_kwargs())
        assert result["name"] == ""


@pytest.mark.unit
class TestResumeSession:
    async def test_resume_active_session(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        result = await session_service.resume_session(session_id)
        assert result["sessionId"] == session_id
        assert result["policyBundle"] is not None

    async def test_resume_not_found(self, session_service: SessionService) -> None:
        with pytest.raises(SessionNotFoundError):
            await session_service.resume_session("nonexistent")

    async def test_resume_incompatible_session_blocked(
        self, session_service: SessionService
    ) -> None:
        """Resuming a session created with incompatible versions must not bypass the gate."""
        req = make_service_kwargs(
            client_info={
                "desktopAppVersion": "0.0.1",
                "localAgentHostVersion": "1.0.0",
                "osFamily": "macOS",
            }
        )
        create_result = await session_service.create_session(**req)
        assert create_result["compatibilityStatus"] == "incompatible"
        session_id = create_result["sessionId"]

        with pytest.raises(IncompatibleError):
            await session_service.resume_session(session_id)

    async def test_resume_expired_session_fails(
        self, session_service: SessionService, session_repo: InMemorySessionRepository
    ) -> None:
        """Resuming an expired session must fail even if status is not terminal."""
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        # Force session to be expired by setting expires_at in the past
        stored = session_repo._sessions[session_id]
        stored.expires_at = datetime.now(UTC) - timedelta(hours=1)

        with pytest.raises(ConflictError, match="expired"):
            await session_service.resume_session(session_id)

        # Verify session was marked as failed
        result = await session_service.get_session(session_id)
        assert result["status"] == "SESSION_FAILED"

    async def test_resume_completed_session(
        self, session_service: SessionService, session_repo: InMemorySessionRepository
    ) -> None:
        """Resuming a completed session should succeed (Continue Conversation)."""
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]
        await session_repo.update_status(session_id, "SESSION_COMPLETED")

        result = await session_service.resume_session(session_id)
        assert result["sessionId"] == session_id
        assert result["workspaceId"] == "ws-1"
        assert result["policyBundle"] is not None

        # Verify session is back to RUNNING
        session = await session_service.get_session(session_id)
        assert session["status"] == "SESSION_RUNNING"

    async def test_resume_cancelled_session_fails(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]
        await session_service.cancel_session(session_id)

        with pytest.raises(ConflictError, match="Cannot resume"):
            await session_service.resume_session(session_id)


@pytest.mark.unit
class TestCancelSession:
    async def test_cancel_active_session(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        await session_service.cancel_session(session_id)
        result = await session_service.get_session(session_id)
        assert result["status"] == "SESSION_CANCELLED"

    async def test_cancel_not_found(self, session_service: SessionService) -> None:
        with pytest.raises(SessionNotFoundError):
            await session_service.cancel_session("nonexistent")

    async def test_cancel_already_cancelled(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]
        await session_service.cancel_session(session_id)

        with pytest.raises(ConflictError, match="Cannot cancel"):
            await session_service.cancel_session(session_id)


@pytest.mark.unit
class TestGetSession:
    async def test_get_session(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        result = await session_service.get_session(session_id)
        assert result["sessionId"] == session_id
        assert result["status"] == "SESSION_RUNNING"

    async def test_get_session_includes_name(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        result = await session_service.get_session(session_id)
        assert result["name"] == ""
        assert result["autoNamed"] is True

    async def test_update_session_name(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]

        result = await session_service.update_session_name(session_id, "My Project", False)
        assert result["name"] == "My Project"
        assert result["autoNamed"] is False

        get_result = await session_service.get_session(session_id)
        assert get_result["name"] == "My Project"

    async def test_get_not_found(self, session_service: SessionService) -> None:
        with pytest.raises(SessionNotFoundError):
            await session_service.get_session("nonexistent")

    async def test_get_session_includes_session_type(self, session_service: SessionService) -> None:
        create_result = await session_service.create_session(**make_service_kwargs())
        session_id = create_result["sessionId"]
        result = await session_service.get_session(session_id)
        assert result["sessionType"] == "solo"


@pytest.mark.unit
class TestTeamSessionFields:
    async def test_create_lead_session(self, session_service: SessionService) -> None:
        result = await session_service.create_session(
            **make_service_kwargs(),
            session_type="lead",
            team_id="team-abc",
        )
        session_id = result["sessionId"]
        session = await session_service.get_session(session_id)
        assert session["sessionType"] == "lead"
        assert session["teamId"] == "team-abc"

    async def test_create_teammate_session(self, session_service: SessionService) -> None:
        # Create lead first
        lead_result = await session_service.create_session(
            **make_service_kwargs(),
            session_type="lead",
            team_id="team-abc",
        )
        lead_id = lead_result["sessionId"]

        # Create teammate referencing lead
        result = await session_service.create_session(
            **make_service_kwargs(),
            session_type="teammate",
            team_id="team-abc",
            parent_session_id=lead_id,
        )
        session_id = result["sessionId"]
        session = await session_service.get_session(session_id)
        assert session["sessionType"] == "teammate"
        assert session["teamId"] == "team-abc"
        assert session["parentSessionId"] == lead_id

    async def test_solo_session_omits_team_fields(self, session_service: SessionService) -> None:
        result = await session_service.create_session(**make_service_kwargs())
        session_id = result["sessionId"]
        session = await session_service.get_session(session_id)
        assert session["sessionType"] == "solo"
        assert "teamId" not in session
        assert "parentSessionId" not in session

    async def test_list_by_team(
        self,
        session_service: SessionService,
        session_repo: InMemorySessionRepository,
    ) -> None:
        # Create two sessions in the same team
        r1 = await session_service.create_session(
            **make_service_kwargs(), session_type="lead", team_id="team-xyz"
        )
        r2 = await session_service.create_session(
            **make_service_kwargs(),
            session_type="teammate",
            team_id="team-xyz",
            parent_session_id=r1["sessionId"],
        )
        # Create one solo session (different team)
        await session_service.create_session(**make_service_kwargs())

        team_sessions = await session_repo.list_by_team("team-xyz")
        assert len(team_sessions) == 2
        ids = {s.session_id for s in team_sessions}
        assert r1["sessionId"] in ids
        assert r2["sessionId"] in ids
