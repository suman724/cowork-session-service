# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

`cowork-session-service` is the entry point for every agent session. It establishes sessions, performs compatibility checks, resolves workspaces (via Workspace Service), fetches policy bundles (via Policy Service), and returns everything the Local Agent Host needs to begin work.

## Tech Stack

Python, FastAPI, PynamoDB/boto3, Pydantic models from `cowork-platform`.

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create session — resolve workspace, fetch policy, return sessionId + policyBundle + featureFlags |
| `POST` | `/sessions/{sessionId}/resume` | Resume session (after completion, failure, or crash) — re-validate policy, extend expiry, return refreshed bundle |
| `POST` | `/sessions/{sessionId}/register` | Sandbox self-registration — validates task ARN, stores endpoint, transitions to `SANDBOX_READY`, returns policy bundle |
| `POST` | `/sessions/{sessionId}/cancel` | Cancel session |
| `GET` | `/sessions/{sessionId}` | Get session metadata (includes sandbox fields when present) |
| `POST` | `/sessions/{sessionId}/rpc` | Proxy: forward JSON-RPC to sandbox `/rpc` |
| `GET` | `/sessions/{sessionId}/events` | Proxy: SSE stream from sandbox (supports `Last-Event-ID`) |
| `POST` | `/sessions/{sessionId}/upload` | Unified upload: persist to S3 via Workspace Service, then sync to sandbox |
| `GET` | `/sessions/{sessionId}/files/{path}` | Proxy: download file from sandbox workspace |
| `GET` | `/sessions/{sessionId}/files` | Proxy: list files or download workspace archive |

## Session Handshake Flow

1. Local Agent Host → `POST /sessions` with clientInfo, capabilities, workspaceHint
2. Session Service → Workspace Service: resolve or create workspace
3. Session Service → Policy Service: fetch policy bundle
4. Session Service → Local Agent Host: sessionId, workspaceId, policyBundle, featureFlags

LLM Gateway config (endpoint + auth token) is NOT in the response — agent-runtime reads from local env vars.

## Workspace Resolution

- `workspaceHint.localPaths` provided → resolve/create a `local`-scoped workspace (reused per project)
- No `workspaceHint` (desktop) → create a `general`-scoped workspace (single-use)
- `cloud_sandbox` sessions → create a `cloud`-scoped workspace (S3-backed)

## Compatibility Check

Validates at handshake: `localAgentHostVersion` and `desktopAppVersion` within supported ranges, requested capabilities are a subset of tenant policy. Returns `compatibilityStatus: "incompatible"` with reason if checks fail.

## Repository Pattern

```
SessionService
  └── SessionRepository (interface)
        ├── DynamoSessionRepository   ← production + test tiers
        └── InMemorySessionRepository ← unit tests
```

## DynamoDB Table: `{env}-sessions`

| Key | Value |
|-----|-------|
| PK | `sessionId` (String) |
| TTL | `ttl` (Unix epoch, set from `expiresAt`) |

| GSI | PK | SK | Use |
|-----|----|----|-----|
| `tenantId-userId-index` | `tenantId` | `createdAt` | List sessions for a user |

Stored: `sessionId`, `tenantId`, `userId`, `workspaceId`, `executionEnvironment`, `status`, `createdAt`, `expiresAt`, `ttl`, `clientInfo`, `sandboxEndpoint`, `taskArn`, `expectedTaskArn`, `networkAccess`, `lastActivityAt`

## Session States

Desktop sessions:
`SESSION_CREATED` → `SESSION_RUNNING` ↔ `WAITING_FOR_LLM` / `WAITING_FOR_TOOL` / `WAITING_FOR_APPROVAL` / `SESSION_PAUSED` → `SESSION_COMPLETED` / `SESSION_FAILED` / `SESSION_CANCELLED`

