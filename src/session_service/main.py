"""FastAPI application factory for the Session Service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from session_service.clients.policy_client import PolicyClient
from session_service.clients.workspace_client import WorkspaceClient
from session_service.config import Settings
from session_service.exceptions import ServiceError
from session_service.middleware import RequestIdMiddleware
from session_service.repositories.dynamo import DynamoSessionRepository
from session_service.repositories.dynamo_task import DynamoTaskRepository
from session_service.routes import health, sessions, tasks
from session_service.services.session_service import SessionService
from session_service.services.task_service import TaskService

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()

    log_level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )

    # DynamoDB
    boto_session = aioboto3.Session()
    boto_kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.aws_endpoint_url:
        boto_kwargs["endpoint_url"] = settings.aws_endpoint_url

    # HTTP clients for downstream services
    policy_http = httpx.AsyncClient(
        base_url=settings.policy_service_url,
        timeout=httpx.Timeout(settings.downstream_timeout),
    )
    workspace_http = httpx.AsyncClient(
        base_url=settings.workspace_service_url,
        timeout=httpx.Timeout(settings.downstream_timeout),
    )

    async with boto_session.resource("dynamodb", **boto_kwargs) as dynamodb:
        table = await dynamodb.Table(settings.sessions_table)
        repo = DynamoSessionRepository(table)

        tasks_table = await dynamodb.Table(settings.tasks_table)
        task_repo = DynamoTaskRepository(tasks_table)

        policy_client = PolicyClient(policy_http)
        workspace_client = WorkspaceClient(workspace_http)

        app.state.session_service = SessionService(repo, policy_client, workspace_client, settings)
        app.state.task_service = TaskService(task_repo, repo)

        logger.info("session_service_started", env=settings.env)
        yield

        await policy_http.aclose()
        await workspace_http.aclose()
        logger.info("session_service_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cowork Session Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(tasks.router)

    app.add_exception_handler(ServiceError, _service_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

    return app


async def _service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    se = (
        exc
        if isinstance(exc, ServiceError)
        else ServiceError("Unknown", code="INTERNAL_ERROR", status_code=500)
    )
    body: dict[str, Any] = {
        "code": se.code,
        "message": se.message,
        "retryable": se.status_code >= 500,
    }
    return JSONResponse(status_code=se.status_code, content=body)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": "Internal server error", "retryable": True},
    )


app = create_app()
