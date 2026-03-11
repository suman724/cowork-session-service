#!/usr/bin/env python3
"""
Test the full web sandbox lifecycle end-to-end.

Scenarios:
  1. Full lifecycle: create → provision → ready → proxy RPC → SSE events → cancel
  2. SSE reconnect: disconnect mid-stream, reconnect with Last-Event-ID, verify replay
  3. File upload/download through proxy
  4. Idle timeout: sandbox terminated after inactivity
  5. Provisioning timeout: sandbox never registers → SESSION_FAILED

Requires:
  - Session Service running on :8000 with SANDBOX_LAUNCHER_TYPE=local
  - Policy Service running on :8001
  - Workspace Service running on :8002
  - LocalStack running on :4566 (DynamoDB + S3)
  - LLM Gateway reachable (LLM_GATEWAY_ENDPOINT env var on session-service)

For idle/provisioning timeout tests, start session-service with:
  SANDBOX_IDLE_TIMEOUT_SECONDS=10
  SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS=2
  SANDBOX_PROVISION_TIMEOUT_SECONDS=5

Usage:
  python scripts/test-web-sandbox.py
  make test-web-sandbox
"""

import contextlib
import json
import os
import sys
import threading
import time

import httpx

# --- Configuration ---

SESSION_SERVICE_URL = os.environ.get("SESSION_SERVICE_URL", "http://localhost:8000")
TENANT_ID = "dev-tenant"
USER_ID = "dev-user"
POLL_INTERVAL = 0.5
SANDBOX_READY_TIMEOUT = 30
SKIP_LLM_TESTS = os.environ.get("SKIP_LLM_TESTS", "").lower() in ("1", "true", "yes")


# --- Exception ---


class TestFailureError(Exception):
    """Raised when a test scenario fails."""


# --- Helpers ---