Sandbox sessions (cloud_sandbox):
`SANDBOX_PROVISIONING` → `SANDBOX_READY` → `SESSION_RUNNING` ↔ (same as desktop) → `SANDBOX_TERMINATED`

`SANDBOX_PROVISIONING`: ECS task is starting. Set at session creation for `cloud_sandbox` sessions.
`SANDBOX_READY`: Container registered via `POST /sessions/{id}/register`. Sandbox is ready for work.
`SANDBOX_TERMINATED`: Container shut down (idle timeout, max duration, or explicit). Terminal state.

Resumable: `SESSION_COMPLETED` and `SESSION_FAILED` can transition back to `SESSION_RUNNING` via `POST /sessions/{id}/resume`. `SESSION_CANCELLED` and `SANDBOX_TERMINATED` are terminal.

## Sandbox Launcher

Pluggable `SandboxLauncher` abstraction with two implementations:

| Config | Implementation | Use case |
|--------|---------------|----------|
| `SANDBOX_LAUNCHER_TYPE=ecs` | `EcsSandboxLauncher` — ECS RunTask/StopTask/DescribeTasks | Production |
| `SANDBOX_LAUNCHER_TYPE=local` | `LocalSandboxLauncher` — subprocess spawn with free port | Development |

`SandboxService` orchestrates provisioning (limit check → launch → store expected_task_arn) and termination (best-effort stop → status transition). Wired in `main.py` lifespan via `AsyncExitStack`.

Key config: `SANDBOX_MAX_CONCURRENT_SESSIONS` (default 5), `ECS_CLUSTER`, `ECS_TASK_DEFINITION`, `ECS_SUBNETS`, `ECS_SECURITY_GROUPS`, `AGENT_RUNTIME_PATH`, `SESSION_SERVICE_URL`.

## Proxy Layer

`ProxyService` resolves sandbox endpoints with TTL caching (30s default), validates session ownership and state, and batch-updates `lastActivityAt` (at most once per 60s). Five proxy endpoints forward browser traffic to sandbox containers:

- `POST /rpc` — JSON-RPC (buffered request/response)
- `GET /events` — SSE streaming (chunk-by-chunk, `Last-Event-ID` pass-through)
- `POST /upload` — Unified file upload (S3 persist + sandbox sync)
- `GET /files/{path}` — File download
- `GET /files` — File listing or archive download

Error mapping: sandbox unreachable → 503, session not found → 404, inactive → 409, wrong owner → 403. Separate `httpx.AsyncClient` with its own connection pool for sandbox connections.

Key config: `PROXY_ENDPOINT_CACHE_TTL_SECONDS` (30), `PROXY_ACTIVITY_BATCH_SECONDS` (60), `PROXY_TIMEOUT_SECONDS` (30), `PROXY_SSE_TIMEOUT_SECONDS` (14400).

## Sandbox Lifecycle Manager

`SandboxLifecycleManager` runs as a background `asyncio.Task` started in lifespan (only when `sandbox_service` is configured). Periodically checks all active sandbox sessions for:

1. **Provisioning timeout**: `SANDBOX_PROVISIONING` sessions older than `SANDBOX_PROVISION_TIMEOUT_SECONDS` (default 180) → transition to `SESSION_FAILED`
2. **Max duration**: Active sandbox sessions older than `SANDBOX_MAX_DURATION_SECONDS` (default 14400 / 4h) → terminate via `SandboxService`
3. **Idle timeout**: Sessions with no `lastActivityAt` update within `SANDBOX_IDLE_TIMEOUT_SECONDS` (default 1800 / 30m) AND no running tasks → terminate via `SandboxService`

Key design:
- Uses `conditional_update_status()` (DynamoDB ConditionExpression) to prevent double-termination when multiple service instances run concurrently
- Per-session error handling — one failed session doesn't block others
- `terminate_sandbox()` is best-effort — failure is logged but doesn't crash the loop
- Check interval: `SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS` (default 300 / 5m)

## External Calls

