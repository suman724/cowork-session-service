"""Tests for version and capability compatibility checking."""

from __future__ import annotations

import pytest

from session_service.config import Settings
from session_service.services.compatibility import check_compatibility


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        min_desktop_app_version="0.1.0",
        min_agent_host_version="0.1.0",
    )


@pytest.mark.unit
class TestCompatibility:
    def test_compatible(self, settings: Settings) -> None:
        ok, reason = check_compatibility(
            desktop_app_version="1.0.0",
            agent_host_version="1.0.0",
            supported_capabilities=["File.Read"],
            settings=settings,
        )
        assert ok is True
        assert reason == ""

    def test_desktop_too_old(self, settings: Settings) -> None:
        ok, reason = check_compatibility(
            desktop_app_version="0.0.9",
            agent_host_version="1.0.0",
            supported_capabilities=["File.Read"],
            settings=settings,
        )
        assert ok is False
        assert "Desktop App" in reason

    def test_agent_too_old(self, settings: Settings) -> None:
        ok, reason = check_compatibility(
            desktop_app_version="1.0.0",
            agent_host_version="0.0.1",
            supported_capabilities=["File.Read"],
            settings=settings,
        )
        assert ok is False
        assert "Agent Host" in reason

    def test_no_capabilities(self, settings: Settings) -> None:
        ok, reason = check_compatibility(
            desktop_app_version="1.0.0",
            agent_host_version="1.0.0",
            supported_capabilities=[],
            settings=settings,
        )
        assert ok is False
        assert "capability" in reason.lower()

    def test_invalid_desktop_version(self, settings: Settings) -> None:
        ok, reason = check_compatibility(
            desktop_app_version="not-a-version",
            agent_host_version="1.0.0",
            supported_capabilities=["File.Read"],
            settings=settings,
        )
        assert ok is False
        assert "Invalid" in reason

    def test_exact_minimum_version(self, settings: Settings) -> None:
        ok, _ = check_compatibility(
            desktop_app_version="0.1.0",
            agent_host_version="0.1.0",
            supported_capabilities=["File.Read"],
            settings=settings,
        )
        assert ok is True
