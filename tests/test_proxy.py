"""Tests for proxy service and proxy routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from session_service.exceptions import (
    ForbiddenError,
    SandboxUnavailableError,
    SessionInactiveError,
    SessionNotFoundError,
)
from session_service.models.domain import SessionDomain
from session_service.repositories.memory import InMemorySessionRepository
from session_service.services.proxy_service import ProxyService


def _make_session(
    session_id: str = "sess-1",
    user_id: str = "user-1",
    status: str = "SANDBOX_READY",
    sandbox_endpoint: str = "http://10.0.1.42:8080",
) -> SessionDomain:
    now = datetime.now(UTC)
    return SessionDomain(
        session_id=session_id,
        workspace_id="ws-1",
        tenant_id="tenant-1",
        user_id=user_id,
        execution_environment="cloud_sandbox",
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        sandbox_endpoint=sandbox_endpoint if status != "SANDBOX_PROVISIONING" else None,
    )


# ---------------------------------------------------------------------------
# ProxyService unit tests
# ---------------------------------------------------------------------------


class TestProxyServiceResolve:
    @pytest.mark.unit
    async def test_resolve_returns_endpoint(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo)
        endpoint = await proxy.resolve_sandbox("sess-1", "user-1")
        assert endpoint == "http://10.0.1.42:8080"

    @pytest.mark.unit
    async def test_resolve_caches_endpoint(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo)
        # First call hits repo
        await proxy.resolve_sandbox("sess-1", "user-1")
        # Delete from repo — cache should still return
        await repo.delete("sess-1")
        endpoint = await proxy.resolve_sandbox("sess-1", "user-1")
        assert endpoint == "http://10.0.1.42:8080"

    @pytest.mark.unit
    async def test_resolve_cache_expires(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo, endpoint_cache_ttl=0.0)  # Immediate expiry
        await proxy.resolve_sandbox("sess-1", "user-1")
        # Delete from repo — expired cache forces re-fetch
        await repo.delete("sess-1")
        with pytest.raises(SessionNotFoundError):
            await proxy.resolve_sandbox("sess-1", "user-1")

    @pytest.mark.unit
    async def test_resolve_session_not_found(self) -> None:
        repo = InMemorySessionRepository()
        proxy = ProxyService(repo)
        with pytest.raises(SessionNotFoundError):
            await proxy.resolve_sandbox("nonexistent", "user-1")

    @pytest.mark.unit
    async def test_resolve_wrong_user(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session(user_id="user-1")
        await repo.create(session)

        proxy = ProxyService(repo)
        with pytest.raises(ForbiddenError):
            await proxy.resolve_sandbox("sess-1", "other-user")

    @pytest.mark.unit
    async def test_resolve_inactive_session(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session(status="SESSION_COMPLETED")
        await repo.create(session)

        proxy = ProxyService(repo)
        with pytest.raises(SessionInactiveError):
            await proxy.resolve_sandbox("sess-1", "user-1")

    @pytest.mark.unit
    async def test_resolve_provisioning_no_endpoint(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session(status="SANDBOX_PROVISIONING")
        await repo.create(session)

        proxy = ProxyService(repo)
        with pytest.raises(SessionInactiveError):
            await proxy.resolve_sandbox("sess-1", "user-1")

    @pytest.mark.unit
    async def test_resolve_no_endpoint(self) -> None:
        """Session is active but endpoint is missing (shouldn't happen normally)."""
        repo = InMemorySessionRepository()
        session = _make_session(sandbox_endpoint="")
        # Force endpoint to None while keeping SANDBOX_READY status
        session.sandbox_endpoint = None
        await repo.create(session)

        proxy = ProxyService(repo)
        with pytest.raises(SandboxUnavailableError):
            await proxy.resolve_sandbox("sess-1", "user-1")

    @pytest.mark.unit
    async def test_invalidate_cache(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo)
        await proxy.resolve_sandbox("sess-1", "user-1")
        proxy.invalidate_cache("sess-1")

        # After invalidation, should hit repo again
        await repo.delete("sess-1")
        with pytest.raises(SessionNotFoundError):
            await proxy.resolve_sandbox("sess-1", "user-1")

    @pytest.mark.unit
    async def test_resolve_all_proxyable_statuses(self) -> None:
        """All active statuses should allow proxy."""
        proxyable = [
            "SANDBOX_READY",
            "SESSION_RUNNING",
            "WAITING_FOR_LLM",
            "WAITING_FOR_TOOL",
            "WAITING_FOR_APPROVAL",
            "SESSION_PAUSED",
        ]
        for status in proxyable:
            repo = InMemorySessionRepository()
            session = _make_session(status=status, sandbox_endpoint="http://10.0.1.42:8080")
            await repo.create(session)

            proxy = ProxyService(repo)
            endpoint = await proxy.resolve_sandbox("sess-1", "user-1")
            assert endpoint == "http://10.0.1.42:8080", f"Failed for status {status}"


class TestProxyServiceActivity:
    @pytest.mark.unit
    async def test_update_activity_writes_on_first_call(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo)
        await proxy.update_activity("sess-1")

        updated = await repo.get("sess-1")
        assert updated is not None
        assert updated.last_activity_at is not None

    @pytest.mark.unit
    async def test_update_activity_batches_within_window(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo, activity_batch_seconds=60.0)
        await proxy.update_activity("sess-1")

        updated = await repo.get("sess-1")
        assert updated is not None
        first_activity = updated.last_activity_at

        # Second call within batch window — should NOT update
        await proxy.update_activity("sess-1")
        updated = await repo.get("sess-1")
        assert updated is not None
        assert updated.last_activity_at == first_activity

    @pytest.mark.unit
    async def test_update_activity_writes_after_window(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo, activity_batch_seconds=0.0)  # No batching
        await proxy.update_activity("sess-1")

        updated = await repo.get("sess-1")
        assert updated is not None
        first_activity = updated.last_activity_at

        # With batch_seconds=0, next call should also write
        await proxy.update_activity("sess-1")
        updated = await repo.get("sess-1")
        assert updated is not None
        # Both writes happen, second should be >= first
        assert updated.last_activity_at is not None
        assert updated.last_activity_at >= first_activity  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Proxy route integration tests (using test app)
# ---------------------------------------------------------------------------


class MockProxyHttp:
    """Mock httpx.AsyncClient that supports request(), build_request(), and send()."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.request = AsyncMock(return_value=response)
        self.post = AsyncMock(return_value=response)
        self.send = AsyncMock(return_value=response)
        self.last_build_request_kwargs: dict[str, object] = {}

    def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
        self.last_build_request_kwargs = {"method": method, "url": url, **kwargs}
        return httpx.Request(method, url)


class MockProxyHttpError:
    """Mock httpx.AsyncClient that raises on all calls."""

    def __init__(self, exc: Exception) -> None:
        self.request = AsyncMock(side_effect=exc)
        self.post = AsyncMock(side_effect=exc)
        self.send = AsyncMock(side_effect=exc)

    def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
        return httpx.Request(method, url)


def _create_test_app(
    repo: InMemorySessionRepository,
    proxy_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with proxy routes for testing."""
    from session_service.exceptions import ServiceError
    from session_service.routes import proxy as proxy_routes

    app = FastAPI()
    app.state.proxy_service = ProxyService(repo)
    app.state.proxy_http = proxy_http or httpx.AsyncClient()
    app.state.proxy_sse_timeout = 14400.0
    app.include_router(proxy_routes.router)

    @app.exception_handler(ServiceError)
    async def handle_service_error(request: object, exc: ServiceError) -> None:
        from fastapi.responses import JSONResponse

        return JSONResponse(  # type: ignore[return-value]
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_error(request: object, exc: Exception) -> None:
        from fastapi.responses import JSONResponse

        return JSONResponse(  # type: ignore[return-value]
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "Internal server error"},
        )

    return app


class TestProxyRoutes:
    @pytest.mark.unit
    async def test_rpc_forwards_to_sandbox(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "result": "ok", "id": 1},
            headers={"content-type": "application/json"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )

        assert resp.status_code == 200
        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert "http://10.0.1.42:8080/rpc" in str(call_args)

    @pytest.mark.unit
    async def test_rpc_session_not_found(self) -> None:
        repo = InMemorySessionRepository()
        app = _create_test_app(repo)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/nonexistent/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 404

    @pytest.mark.unit
    async def test_rpc_wrong_user(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session(user_id="user-1")
        await repo.create(session)

        app = _create_test_app(repo)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "wrong-user"},
            )
        assert resp.status_code == 403

    @pytest.mark.unit
    async def test_rpc_inactive_session(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session(status="SANDBOX_TERMINATED")
        await repo.create(session)

        app = _create_test_app(repo)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 409

    @pytest.mark.unit
    async def test_rpc_sandbox_unreachable(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_http = MockProxyHttpError(httpx.ConnectError("Connection refused"))

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 503

    @pytest.mark.unit
    async def test_rpc_sandbox_timeout(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_http = MockProxyHttpError(httpx.TimeoutException("timed out"))

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 503

    @pytest.mark.unit
    async def test_upload_forwards_to_sandbox(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            json={"status": "uploaded"},
            headers={"content-type": "application/json"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/upload",
                files={"file": ("test.txt", b"hello", "text/plain")},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 200

    @pytest.mark.unit
    async def test_file_download_forwards_to_sandbox(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            content=b"file content here",
            headers={"content-type": "text/plain"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/sessions/sess-1/files/src/main.py",
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 200

    @pytest.mark.unit
    async def test_events_forwards_to_sandbox(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            content=b"data: hello\n\n",
            headers={"content-type": "text/event-stream"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/sessions/sess-1/events",
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "text/event-stream; charset=utf-8"

    @pytest.mark.unit
    async def test_events_forwards_last_event_id(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            content=b"data: reconnected\n\n",
            headers={"content-type": "text/event-stream"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/sessions/sess-1/events",
                headers={"X-User-Id": "user-1", "Last-Event-ID": "42"},
            )
        assert resp.status_code == 200
        # Verify Last-Event-ID was forwarded
        assert mock_http.last_build_request_kwargs.get("headers", {}).get("Last-Event-ID") == "42"

    @pytest.mark.unit
    async def test_files_list_forwards_to_sandbox(self) -> None:
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            200,
            json={"files": ["a.py", "b.py"]},
            headers={"content-type": "application/json"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/sessions/sess-1/files",
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 200

    @pytest.mark.unit
    async def test_rpc_sandbox_500_becomes_503(self) -> None:
        """Sandbox 5xx errors should be translated to 503, not passed raw."""
        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        mock_response = httpx.Response(
            500,
            json={"error": "internal sandbox error"},
            headers={"content-type": "application/json"},
        )
        mock_http = MockProxyHttp(mock_response)

        app = _create_test_app(repo, mock_http)  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/sessions/sess-1/rpc",
                json={"jsonrpc": "2.0", "method": "test", "id": 1},
                headers={"X-User-Id": "user-1"},
            )
        assert resp.status_code == 503
        assert resp.json()["code"] == "SANDBOX_UNREACHABLE"

    @pytest.mark.unit
    async def test_file_download_path_traversal_rejected(self) -> None:
        """Paths with '..' components should be rejected at the handler level."""
        from unittest.mock import MagicMock

        from session_service.exceptions import ValidationError as SvcValidationError
        from session_service.routes.proxy import proxy_file_download

        repo = InMemorySessionRepository()
        session = _make_session()
        await repo.create(session)

        proxy = ProxyService(repo)
        mock_http = MockProxyHttp(httpx.Response(200))
        mock_request = MagicMock()
        mock_request.headers = {"X-User-Id": "user-1"}

        # Path with .. segments
        with pytest.raises(SvcValidationError, match="Invalid file path"):
            await proxy_file_download(
                session_id="sess-1",
                file_path="src/../../etc/passwd",
                request=mock_request,
                proxy=proxy,
                proxy_http=mock_http,  # type: ignore[arg-type]
            )

        # Absolute path
        with pytest.raises(SvcValidationError, match="Invalid file path"):
            await proxy_file_download(
                session_id="sess-1",
                file_path="/etc/passwd",
                request=mock_request,
                proxy=proxy,
                proxy_http=mock_http,  # type: ignore[arg-type]
            )