- Policy Service: `GET /policy-bundles?tenantId=...&userId=...&sessionId=...&capabilities=...`
- Workspace Service: `POST /workspaces` (create/resolve)
- ECS (production): `RunTask`, `StopTask`, `DescribeTasks` via aioboto3

## Design Doc

Full specification: `cowork-infra/docs/services/session-service.md`

---

## Engineering Standards

### Project Structure

```
cowork-session-service/
  CLAUDE.md
  README.md
  Makefile
  Dockerfile
  pyproject.toml
  .python-version             # 3.12
  .env.example
  src/
    session_service/
      __init__.py
      main.py                 # FastAPI app factory with lifespan
      config.py               # pydantic-settings: Settings class
      dependencies.py         # FastAPI Depends providers (repos, HTTP clients)
      routes/
        __init__.py
        health.py             # GET /health, GET /ready
        sessions.py           # Session CRUD endpoints
        sandbox.py            # Sandbox registration endpoint
        proxy.py              # Proxy endpoints (rpc, events, upload, files)
        tasks.py              # Task CRUD endpoints
      services/
        __init__.py
        session_service.py    # Business logic
        compatibility.py      # Version/capability compatibility checks
        sandbox_launcher.py   # SandboxLauncher protocol + LaunchResult
        sandbox_service.py    # Sandbox provisioning and termination orchestration
        sandbox_lifecycle.py  # Background lifecycle manager (idle/provisioning/max-duration)
        proxy_service.py      # Endpoint caching, ownership validation, activity tracking
        file_upload_service.py # Unified upload: S3 persist + sandbox sync
      repositories/
        __init__.py
        base.py               # SessionRepository Protocol
        dynamo.py             # DynamoSessionRepository
        memory.py             # InMemorySessionRepository
      clients/
        __init__.py
        policy_client.py      # Policy Service HTTP client
        workspace_client.py   # Workspace Service HTTP client
        ecs_launcher.py       # ECS Fargate sandbox launcher (production)
        local_launcher.py     # Local subprocess sandbox launcher (development)
      models/
        __init__.py
        domain.py             # Session domain models
        requests.py           # API request models
        responses.py          # API response models
      exceptions.py           # Service-specific exceptions
  scripts/
    test-web-sandbox.py       # E2E integration test for web sandbox lifecycle
  tests/
    unit/                     # pytest -m unit (InMemory repos, mocked clients)
    service/                  # pytest -m service (DynamoDB Local)
    integration/              # pytest -m integration (LocalStack, real HTTP to downstream)
    fixtures/                 # Policy bundles, client info, workspace hints
    conftest.py
```

### Python Tooling

- **Python**: 3.12+
- **Package manager**: pip with `pyproject.toml` (`[project]` table, PEP 621)
- **Linting/formatting**: `ruff`
  - Enable rule sets: `E`, `F`, `W`, `I`, `N`, `UP`, `S`, `B`, `A`, `C4`, `SIM`, `TCH`, `ARG`, `PTH`, `RUF`
  - Line length: 100
- **Type checking**: `mypy --strict`
- **Testing**: `pytest` with `pytest-asyncio`, `pytest-cov`, `httpx` (for `AsyncClient` test client)
- **Coverage**: 90% for unit tests

### Makefile Targets

```
make help              # Show all targets
make install           # pip install -e ".[dev]"
make lint              # ruff check src/ tests/
make format            # ruff format src/ tests/
make format-check      # ruff format --check src/ tests/
make typecheck         # mypy src/
make test              # pytest -m unit
make test-unit         # pytest -m unit
make test-service      # pytest -m service (requires DynamoDB Local)
make test-integration  # pytest -m integration (requires LocalStack)
make test-web-sandbox  # E2E web sandbox lifecycle (requires running services)
make coverage          # pytest -m unit --cov --cov-fail-under=90
make docker-build      # docker build -t session-service .
make docker-run        # docker run with .env
make check             # CI gate: lint + format-check + typecheck + test
make clean             # Remove __pycache__, .pytest_cache, .mypy_cache, dist/
```

