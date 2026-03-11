"""ECS Fargate sandbox launcher for production."""

from __future__ import annotations

from typing import Any

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from session_service.config import Settings
from session_service.exceptions import SandboxProvisionError
from session_service.services.sandbox_launcher import LaunchResult

logger = structlog.get_logger()

# Retry on transient AWS errors (throttling, service unavailable)
_ecs_retry = retry(
    retry=retry_if_exception_type(SandboxProvisionError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=10, jitter=2),
    reraise=True,
)


def _is_throttle_or_transient(exc: Exception) -> bool:
    """Check if an exception is a transient AWS error worth retrying."""
    msg = str(exc).lower()
    return any(
        keyword in msg
        for keyword in ("throttl", "rate exceeded", "service unavailable", "too many requests")
    )


class EcsSandboxLauncher:
    """Launches sandbox containers via ECS RunTask."""

    def __init__(self, ecs_client: Any, settings: Settings) -> None:
        self._ecs = ecs_client
        self._settings = settings

    @_ecs_retry
    async def launch(self, session_id: str, env_vars: dict[str, str]) -> LaunchResult:
        """Call ECS RunTask with session-specific overrides. Retries on throttling."""
        container_overrides = {
            "containerOverrides": [
                {
                    "name": "agent-runtime",
                    "environment": [{"name": k, "value": v} for k, v in env_vars.items()],
                }
            ]
        }

        network_config = {
            "awsvpcConfiguration": {
                "subnets": self._settings.ecs_subnets,
                "securityGroups": self._settings.ecs_security_groups,
                "assignPublicIp": "DISABLED",
            }
        }

        try:
            resp = await self._ecs.run_task(
                cluster=self._settings.ecs_cluster,
                taskDefinition=self._settings.ecs_task_definition,
                launchType="FARGATE",
                overrides=container_overrides,
                networkConfiguration=network_config,
                count=1,
            )
        except Exception as exc:
            logger.error("ecs_run_task_failed", session_id=session_id, error=str(exc))
            if _is_throttle_or_transient(exc):
                raise SandboxProvisionError(f"ECS RunTask throttled: {exc}") from exc
            raise SandboxProvisionError(f"ECS RunTask failed: {exc}") from exc

        tasks = resp.get("tasks", [])
        if not tasks:
            failures = resp.get("failures", [])
            reason = failures[0].get("reason", "unknown") if failures else "no tasks started"
            logger.error("ecs_run_task_no_tasks", session_id=session_id, reason=reason)
            raise SandboxProvisionError(f"ECS RunTask returned no tasks: {reason}")

        task_arn = tasks[0]["taskArn"]
        logger.info("ecs_sandbox_launched", session_id=session_id, task_arn=task_arn)

        # Endpoint is not known until the container registers; return empty hint
        return LaunchResult(task_id=task_arn, endpoint_hint="")

    async def stop(self, task_id: str) -> None:
        """Stop an ECS task."""
        try:
            await self._ecs.stop_task(
                cluster=self._settings.ecs_cluster,
                task=task_id,
                reason="Session terminated",
            )
            logger.info("ecs_sandbox_stopped", task_arn=task_id)
        except Exception as exc:
            logger.error("ecs_stop_task_failed", task_arn=task_id, error=str(exc))
            raise SandboxProvisionError(f"ECS StopTask failed: {exc}") from exc

    async def is_healthy(self, task_id: str) -> bool:
        """Check if an ECS task is still running."""
        try:
            resp = await self._ecs.describe_tasks(
                cluster=self._settings.ecs_cluster,
                tasks=[task_id],
            )
            tasks = resp.get("tasks", [])
            if not tasks:
                return False
            last_status = tasks[0].get("lastStatus", "")
            return last_status in ("RUNNING", "PROVISIONING", "PENDING")
        except Exception as exc:
            logger.error("ecs_describe_tasks_failed", task_arn=task_id, error=str(exc))
            return False
