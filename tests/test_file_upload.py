"""Tests for FileUploadService — unified upload (S3 persist + sandbox sync)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from session_service.exceptions import (
    DownstreamError,
    ForbiddenError,
    SessionInactiveError,
    SessionNotFoundError,
    ValidationError,
)
from session_service.models.domain import SessionDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.services.file_upload_service import FileUploadService


def _make_session(
    session_id: str = "sess-1",
    user_id: str = "user-1",
    workspace_id: str = "ws-1",
    status: str = "SANDBOX_READY",
    sandbox_endpoint: str | None = "http://10.0.1.42:8080",
) -> SessionDomain:
    now = datetime.now(UTC)
    return SessionDomain(
        session_id=session_id,
        workspace_id=workspace_id,
        tenant_id="tenant-1",
        user_id=user_id,
        execution_environment="cloud_sandbox",
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        sandbox_endpoint=sandbox_endpoint,
    )


def _make_workspace_http(
    status_code: int = 201,
    json_body: dict[str, object] | None = None,
) -> AsyncMock:
    """Mock workspace_http that returns a given response."""
    resp = httpx.Response(
        status_code,
        json=json_body or {"path": "test.txt", "size": 11},
    )
    mock = AsyncMock()
    mock.post = AsyncMock(return_value=resp)
    return mock


def _make_proxy_http(
    status_code: int = 200,
    json_body: dict[str, object] | None = None,
    exc: Exception | None = None,
) -> AsyncMock:
    """Mock proxy_http for sandbox sync RPC."""
    mock = AsyncMock()
    if exc:
        mock.post = AsyncMock(side_effect=exc)
    else:
        resp = httpx.Response(
            status_code,
            json=json_body or {"jsonrpc": "2.0", "result": {"synced": ["test.txt"]}, "id": 1},
        )
        mock.post = AsyncMock(return_value=resp)
    return mock


class TestFileUploadService:
    """Unit tests for FileUploadService."""

    @pytest.mark.unit
    async def test_upload_with_sandbox_running(self) -> None:
        """Upload persists to S3 and syncs to sandbox."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        result = await svc.upload_file(
            "sess-1", "user-1", "test.txt", b"hello world", "text/plain", "test.txt"
        )

        assert result.persisted is True
        assert result.sandbox_synced is True
        assert result.path == "test.txt"
        assert result.size == 11

        # Verify workspace service was called
        ws_http.post.assert_called_once()
        call_kwargs = ws_http.post.call_args
        assert "/workspaces/ws-1/files" in str(call_kwargs)

        # Verify sandbox sync RPC was called
        proxy_http.post.assert_called_once()
        call_kwargs = proxy_http.post.call_args
        assert "http://10.0.1.42:8080/rpc" in str(call_kwargs)

    @pytest.mark.unit
    async def test_upload_during_provisioning(self) -> None:
        """Upload during SANDBOX_PROVISIONING persists to S3 only."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SANDBOX_PROVISIONING", sandbox_endpoint=None)
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        result = await svc.upload_file(
            "sess-1", "user-1", "test.txt", b"hello", "text/plain", "test.txt"
        )

        assert result.persisted is True
        assert result.sandbox_synced is False

        # Workspace service called, sandbox NOT called
        ws_http.post.assert_called_once()
        proxy_http.post.assert_not_called()

    @pytest.mark.unit
    async def test_upload_to_terminated_session(self) -> None:
        """Upload to terminated session is rejected."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SANDBOX_TERMINATED")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(SessionInactiveError):
            await svc.upload_file("sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt")

    @pytest.mark.unit
    async def test_upload_to_cancelled_session(self) -> None:
        """Upload to cancelled session is rejected."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_CANCELLED")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(SessionInactiveError):
            await svc.upload_file("sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt")

    @pytest.mark.unit
    async def test_upload_s3_success_but_sync_timeout(self) -> None:
        """S3 write succeeds but sandbox sync times out — still returns persisted=True."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http(exc=httpx.TimeoutException("timed out"))
        svc = FileUploadService(repo, ws_http, proxy_http)

        result = await svc.upload_file(
            "sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt"
        )

        assert result.persisted is True
        assert result.sandbox_synced is False

    @pytest.mark.unit
    async def test_upload_s3_success_but_sync_connect_error(self) -> None:
        """S3 write succeeds but sandbox is unreachable — still returns persisted=True."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http(exc=httpx.ConnectError("Connection refused"))
        svc = FileUploadService(repo, ws_http, proxy_http)

        result = await svc.upload_file(
            "sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt"
        )

        assert result.persisted is True
        assert result.sandbox_synced is False

    @pytest.mark.unit
    async def test_upload_workspace_service_down(self) -> None:
        """Workspace Service unreachable raises DownstreamError."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = AsyncMock()
        ws_http.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(DownstreamError):
            await svc.upload_file("sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt")

    @pytest.mark.unit
    async def test_upload_file_too_large(self) -> None:
        """Workspace Service returns 413 — propagated as ValidationError."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http(status_code=413)
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(ValidationError, match="File too large"):
            await svc.upload_file(
                "sess-1", "user-1", "big.bin", b"x" * 100, "application/octet-stream", "big.bin"
            )

    @pytest.mark.unit
    async def test_upload_invalid_path_traversal(self) -> None:
        """Path with '..' is rejected."""
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(ValidationError, match="Invalid file path"):
            await svc.upload_file(
                "sess-1", "user-1", "../etc/passwd", b"evil", "text/plain", "passwd"
            )

    @pytest.mark.unit
    async def test_upload_invalid_absolute_path(self) -> None:
        """Absolute path is rejected."""
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(ValidationError, match="Invalid file path"):
            await svc.upload_file(
                "sess-1", "user-1", "/etc/passwd", b"evil", "text/plain", "passwd"
            )

    @pytest.mark.unit
    async def test_upload_wrong_user(self) -> None:
        """Upload by non-owner is rejected."""
        repo = InMemorySessionRepository()
        session = _make_session(user_id="user-1")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(ForbiddenError):
            await svc.upload_file(
                "sess-1", "other-user", "test.txt", b"data", "text/plain", "test.txt"
            )

    @pytest.mark.unit
    async def test_upload_session_not_found(self) -> None:
        """Upload to non-existent session is rejected."""
        repo = InMemorySessionRepository()
        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(SessionNotFoundError):
            await svc.upload_file(
                "nonexistent", "user-1", "test.txt", b"data", "text/plain", "test.txt"
            )

    @pytest.mark.unit
    async def test_upload_workspace_service_400(self) -> None:
        """Workspace Service returns 400 (bad path) — propagated as ValidationError."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http(status_code=400)
        proxy_http = _make_proxy_http()
        svc = FileUploadService(repo, ws_http, proxy_http)

        with pytest.raises(ValidationError, match="Invalid file path"):
            await svc.upload_file("sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt")

    @pytest.mark.unit
    async def test_upload_sandbox_sync_rpc_error(self) -> None:
        """Sandbox sync RPC returns error status — sandbox_synced=False."""
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_RUNNING")
        await repo.create(session)

        ws_http = _make_workspace_http()
        proxy_http = _make_proxy_http(status_code=500)
        svc = FileUploadService(repo, ws_http, proxy_http)

        result = await svc.upload_file(
            "sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt"
        )

        assert result.persisted is True
        assert result.sandbox_synced is False

    @pytest.mark.unit
    async def test_upload_allowed_in_all_non_terminal_states(self) -> None:
        """Upload should be allowed in any non-terminal state."""
        non_terminal = [
            "SESSION_CREATED",
            "SESSION_RUNNING",
            "WAITING_FOR_LLM",
            "WAITING_FOR_TOOL",
            "WAITING_FOR_APPROVAL",
            "SESSION_PAUSED",
            "SESSION_COMPLETED",
            "SESSION_FAILED",
            "SANDBOX_PROVISIONING",
            "SANDBOX_READY",
        ]
        for status in non_terminal:
            repo = InMemorySessionRepository()
            endpoint = "http://10.0.1.42:8080" if status != "SANDBOX_PROVISIONING" else None
            session = _make_session(status=status, sandbox_endpoint=endpoint)
            await repo.create(session)

            ws_http = _make_workspace_http()
            proxy_http = _make_proxy_http()
            svc = FileUploadService(repo, ws_http, proxy_http)

            result = await svc.upload_file(
                "sess-1", "user-1", "test.txt", b"data", "text/plain", "test.txt"
            )
            assert result.persisted is True, f"Failed for status {status}"

    @pytest.mark.unit
    async def test_response_serialization_camel_case(self) -> None:
        """UploadFileResponse serializes sandbox_synced as sandboxSynced."""
        from session_service.models.responses import UploadFileResponse

        resp = UploadFileResponse(path="test.txt", size=10, persisted=True, sandbox_synced=True)
        data = resp.model_dump(by_alias=True)
        assert "sandboxSynced" in data
        assert data["sandboxSynced"] is True