### Error Handling

Custom exception hierarchy:
```
ServiceError (base — carries code, message, retryable, details)
  ├── NotFoundError          → 404, code: SESSION_NOT_FOUND
  ├── ConflictError          → 409 (e.g., session already exists)
  ├── ValidationError        → 400, code: INVALID_REQUEST
  ├── SandboxRegistrationError → 409 (wrong state, task ARN mismatch)
  ├── SandboxProvisionError  → 502 (ECS RunTask failure, subprocess error)
  ├── ConcurrentSessionLimitError → 409 (too many active sandbox sessions)
  ├── ForbiddenError         → 403 (not session owner)
  ├── SessionInactiveError   → 409 (session not in proxyable state)
  ├── SandboxUnavailableError → 503 (sandbox container not responding)
  ├── PolicyBundleError      → 502 (Policy Service returned invalid bundle)
  ├── DownstreamError        → 502/503 (Policy Service or Workspace Service unreachable)
  └── IncompatibleError      → 409, code: POLICY_BUNDLE_INVALID
```

FastAPI exception handlers in `main.py` catch `ServiceError` and return the standard error shape from `cowork-platform`. Unhandled exceptions return `500 INTERNAL_ERROR` with no internal details leaked.

### FastAPI Patterns

- **App factory** with `lifespan` context manager: create httpx clients and DynamoDB resources on startup, close on shutdown.
- **Dependency injection** via `Depends()`: repository instances, HTTP clients, settings — all injected, never imported globally.
- **Request ID middleware**: Generate `X-Request-ID` (UUID) for every request, bind to structlog context, propagate to downstream calls.
- **Structured logging middleware**: Log request method, path, status, duration, request_id on every response.
- **Health endpoints**: `GET /health` (liveness, always 200), `GET /ready` (checks DynamoDB connectivity + downstream service health).

### Repository Pattern

```python
class SessionRepository(Protocol):
    async def create(self, session: Session) -> Session: ...
    async def get(self, session_id: str) -> Session | None: ...
    async def update(self, session: Session) -> Session: ...
    async def list_by_tenant_user(self, tenant_id: str, user_id: str) -> list[Session]: ...
```

- `DynamoSessionRepository`: Uses aioboto3 or PynamoDB async. Connects via `AWS_ENDPOINT_URL` env var.
- `InMemorySessionRepository`: Dict-based, for unit tests. Same interface.
- Never use repository implementations directly — always inject via `Depends()`.

### Testing

- **Unit tests** (`@pytest.mark.unit`): InMemory repos, mocked HTTP clients (Policy Service, Workspace Service). Test business logic: session creation, compatibility check, state transitions, workspace resolution.
- **Service tests** (`@pytest.mark.service`): DynamoDB Local. Test DynamoDB repository — CRUD, GSI queries, TTL, conditional writes.
- **Integration tests** (`@pytest.mark.integration`): LocalStack. Full HTTP flow: create session → verify downstream calls → check DynamoDB state.
- **Session state machine tests**: Verify all valid transitions, reject invalid transitions.
- **Compatibility check tests**: Various client versions × supported ranges → compatible/incompatible.
- **Fixtures**: Pre-built `clientInfo`, `workspaceHint`, policy bundle responses.

### Async Patterns

- All route handlers and service methods are `async def`.
- `httpx.AsyncClient` with connection pooling for Policy Service and Workspace Service calls. Created in lifespan, injected via Depends.
- Timeouts: 10s for Policy Service, 10s for Workspace Service. Configurable via Settings.

### Docker

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY src/ src/
USER nobody
EXPOSE 8000
HEALTHCHECK CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"
CMD ["uvicorn", "session_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

Multi-stage build, non-root user, health check.
