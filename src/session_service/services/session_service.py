"""Core session business logic: create, resume, cancel, get."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from session_service.clients.policy_client import PolicyClient
from session_service.clients.workspace_client import WorkspaceClient
from session_service.config import Settings
from session_service.exceptions import (
    ConflictError,
    DownstreamError,
    IncompatibleError,
    SessionNotFoundError,
    ValidationError,
)
from session_service.models.domain import SessionDomain
from session_service.repositories.base import SessionRepository
from session_service.services.compatibility import check_compatibility

logger = structlog.get_logger()


class SessionService:
    def __init__(
        self,
        repo: SessionRepository,
        policy_client: PolicyClient,
        workspace_client: WorkspaceClient,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._policy_client = policy_client
        self._workspace_client = workspace_client
        self._settings = settings

    async def create_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        execution_environment: str,
        workspace_hint: dict[str, Any] | None = None,
        client_info: dict[str, Any],
        supported_capabilities: list[str],
    ) -> dict[str, Any]:
        """Create a new session — the handshake endpoint."""
        if not tenant_id.strip() or not user_id.strip():
            raise ValidationError("tenantId and userId are required")

        desktop_version = client_info.get("desktopAppVersion", "0.0.0")
        agent_version = client_info.get("localAgentHostVersion", "0.0.0")

        # Compatibility check
        is_compatible, reason = check_compatibility(
            desktop_app_version=desktop_version,
            agent_host_version=agent_version,
            supported_capabilities=supported_capabilities,
            settings=self._settings,
        )

        # Resolve workspace
        # Priority: explicit workspaceId > localPaths > general (new workspace)
        workspace_id: str
        if workspace_hint and workspace_hint.get("workspaceId"):
            # Reuse an existing workspace (e.g., "Continue Conversation")
            workspace_id = workspace_hint["workspaceId"]
        else:
            local_path = None
            workspace_scope = "general"
            if workspace_hint and workspace_hint.get("localPaths"):
                local_paths = workspace_hint["localPaths"]
                if not isinstance(local_paths, list) or not local_paths:
                    raise ValidationError("workspaceHint.localPaths must be a non-empty list")
                local_path = local_paths[0]
                workspace_scope = "local"

            ws_result = await self._workspace_client.create_workspace(
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_scope=workspace_scope,
                local_path=local_path,
            )
            try:
                workspace_id = ws_result["workspaceId"]
            except KeyError as exc:
                raise DownstreamError(
                    "WorkspaceService", "missing workspaceId in response"
                ) from exc

        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self._settings.session_expiry_hours)
        session_id = str(uuid.uuid4())

        # Fetch policy bundle before persisting session so a downstream failure
        # does not leave an orphaned SESSION_CREATED record
        policy_bundle = None
        initial_status = "SESSION_CREATED"
        if is_compatible:
            policy_bundle = await self._policy_client.get_policy_bundle(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                capabilities=supported_capabilities,
            )
            initial_status = "SESSION_RUNNING"

        session = SessionDomain(
            session_id=session_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            execution_environment=execution_environment,
            status=initial_status,
            desktop_app_version=desktop_version,
            agent_host_version=agent_version,
            supported_capabilities=supported_capabilities,
            created_at=now,
            expires_at=expires_at,
            ttl=int(expires_at.timestamp()),
        )
        await self._repo.create(session)

        logger.info(
            "session_created",
            session_id=session_id,
            workspace_id=workspace_id,
            compatible=is_compatible,
        )

        result: dict[str, Any] = {
            "sessionId": session_id,
            "workspaceId": workspace_id,
            "compatibilityStatus": "compatible" if is_compatible else "incompatible",
        }
        if policy_bundle:
            result["policyBundle"] = policy_bundle
        if not is_compatible:
            result["incompatibilityReason"] = reason
        result["featureFlags"] = {
            "approvalUiEnabled": False,
            "mcpEnabled": False,
        }
        return result

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        """Resume an existing session — re-fetch policy bundle."""
        session = await self._repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        if session.status != "SESSION_RUNNING" and not session.can_transition_to("SESSION_RUNNING"):
            raise ConflictError(f"Cannot resume session in {session.status} state")

        # Check session expiry
        if datetime.now(UTC) >= session.expires_at:
            await self._repo.update_status(session_id, "SESSION_FAILED")
            raise ConflictError("Session has expired")

        # Re-run compatibility check to prevent bypassing the gate
        is_compatible, reason = check_compatibility(
            desktop_app_version=session.desktop_app_version or "0.0.0",
            agent_host_version=session.agent_host_version or "0.0.0",
            supported_capabilities=session.supported_capabilities,
            settings=self._settings,
        )
        if not is_compatible:
            raise IncompatibleError(reason or "Client version incompatible")

        # Re-fetch policy
        policy_bundle = await self._policy_client.get_policy_bundle(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            session_id=session_id,
            capabilities=session.supported_capabilities,
        )

        # Extend expiry on resume so the session doesn't expire mid-task
        new_expires = datetime.now(UTC) + timedelta(hours=self._settings.session_expiry_hours)
        await self._repo.update_status(session_id, "SESSION_RUNNING")
        await self._repo.update_expiry(session_id, new_expires)

        return {
            "sessionId": session_id,
            "workspaceId": session.workspace_id,
            "compatibilityStatus": "compatible",
            "policyBundle": policy_bundle,
        }

    async def cancel_session(self, session_id: str) -> None:
        """Cancel a session."""
        session = await self._repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        if not session.can_transition_to("SESSION_CANCELLED"):
            raise ConflictError(f"Cannot cancel session in {session.status} state")

        await self._repo.update_status(session_id, "SESSION_CANCELLED")
        logger.info("session_cancelled", session_id=session_id)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session metadata."""
        session = await self._repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        return {
            "sessionId": session.session_id,
            "workspaceId": session.workspace_id,
            "tenantId": session.tenant_id,
            "userId": session.user_id,
            "executionEnvironment": session.execution_environment,
            "status": session.status,
            "createdAt": session.created_at.isoformat(),
            "expiresAt": session.expires_at.isoformat(),
        }
