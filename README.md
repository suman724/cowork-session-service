# cowork-session-service

Session lifecycle service for the cowork platform. Entry point for all agent sessions — creates sessions, performs compatibility checks, resolves workspaces (via Workspace Service), and fetches policy bundles (via Policy Service).

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sessions` | Create session (handshake). Cloud sandbox sessions start in `SANDBOX_PROVISIONING` |
| POST | `/sessions/{id}/resume` | Resume session (re-fetch policy) |
| POST | `/sessions/{id}/register` | Sandbox self-registration — validates task ARN, stores endpoint, returns policy bundle |
| POST | `/sessions/{id}/cancel` | Cancel session |
| GET | `/sessions/{id}` | Get session metadata (includes sandbox fields when present) |
| POST | `/sessions/{id}/tasks` | Create task within a session |
| POST | `/sessions/{id}/tasks/{taskId}/complete` | Mark task as completed/failed/cancelled |
| GET | `/sessions/{id}/tasks` | List tasks for a session |
| GET | `/sessions/{id}/tasks/{taskId}` | Get task details |
| POST | `/sessions/{id}/rpc` | Proxy: forward JSON-RPC to sandbox |
| GET | `/sessions/{id}/events` | Proxy: SSE stream from sandbox |
| POST | `/sessions/{id}/upload` | Proxy: file upload to sandbox |
| GET | `/sessions/{id}/files/{path}` | Proxy: download file from sandbox |
| GET | `/sessions/{id}/files` | Proxy: list/archive sandbox files |
| GET | `/health` | Liveness check |
| GET | `/ready` | Readiness check |

## Development

```bash
# Install dependencies (requires cowork-platform sibling repo)
make install

# Run all checks
make check

# Run with uvicorn (requires Policy + Workspace services running)
uvicorn session_service.main:app --reload

# Run tests with coverage
make coverage

# Build Docker image
make docker-build
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ENV` | `dev` | Environment name |
| `LOG_LEVEL` | `info` | Logging level |
| `AWS_REGION` | `us-east-1` | AWS region |
| `AWS_ENDPOINT_URL` | — | Override for DynamoDB Local |
| `DYNAMODB_TABLE_PREFIX` | `dev-` | Table name prefix |
| `POLICY_SERVICE_URL` | `http://localhost:8001` | Policy Service base URL |
| `WORKSPACE_SERVICE_URL` | `http://localhost:8002` | Workspace Service base URL |
| `DOWNSTREAM_TIMEOUT` | `30.0` | Timeout for downstream HTTP calls |
| `MIN_DESKTOP_APP_VERSION` | `0.1.0` | Minimum Desktop App version |
| `MIN_AGENT_HOST_VERSION` | `0.1.0` | Minimum Agent Host version |
| `SESSION_EXPIRY_HOURS` | `24` | Hours until session expires |
| `SANDBOX_LAUNCHER_TYPE` | `ecs` | Sandbox launcher: `ecs` (production) or `local` (development) |
| `SANDBOX_MAX_CONCURRENT_SESSIONS` | `5` | Max active sandboxes per user |
| `SANDBOX_IDLE_TIMEOUT_SECONDS` | `1800` | Terminate idle sandbox after this many seconds |
| `SANDBOX_MAX_DURATION_SECONDS` | `14400` | Max sandbox session duration (absolute) |
| `SANDBOX_PROVISION_TIMEOUT_SECONDS` | `180` | Fail provisioning sessions after this long |
| `SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS` | `300` | How often to check sandbox lifecycles |

## Session Handshake Flow

1. Client sends `POST /sessions` with `clientInfo` and `supportedCapabilities`
2. Service checks version compatibility
3. Resolves workspace via Workspace Service
4. Fetches policy bundle via Policy Service (if compatible)
5. Returns composite response with session ID, workspace ID, policy bundle

## Session States

### Desktop sessions

```
SESSION_CREATED → SESSION_RUNNING → SESSION_COMPLETED
                                  → SESSION_FAILED
                                  → SESSION_CANCELLED
SESSION_RUNNING → WAITING_FOR_LLM/TOOL/APPROVAL → SESSION_RUNNING
SESSION_RUNNING → SESSION_PAUSED → SESSION_RUNNING
SESSION_COMPLETED → SESSION_RUNNING  (via POST /sessions/{id}/resume)
SESSION_FAILED    → SESSION_RUNNING  (via POST /sessions/{id}/resume)
```

### Sandbox sessions (cloud_sandbox)

```
SANDBOX_PROVISIONING → SANDBOX_READY   (via POST /sessions/{id}/register)
                     → SESSION_FAILED  (provision timeout or launch failure)
SANDBOX_READY → SESSION_RUNNING → (same as desktop)
              → SANDBOX_TERMINATED  (idle timeout, max duration, explicit)
```

Terminal states: `SESSION_CANCELLED`, `SANDBOX_TERMINATED`

Resumable states: `SESSION_COMPLETED`, `SESSION_FAILED` — resume transitions back to `SESSION_RUNNING`, refreshes policy bundle, and extends session expiry.
