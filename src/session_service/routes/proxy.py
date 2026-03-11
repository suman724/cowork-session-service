"""Proxy endpoints: forward browser traffic to sandbox containers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import StreamingResponse

from session_service.dependencies import get_file_upload_service, get_proxy_http, get_proxy_service
from session_service.exceptions import SandboxUnavailableError, validate_file_path
from session_service.models.responses import UploadFileResponse
from session_service.services.file_upload_service import FileUploadService
from session_service.services.proxy_service import ProxyService

logger = structlog.get_logger()

router = APIRouter(prefix="/sessions", tags=["proxy"])

# Track background tasks to prevent GC before completion
_background_tasks: set[asyncio.Task[None]] = set()

# Placeholder user_id until auth is implemented (Step 16)
_PLACEHOLDER_USER_ID = "__proxy__"


def _fire_and_forget(coro: Any) -> None:
    """Schedule a coroutine as a background task with proper cleanup."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _get_user_id(request: Request) -> str:
    """Extract user_id from request. Placeholder until OIDC auth (Step 16)."""
    return request.headers.get("X-User-Id", _PLACEHOLDER_USER_ID)


async def _forward_request(
    proxy_http: httpx.AsyncClient,
    proxy: ProxyService,
    session_id: str,
    method: str,
    url: str,
    *,
    stream: bool = False,
    timeout: httpx.Timeout | None = None,
    log_prefix: str = "proxy",
    **kwargs: Any,
) -> httpx.Response:
    """Send a request to the sandbox, translating connection/timeout errors."""
    try:
        if stream:
            build_kwargs = {**kwargs}
            if timeout:
                build_kwargs["timeout"] = timeout
            req = proxy_http.build_request(method, url, **build_kwargs)
            resp = await proxy_http.send(req, stream=True)
        else:
            if timeout:
                kwargs["timeout"] = timeout
            resp = await proxy_http.request(method, url, **kwargs)
    except httpx.ConnectError as exc:
        logger.warning(f"{log_prefix}_connect_error", session_id=session_id, error=str(exc))
        proxy.invalidate_cache(session_id)
        raise SandboxUnavailableError("Sandbox container is not responding") from exc
    except httpx.TimeoutException as exc:
        logger.warning(f"{log_prefix}_timeout", session_id=session_id, error=str(exc))
        raise SandboxUnavailableError("Sandbox request timed out") from exc

    # Translate sandbox 5xx into proxy 503 — don't pass raw internal errors
    if resp.status_code >= 500:
        logger.warning(
            f"{log_prefix}_sandbox_error",
            session_id=session_id,
            status=resp.status_code,
        )
        if stream:
            await resp.aclose()
        raise SandboxUnavailableError(f"Sandbox returned error (status {resp.status_code})")

    return resp


async def _stream_and_close(
    resp: httpx.Response,
    *,
    swallow_remote_close: bool = False,
    session_id: str = "",
) -> AsyncIterator[bytes]:
    """Stream response bytes and ensure cleanup."""
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except httpx.RemoteProtocolError:
        if swallow_remote_close:
            logger.info("proxy_events_sandbox_closed", session_id=session_id)
        else:
            raise
    finally:
        await resp.aclose()


def _forward_headers(resp: httpx.Response) -> dict[str, str]:
    """Extract Content-Disposition and similar headers worth forwarding."""
    headers: dict[str, str] = {}
    if "content-disposition" in resp.headers:
        headers["Content-Disposition"] = resp.headers["content-disposition"]
    return headers


