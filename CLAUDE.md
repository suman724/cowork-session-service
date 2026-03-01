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
| `POST` | `/sessions/{sessionId}/resume` | Resume after crash — re-validate policy, return refreshed bundle |
| `POST` | `/sessions/{sessionId}/cancel` | Cancel session |
| `GET` | `/sessions/{sessionId}` | Get session metadata |

## Session Handshake Flow

1. Local Agent Host → `POST /sessions` with clientInfo, capabilities, workspaceHint
2. Session Service → Workspace Service: resolve or create workspace
3. Session Service → Policy Service: fetch policy bundle
4. Session Service → Local Agent Host: sessionId, workspaceId, policyBundle, featureFlags

LLM Gateway config (endpoint + auth token) is NOT in the response — agent-runtime reads from local env vars.

## Workspace Resolution

- `workspaceHint.localPaths` provided → resolve/create a `local`-scoped workspace (reused per project)
- No `workspaceHint` → create a `general`-scoped workspace (single-use)

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

Stored: `sessionId`, `tenantId`, `userId`, `workspaceId`, `executionEnvironment`, `status`, `createdAt`, `expiresAt`, `ttl`, `clientInfo`

## Session States

`SESSION_CREATED` → `SESSION_RUNNING` ↔ `WAITING_FOR_LLM` / `WAITING_FOR_TOOL` / `WAITING_FOR_APPROVAL` / `SESSION_PAUSED` → `SESSION_COMPLETED` / `SESSION_FAILED` / `SESSION_CANCELLED`

## External Calls

- Policy Service: `GET /policy-bundles?tenantId=...&userId=...&sessionId=...&capabilities=...`
- Workspace Service: `POST /workspaces` (create/resolve)

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
      services/
        __init__.py
        session_service.py    # Business logic
        compatibility.py      # Version/capability compatibility checks
      repositories/
        __init__.py
        base.py               # SessionRepository Protocol
        dynamo.py             # DynamoSessionRepository
        memory.py             # InMemorySessionRepository
      clients/
        __init__.py
        policy_client.py      # Policy Service HTTP client
        workspace_client.py   # Workspace Service HTTP client
      models/
        __init__.py
        domain.py             # Session domain models
        requests.py           # API request models
        responses.py          # API response models
      exceptions.py           # Service-specific exceptions
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