def create_sandbox_session(client: httpx.Client) -> dict:
    """Create a cloud_sandbox session. Returns full response dict."""
    resp = client.post(
        f"{SESSION_SERVICE_URL}/sessions",
        json={
            "tenantId": TENANT_ID,
            "userId": USER_ID,
            "executionEnvironment": "cloud_sandbox",
            "clientInfo": {},
            "supportedCapabilities": [
                "File.Read",
                "File.Write",
                "Shell.Exec",
                "Network.Http",
            ],
        },
        timeout=30,
    )
    if resp.status_code != 201:
        raise TestFailureError(f"POST /sessions returned {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    print(f"    Session created: {data.get('sessionId', '?')[:12]}...")
    print(f"    Status: {data.get('status')}")
    return data


def get_session(client: httpx.Client, session_id: str) -> dict:
    """GET /sessions/{id}."""
    resp = client.get(
        f"{SESSION_SERVICE_URL}/sessions/{session_id}",
        timeout=10,
    )
    if resp.status_code != 200:
        raise TestFailureError(
            f"GET /sessions/{session_id} returned {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def poll_session_status(
    client: httpx.Client,
    session_id: str,
    target_statuses: set[str],
    timeout: float = SANDBOX_READY_TIMEOUT,
) -> dict:
    """Poll GET /sessions/{id} until status is in target_statuses."""
    start = time.time()
    last_status = "?"
    interval = POLL_INTERVAL
    while time.time() - start < timeout:
        data = get_session(client, session_id)
        last_status = data.get("status", "?")
        if last_status in target_statuses:
            return data
        time.sleep(interval)
        interval = min(interval * 1.5, 5.0)
    raise TestFailureError(
        f"Session {session_id} did not reach {target_statuses} within {timeout}s "
        f"(last status: {last_status})"
    )


def send_rpc(
    client: httpx.Client,
    session_id: str,
    method: str,
    params: dict,
    timeout: float = 60,
) -> dict:
    """POST /sessions/{id}/rpc with JSON-RPC envelope. Returns parsed response."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    resp = client.post(
        f"{SESSION_SERVICE_URL}/sessions/{session_id}/rpc",
        json=body,
        headers={"X-User-Id": USER_ID},
        timeout=timeout,
    )
    if resp.status_code >= 500:
        raise TestFailureError(f"RPC {method} returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def cancel_session(client: httpx.Client, session_id: str) -> None:
    """Best-effort cancel session."""
    with contextlib.suppress(Exception):
        client.post(
            f"{SESSION_SERVICE_URL}/sessions/{session_id}/cancel",
            timeout=10,
        )


def collect_sse_in_thread(
    session_id: str,
    events_out: list,
    stop_event: threading.Event,
    last_event_id: str | None = None,
    max_events: int = 50,
    timeout: float = 30,
) -> None:
    """Collect SSE events in a background thread."""
    headers = {"X-User-Id": USER_ID}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id

    try:
        with (
            httpx.Client() as c,
            c.stream(
                "GET",
                f"{SESSION_SERVICE_URL}/sessions/{session_id}/events",
                headers=headers,
                timeout=httpx.Timeout(timeout, connect=10),
            ) as resp,
        ):
            current: dict[str, str] = {}
            start = time.time()
            for line in resp.iter_lines():
                if stop_event.is_set() or time.time() - start > timeout:
                    break
                if line == "":
                    if current:
                        raw_data = current.get("data", "")
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            current["data"] = json.loads(raw_data)
                        events_out.append(current)
                        current = {}
                        if len(events_out) >= max_events:
                            break
                elif line.startswith("id:"):
                    current["id"] = line[3:].strip()
                elif line.startswith("event:"):
                    current["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    prev = current.get("data", "")
                    chunk = line[5:].strip()
                    current["data"] = f"{prev}\n{chunk}" if prev else chunk
    except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError):
        pass  # Expected when we stop
    except Exception as exc:
        if not stop_event.is_set():
            print(f"    SSE thread error: {exc}")


# --- Scenarios ---


def test_full_lifecycle():
    """Full lifecycle: create → provision → ready → RPC → events → cancel."""
    client = httpx.Client()
    session_id = None
    stop_event: threading.Event | None = None
    sse_thread: threading.Thread | None = None
    try:
        # Create sandbox session
        data = create_sandbox_session(client)
        session_id = data["sessionId"]
        if data.get("status") != "SANDBOX_PROVISIONING":
            raise TestFailureError(f"Expected SANDBOX_PROVISIONING, got {data.get('status')}")

        # Wait for sandbox to register
        print("    Waiting for SANDBOX_READY...")
        session_data = poll_session_status(client, session_id, {"SANDBOX_READY", "SESSION_RUNNING"})
        print(f"    Status: {session_data['status']}")

        # Verify sandbox endpoint is populated
        if not session_data.get("sandboxEndpoint"):
            raise TestFailureError("sandboxEndpoint not set after registration")
        print(f"    Endpoint: {session_data['sandboxEndpoint']}")

        if SKIP_LLM_TESTS:
            print("    SKIP_LLM_TESTS=1, skipping RPC/SSE test")
            return

        # Start SSE listener in background
        events: list[dict] = []
        stop_event = threading.Event()
        sse_thread = threading.Thread(
            target=collect_sse_in_thread,
            args=(session_id, events, stop_event),
            kwargs={"timeout": 90},
        )
        sse_thread.start()
        time.sleep(1)  # Let SSE connect

        # Start a task via RPC proxy
        rpc_resp = send_rpc(
            client,
            session_id,
            "StartTask",
            {
                "taskId": "task-lifecycle-1",
                "prompt": "Say hello in one sentence. Do not use any tools.",
                "taskOptions": {"maxSteps": 5},
            },
        )
        print(f"    RPC response: {json.dumps(rpc_resp)[:200]}")

        if "error" in rpc_resp:
            raise TestFailureError(f"StartTask RPC error: {rpc_resp['error']}")

        # Wait for task completion or timeout, printing progress
        print("    Waiting for SSE events...")
        deadline = time.time() + 60
        last_count = 0
        while time.time() < deadline:
            # Print new events as they arrive
            if len(events) > last_count:
                for e in events[last_count:]:
                    data = e.get("data", {})
                    if isinstance(data, dict):
                        et = data.get("eventType", "?")
                        detail = ""
                        if et in ("llm_chunk", "LlmChunk"):
                            detail = f" chunk={data.get('text', data.get('chunk', ''))!r}"
                        elif et in ("task_completed", "task_failed", "TaskCompleted", "TaskFailed"):
                            detail = f" status={data.get('status', '?')}"
                        elif et in ("llm_response", "LlmResponse"):
                            usage = data.get("usage", data.get("tokenUsage", {}))
                            detail = f" tokens={usage}"
                        elif et in ("step_completed", "StepCompleted"):
                            detail = f" step={data.get('stepIndex', '?')}"
                        elif "error" in str(et).lower() or "fail" in str(et).lower():
                            detail = f" error={json.dumps(data)[:200]}"
                        print(f"      Event: {et}{detail}")
                    else:
                        print(f"      Event: {e.get('event', '?')} data={str(data)[:100]}")
                last_count = len(events)

            # Check if we got a terminal event
            for e in events:
                if isinstance(e.get("data"), dict):
                    et = e["data"].get("eventType", "")
                    if et in ("task_completed", "task_failed", "TaskCompleted", "TaskFailed"):
                        break
            else:
                time.sleep(1)
                continue
            break
        stop_event.set()
        sse_thread.join(timeout=5)

        elapsed = time.time() - deadline + 60
        event_types = [
            e.get("data", {}).get("eventType", e.get("event", "?"))
            if isinstance(e.get("data"), dict)
            else e.get("event", "?")
            for e in events
        ]
        print(f"    SSE events received: {len(events)} in {elapsed:.1f}s")
        print(f"    Event types: {event_types}")

        if not events:
            raise TestFailureError("No SSE events received")

    finally:
        if stop_event is not None:
            stop_event.set()
        if sse_thread is not None:
            sse_thread.join(timeout=5)
        if session_id:
            cancel_session(client, session_id)
        client.close()


def test_sse_reconnect():
    """SSE reconnect: disconnect, reconnect with Last-Event-ID, verify replay."""
    client = httpx.Client()
    session_id = None
    try:
        data = create_sandbox_session(client)
        session_id = data["sessionId"]
        poll_session_status(client, session_id, {"SANDBOX_READY", "SESSION_RUNNING"})

        if SKIP_LLM_TESTS:
            print("    SKIP_LLM_TESTS=1, skipping SSE reconnect test")
            return

        # Start a task to generate events
        send_rpc(
            client,
            session_id,
            "StartTask",
            {
                "taskId": "task-reconnect-1",
                "prompt": "Say 'reconnect test complete' in one sentence. Do not use tools.",
                "taskOptions": {"maxSteps": 5},
            },
        )

        # Collect first batch of events
        events_batch1: list[dict] = []
        stop1 = threading.Event()
        t1 = threading.Thread(
            target=collect_sse_in_thread,
            args=(session_id, events_batch1, stop1),
            kwargs={"max_events": 3, "timeout": 30},
        )
        t1.start()
        t1.join(timeout=35)
        stop1.set()

        if not events_batch1:
            raise TestFailureError("No events in first SSE batch")

        last_id = events_batch1[-1].get("id")
        print(f"    First batch: {len(events_batch1)} events, last_id={last_id}")

        if not last_id:
            raise TestFailureError("Events don't have IDs for reconnect")

        # Wait for more events to accumulate
        time.sleep(5)

        # Reconnect with Last-Event-ID
        events_batch2: list[dict] = []
        stop2 = threading.Event()
        t2 = threading.Thread(
            target=collect_sse_in_thread,
            args=(session_id, events_batch2, stop2),
            kwargs={"last_event_id": last_id, "max_events": 20, "timeout": 15},
        )
        t2.start()
        t2.join(timeout=20)
        stop2.set()

        print(f"    Reconnect batch: {len(events_batch2)} events")

        # Verify we got events after the last_id (replay)
        if not events_batch2:
            # May be OK if all events already delivered in batch 1
            print("    WARN: No events on reconnect (task may have completed before)")
        else:
            # Verify no duplicate IDs from batch 1
            batch1_ids = {e.get("id") for e in events_batch1}
            batch2_ids = {e.get("id") for e in events_batch2}
            overlap = batch1_ids & batch2_ids
            if overlap:
                raise TestFailureError(f"Duplicate event IDs on reconnect: {overlap}")
            print("    No duplicate events on reconnect — replay works correctly")

    finally:
        if session_id:
            cancel_session(client, session_id)
        client.close()


def test_file_upload_download():
    """File upload/download through proxy: upload → download → verify content."""
    client = httpx.Client()
    session_id = None
    try:
        data = create_sandbox_session(client)
        session_id = data["sessionId"]
        poll_session_status(client, session_id, {"SANDBOX_READY", "SESSION_RUNNING"})

        # Give sandbox HTTP server a moment to be fully ready
        time.sleep(2)

        test_content = b"Hello from test_file_upload_download!\nLine 2.\n"
        test_filename = "test-upload.txt"

        # Upload file (retry on 503 — sandbox may still be starting HTTP server)
        for attempt in range(3):
            resp = client.post(
                f"{SESSION_SERVICE_URL}/sessions/{session_id}/upload",
                files={"file": (test_filename, test_content, "text/plain")},
                headers={"X-User-Id": USER_ID},
                timeout=30,
            )
            if resp.status_code != 503 or attempt == 2:
                break
            print(f"    Upload got 503, retrying ({attempt + 1}/3)...")
            time.sleep(2)
        if resp.status_code >= 400:
            raise TestFailureError(f"Upload returned {resp.status_code}: {resp.text[:300]}")
        print(f"    Upload response: {resp.status_code}")

        # Download file
        resp = client.get(
            f"{SESSION_SERVICE_URL}/sessions/{session_id}/files/{test_filename}",
            headers={"X-User-Id": USER_ID},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise TestFailureError(f"Download returned {resp.status_code}: {resp.text[:300]}")
        downloaded = resp.content
        print(f"    Download response: {resp.status_code}, size={len(downloaded)}")

        if downloaded != test_content:
            raise TestFailureError(
                f"Content mismatch!\n  Uploaded: {test_content!r}\n  Downloaded: {downloaded!r}"
            )
        print("    Content integrity verified")

        # List files
        resp = client.get(
            f"{SESSION_SERVICE_URL}/sessions/{session_id}/files",
            headers={"X-User-Id": USER_ID},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise TestFailureError(f"File list returned {resp.status_code}: {resp.text[:300]}")
        file_list = resp.json()
        print(f"    File listing: {file_list}")

        # Verify our file appears in the listing
        if isinstance(file_list, list):
            filenames = [f.get("name", f) if isinstance(f, dict) else str(f) for f in file_list]
            if test_filename not in filenames:
                raise TestFailureError(
                    f"Uploaded file '{test_filename}' not in listing: {filenames}"
                )
            print(f"    File '{test_filename}' found in listing")

    finally:
        if session_id:
            cancel_session(client, session_id)
        client.close()


def test_idle_timeout():
    """Idle timeout: create sandbox, don't send activity, verify SANDBOX_TERMINATED.

    Requires session-service started with:
      SANDBOX_IDLE_TIMEOUT_SECONDS=10
      SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS=2
    """
    client = httpx.Client()
    session_id = None
    try:
        data = create_sandbox_session(client)
        session_id = data["sessionId"]
        session_data = poll_session_status(client, session_id, {"SANDBOX_READY", "SESSION_RUNNING"})
        print(f"    Session ready: {session_data['status']}")

        # Don't send any activity — wait for idle timeout
        print("    Waiting for idle timeout (up to 60s)...")
        try:
            terminated = poll_session_status(
                client,
                session_id,
                {"SANDBOX_TERMINATED", "SESSION_FAILED"},
                timeout=60,
            )
            print(f"    Session terminated: {terminated['status']}")
            if terminated["status"] == "SANDBOX_TERMINATED":
                print("    Idle timeout works correctly")
            else:
                print(f"    Session ended with: {terminated['status']}")
        except TestFailureError:
            # Check current status
            current = get_session(client, session_id)
            status = current.get("status", "?")
            if status in {"SANDBOX_READY", "SESSION_RUNNING"}:
                raise TestFailureError(
                    f"Session still {status} after 60s — is SANDBOX_IDLE_TIMEOUT_SECONDS "
                    "set to a short value (e.g. 10)? Is SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS "
                    "set to a short value (e.g. 2)?"
                ) from None
            raise

    finally:
        if session_id:
            cancel_session(client, session_id)
        client.close()


def test_provisioning_timeout():
    """Provisioning timeout: session stuck in SANDBOX_PROVISIONING → SESSION_FAILED.

    This test verifies the lifecycle manager cleans up sessions where the sandbox
    subprocess fails to register. Requires session-service started with:
      SANDBOX_PROVISION_TIMEOUT_SECONDS=5
      SANDBOX_LIFECYCLE_CHECK_INTERVAL_SECONDS=2

    Note: With LocalSandboxLauncher, the subprocess usually registers quickly.
    If it does, this test verifies the happy path instead and logs a note.
    """
    client = httpx.Client()
    session_id = None
    try:
        data = create_sandbox_session(client)
        session_id = data["sessionId"]
        status = data.get("status")
        print(f"    Initial status: {status}")

        if status != "SANDBOX_PROVISIONING":
            print(f"    NOTE: Session already past provisioning ({status})")
            print("    Provisioning timeout cannot be tested — subprocess registered too fast")
            return

        # Poll — either it registers quickly (SANDBOX_READY) or times out (SESSION_FAILED)
        print("    Polling for status change (up to 30s)...")
        try:
            result = poll_session_status(
                client,
                session_id,
                {"SANDBOX_READY", "SESSION_RUNNING", "SESSION_FAILED"},
                timeout=30,
            )
            final_status = result.get("status")
            print(f"    Final status: {final_status}")

            if final_status == "SESSION_FAILED":
                print("    Provisioning timeout works correctly")
            else:
                print(
                    f"    NOTE: Subprocess registered successfully ({final_status}). "
                    "To test provisioning timeout, use an invalid AGENT_RUNTIME_PATH "
                    "or set SANDBOX_PROVISION_TIMEOUT_SECONDS=1"
                )
        except TestFailureError:
            current = get_session(client, session_id)
            raise TestFailureError(
                f"Session stuck in {current.get('status')} — neither registered nor timed out"
            ) from None

    finally:
        if session_id:
            cancel_session(client, session_id)
        client.close()


# --- Main ---


def main():
    print("=" * 60)
    print("Web Sandbox E2E Integration Test")
    print("=" * 60)
    print(f"  Session Service: {SESSION_SERVICE_URL}")
    print(f"  Skip LLM tests: {SKIP_LLM_TESTS}")

    # Check session-service is reachable
    try:
        resp = httpx.get(f"{SESSION_SERVICE_URL}/health", timeout=5)
        if resp.status_code != 200:
            print(f"FAIL: Session service health check returned {resp.status_code}")
            sys.exit(1)
        print("  Session service: healthy")
    except httpx.ConnectError:
        print(f"FAIL: Cannot reach session service at {SESSION_SERVICE_URL}")
        print("  Start it with: make run  (with SANDBOX_LAUNCHER_TYPE=local)")
        sys.exit(1)

    scenarios = [
        ("Full Lifecycle", test_full_lifecycle),
        ("File Upload/Download", test_file_upload_download),
        ("SSE Reconnect", test_sse_reconnect),
        ("Idle Timeout", test_idle_timeout),
        ("Provisioning Timeout", test_provisioning_timeout),
    ]

    total = len(scenarios)
    passed = 0
    failed = 0
    results: list[tuple[str, bool, str | None]] = []

    for name, test_fn in scenarios:
        print(f"\n  [{len(results) + 1}/{total}] {name}...")
        try:
            test_fn()
            print(f"    PASS: {name}")
            passed += 1
            results.append((name, True, None))
        except TestFailureError as e:
            print(f"    FAIL: {name} -- {e}")
            failed += 1
            results.append((name, False, str(e)))
        except Exception as e:
            print(f"    FAIL: {name} -- unexpected: {e}")
            failed += 1
            results.append((name, False, str(e)))

    # Summary
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed, {total} total")
    print("=" * 60)
    for name, ok, err in results:
        status = "PASS" if ok else "FAIL"
        suffix = f" -- {err}" if err else ""
        print(f"  [{status}] {name}{suffix}")
    print("=" * 60)

    if failed > 0:
        print("\nWEB SANDBOX TEST FAILED")
        sys.exit(1)
    else:
        print("\nWEB SANDBOX TEST PASSED")


if __name__ == "__main__":
    main()
