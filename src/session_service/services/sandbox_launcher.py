"""SandboxLauncher protocol — abstraction over container/process launching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LaunchResult:
    """Result of a sandbox launch operation."""

    task_id: str  # ECS task ARN or "local:{pid}"
    endpoint_hint: str  # e.g. "http://10.0.1.42:8080" or "http://localhost:9123"


class SandboxLauncher(Protocol):
    """Interface for launching sandbox containers/processes."""

    async def launch(self, session_id: str, env_vars: dict[str, str]) -> LaunchResult:
        """Start a sandbox. Returns task identifier and endpoint hint."""
        ...

    async def stop(self, task_id: str) -> None:
        """Stop a running sandbox."""
        ...

    async def is_healthy(self, task_id: str) -> bool:
        """Check if a sandbox is still running."""
        ...
