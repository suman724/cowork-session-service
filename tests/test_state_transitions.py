"""Tests for session state machine transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from session_service.models.domain import SessionDomain


@pytest.fixture
def session() -> SessionDomain:
    now = datetime.now(UTC)
    return SessionDomain(
        session_id="s1",
        workspace_id="ws-1",
        tenant_id="t1",
        user_id="u1",
        execution_environment="desktop",
        status="SESSION_CREATED",
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )


@pytest.mark.unit
class TestStateTransitions:
    def test_created_to_running(self, session: SessionDomain) -> None:
        assert session.can_transition_to("SESSION_RUNNING") is True

    def test_created_to_cancelled(self, session: SessionDomain) -> None:
        assert session.can_transition_to("SESSION_CANCELLED") is True

    def test_created_to_completed_invalid(self, session: SessionDomain) -> None:
        assert session.can_transition_to("SESSION_COMPLETED") is False

    def test_running_to_waiting_for_llm(self, session: SessionDomain) -> None:
        session.status = "SESSION_RUNNING"
        assert session.can_transition_to("WAITING_FOR_LLM") is True

    def test_running_to_completed(self, session: SessionDomain) -> None:
        session.status = "SESSION_RUNNING"
        assert session.can_transition_to("SESSION_COMPLETED") is True

    def test_completed_is_terminal(self, session: SessionDomain) -> None:
        session.status = "SESSION_COMPLETED"
        assert session.can_transition_to("SESSION_RUNNING") is False
        assert session.can_transition_to("SESSION_CANCELLED") is False

    def test_cancelled_is_terminal(self, session: SessionDomain) -> None:
        session.status = "SESSION_CANCELLED"
        assert session.can_transition_to("SESSION_RUNNING") is False

    def test_failed_is_terminal(self, session: SessionDomain) -> None:
        session.status = "SESSION_FAILED"
        assert session.can_transition_to("SESSION_RUNNING") is False

    def test_paused_to_running(self, session: SessionDomain) -> None:
        session.status = "SESSION_PAUSED"
        assert session.can_transition_to("SESSION_RUNNING") is True
        assert session.can_transition_to("SESSION_CANCELLED") is True