@router.post("/{session_id}/rpc")
async def proxy_rpc(
    session_id: str,
    request: Request,
    proxy: ProxyService = Depends(get_proxy_service),
    proxy_http: httpx.AsyncClient = Depends(get_proxy_http),
) -> StreamingResponse:
    """Forward JSON-RPC request to sandbox /rpc endpoint."""
    user_id = _get_user_id(request)
    endpoint = await proxy.resolve_sandbox(session_id, user_id)
    body = await request.body()

    resp = await _forward_request(
        proxy_http,
        proxy,
        session_id,
        "POST",
        f"{endpoint}/rpc",
        log_prefix="proxy_rpc",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    # Fire-and-forget activity update — don't block the response
    _fire_and_forget(proxy.update_activity(session_id))

    return StreamingResponse(
        content=_stream_and_close(resp),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.get("/{session_id}/events")
async def proxy_events(
    session_id: str,
    request: Request,
    proxy: ProxyService = Depends(get_proxy_service),
    proxy_http: httpx.AsyncClient = Depends(get_proxy_http),
) -> StreamingResponse:
    """SSE proxy: stream events from sandbox to browser, chunk-by-chunk."""
    user_id = _get_user_id(request)
    endpoint = await proxy.resolve_sandbox(session_id, user_id)

    # Forward Last-Event-ID for reconnect support
    headers: dict[str, str] = {}
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id

    # SSE connections are long-lived — use extended timeout
    sse_timeout = httpx.Timeout(
        request.app.state.proxy_sse_timeout,
        connect=10.0,
    )

    resp = await _forward_request(
        proxy_http,
        proxy,
        session_id,
        "GET",
        f"{endpoint}/events",
        log_prefix="proxy_events",
        stream=True,
        timeout=sse_timeout,
        headers=headers,
    )

    return StreamingResponse(
        content=_stream_and_close(resp, swallow_remote_close=True, session_id=session_id),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/upload")
async def proxy_upload(
    session_id: str,
    file: UploadFile,
    request: Request,
    path: str | None = None,
    proxy: ProxyService = Depends(get_proxy_service),
    upload_service: FileUploadService = Depends(get_file_upload_service),
) -> UploadFileResponse:
    """Unified file upload: persist to S3 via Workspace Service, then sync to sandbox.

    Query params:
        path: Target file path within workspace. Falls back to uploaded filename.
    """
    user_id = _get_user_id(request)
    file_path = path or file.filename or "upload"
    content_type = file.content_type or "application/octet-stream"
    file_content = await file.read()

    result = await upload_service.upload_file(
        session_id=session_id,
        user_id=user_id,
        file_path=file_path,
        file_content=file_content,
        content_type=content_type,
        filename=file.filename or "upload",
    )

    _fire_and_forget(proxy.update_activity(session_id))

    return result


@router.get("/{session_id}/files/{file_path:path}")
async def proxy_file_download(
    session_id: str,
    file_path: str,
    request: Request,
    proxy: ProxyService = Depends(get_proxy_service),
    proxy_http: httpx.AsyncClient = Depends(get_proxy_http),
) -> StreamingResponse:
    """Forward file download from sandbox workspace."""
    validate_file_path(file_path)

    user_id = _get_user_id(request)
    endpoint = await proxy.resolve_sandbox(session_id, user_id)

    resp = await _forward_request(
        proxy_http,
        proxy,
        session_id,
        "GET",
        f"{endpoint}/files/{file_path}",
        log_prefix="proxy_file",
        stream=True,
    )

    _fire_and_forget(proxy.update_activity(session_id))

    return StreamingResponse(
        content=_stream_and_close(resp),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=_forward_headers(resp),
    )


@router.get("/{session_id}/files")
async def proxy_file_list_or_archive(
    session_id: str,
    request: Request,
    proxy: ProxyService = Depends(get_proxy_service),
    proxy_http: httpx.AsyncClient = Depends(get_proxy_http),
) -> StreamingResponse:
    """Forward workspace file listing or archive download."""
    user_id = _get_user_id(request)
    endpoint = await proxy.resolve_sandbox(session_id, user_id)

    # Forward query params (e.g. ?archive=true)
    query_string = str(request.query_params)
    url = f"{endpoint}/files"
    if query_string:
        url = f"{url}?{query_string}"

    resp = await _forward_request(
        proxy_http,
        proxy,
        session_id,
        "GET",
        url,
        log_prefix="proxy_files",
        stream=True,
    )

    _fire_and_forget(proxy.update_activity(session_id))

    return StreamingResponse(
        content=_stream_and_close(resp),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=_forward_headers(resp),
    )
