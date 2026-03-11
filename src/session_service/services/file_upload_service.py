"""Unified file upload: persist to Workspace Service (S3), then sync to sandbox."""

from __future__ import annotations

import json

import httpx
import structlog

from session_service.exceptions import (
    DownstreamError,
    ForbiddenError,
    SessionInactiveError,
    SessionNotFoundError,
    ValidationError,
    validate_file_path,
)
from session_service.models.domain import SANDBOX_ACTIVE_STATUSES, TERMINAL_STATUSES
from session_service.models.responses import UploadFileResponse
from session_service.repositories.base import SessionRepository

logger = structlog.get_logger()


class FileUploadService:
    """Two-phase file upload: S3 persist via Workspace Service + sandbox sync."""

    def __init__(
        self,
        repo: SessionRepository,
        workspace_http: httpx.AsyncClient,
        sandbox_http: httpx.AsyncClient,
        *,
        sync_timeout: float = 10.0,
    ) -> None:
        self._repo = repo
        self._workspace_http = workspace_http
        self._sandbox_http = sandbox_http
        self._sync_timeout = sync_timeout

    async def upload_file(
        self,
        session_id: str,
        user_id: str,
        file_path: str,
        file_content: bytes,
        content_type: str,
        filename: str,
    ) -> UploadFileResponse:
        """Upload a file: persist to S3 via Workspace Service, then sync to sandbox.

        Args:
            session_id: Target session.
            user_id: Caller user ID (for ownership check).
            file_path: Relative path within workspace (e.g. "src/main.py").
            file_content: Raw file bytes.
            content_type: MIME type of the file.
            filename: Original filename (for multipart form).

        Returns:
            UploadFileResponse with persisted=True and sandbox_synced status.

        Raises:
            SessionNotFoundError: Session does not exist.
            ForbiddenError: Caller is not the session owner.
            SessionInactiveError: Session is in a terminal state.
            ValidationError: Invalid file path.
            DownstreamError: Workspace Service is unreachable.
        """
        validate_file_path(file_path)

        # Look up session — validate exists, ownership, non-terminal
        session = await self._repo.get(session_id)
        if not session:
            raise SessionNotFoundError(session_id)

        if session.user_id != user_id:
            raise ForbiddenError("Not the session owner")

        if session.status in TERMINAL_STATUSES:
            raise SessionInactiveError(session.status)

        # Phase 1: Persist to S3 via Workspace Service
        await self._persist_to_workspace_service(
            workspace_id=session.workspace_id,
            file_path=file_path,
            file_content=file_content,
            content_type=content_type,
            filename=filename,
            session_id=session_id,
        )

        # Phase 2: Sync to sandbox (best-effort)
        sandbox_synced = False
        if session.status in SANDBOX_ACTIVE_STATUSES and session.sandbox_endpoint:
            sandbox_synced = await self._sync_to_sandbox(
                sandbox_endpoint=session.sandbox_endpoint,
                file_path=file_path,
                session_id=session_id,
            )

        return UploadFileResponse(
            path=file_path,
            size=len(file_content),
            persisted=True,
            sandbox_synced=sandbox_synced,
        )

    async def _persist_to_workspace_service(
        self,
        workspace_id: str,
        file_path: str,
        file_content: bytes,
        content_type: str,
        filename: str,
        session_id: str,
    ) -> None:
        """Upload file to Workspace Service (S3)."""
        url = f"/workspaces/{workspace_id}/files"
        try:
            resp = await self._workspace_http.post(
                url,
                params={"path": file_path},
                files={"file": (filename, file_content, content_type)},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "workspace_service_upload_failed",
                session_id=session_id,
                workspace_id=workspace_id,
                path=file_path,
                error=str(exc),
            )
            raise DownstreamError("Workspace Service", str(exc)) from exc

        if resp.status_code == 413:
            raise ValidationError("File too large")

        if resp.status_code == 400:
            raise ValidationError(f"Invalid file path: {file_path}")

        if resp.status_code >= 400:
            logger.warning(
                "workspace_service_upload_error",
                session_id=session_id,
                workspace_id=workspace_id,
                path=file_path,
                status=resp.status_code,
            )
            raise DownstreamError(
                "Workspace Service",
                f"File upload failed (HTTP {resp.status_code})",
            )

        logger.info(
            "workspace_service_upload_success",
            session_id=session_id,
            workspace_id=workspace_id,
            path=file_path,
            size=len(file_content),
        )

    async def _sync_to_sandbox(
        self,
        sandbox_endpoint: str,
        file_path: str,
        session_id: str,
    ) -> bool:
        """Send workspace.sync RPC to sandbox. Best-effort — returns False on failure."""
        rpc_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "workspace.sync",
                "params": {"direction": "pull", "paths": [file_path]},
            }
        )

        try:
            resp = await self._sandbox_http.post(
                f"{sandbox_endpoint}/rpc",
                content=rpc_body,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(self._sync_timeout, connect=5.0),
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "sandbox_sync_failed",
                session_id=session_id,
                path=file_path,
                error=str(exc),
            )
            return False
        except httpx.HTTPError as exc:
            logger.warning(
                "sandbox_sync_error",
                session_id=session_id,
                path=file_path,
                error=str(exc),
            )
            return False

        if resp.status_code >= 400:
            logger.warning(
                "sandbox_sync_rpc_error",
                session_id=session_id,
                path=file_path,
                status=resp.status_code,
            )
            return False

        logger.info(
            "sandbox_sync_success",
            session_id=session_id,
            path=file_path,
        )
        return True
