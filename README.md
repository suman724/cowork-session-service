# cowork-session-service

Session lifecycle service for the cowork platform. Entry point for all agent sessions — creates sessions, performs compatibility checks, resolves workspaces (via Workspace Service), and fetches policy bundles (via Policy Service).

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sessions` | Create session (handshake) |
| POST | `/sessions/{id}/resume` | Resume session (re-fetch policy) |
| POST | `/sessions/{id}/cancel` | Cancel session |
| GET | `/sessions/{id}` | Get session metadata |
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

## Session Handshake Flow

1. Client sends `POST /sessions` with `clientInfo` and `supportedCapabilities`
2. Service checks version compatibility
3. Resolves workspace via Workspace Service
4. Fetches policy bundle via Policy Service (if compatible)
5. Returns composite response with session ID, workspace ID, policy bundle

## Session States

```
SESSION_CREATED → SESSION_RUNNING → SESSION_COMPLETED
                                  → SESSION_FAILED
                                  → SESSION_CANCELLED
SESSION_RUNNING → WAITING_FOR_LLM/TOOL/APPROVAL → SESSION_RUNNING
SESSION_RUNNING → SESSION_PAUSED → SESSION_RUNNING
SESSION_COMPLETED → SESSION_RUNNING  (via POST /sessions/{id}/resume)
SESSION_FAILED    → SESSION_RUNNING  (via POST /sessions/{id}/resume)
```

Terminal state: `SESSION_CANCELLED`

Resumable states: `SESSION_COMPLETED`, `SESSION_FAILED` — resume transitions back to `SESSION_RUNNING`, refreshes policy bundle, and extends session expiry.
