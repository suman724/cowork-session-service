"""Version and capability compatibility checking."""

from __future__ import annotations

from packaging.version import Version

from session_service.config import Settings


def check_compatibility(
    *,
    desktop_app_version: str,
    agent_host_version: str,
    supported_capabilities: list[str],
    settings: Settings,
) -> tuple[bool, str]:
    """Check client version compatibility.

    Returns (is_compatible, reason).
    """
    try:
        if Version(desktop_app_version) < Version(settings.min_desktop_app_version):
            return False, (
                f"Desktop App version {desktop_app_version} "
                f"< minimum {settings.min_desktop_app_version}"
            )
    except Exception:
        return False, f"Invalid desktop app version: {desktop_app_version}"

    try:
        if Version(agent_host_version) < Version(settings.min_agent_host_version):
            return False, (
                f"Agent Host version {agent_host_version} "
                f"< minimum {settings.min_agent_host_version}"
            )
    except Exception:
        return False, f"Invalid agent host version: {agent_host_version}"

    if not supported_capabilities:
        return False, "Client must support at least one capability"

    return True, ""
