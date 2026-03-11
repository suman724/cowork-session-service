"""Local subprocess sandbox launcher for development."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Any

import structlog

from session_service.config import Settings
from session_service.exceptions import SandboxProvisionError
from session_service.services.sandbox_launcher import LaunchResult

logger = structlog.get_logger()

# Track spawned subprocesses for cleanup
_processes: dict[str, subprocess.Popen[bytes]] = {}
# Track log-forwarding threads for cleanup
_log_threads: dict[str, threading.Thread] = {}


def _find_free_port() -> int:
    """Find a free port by binding to port 0 and immediately releasing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def _stream_subprocess_logs(proc: subprocess.Popen[bytes], session_id: str) -> None:
    """Forward subprocess stderr to structlog in a background thread."""
    if not proc.stderr:
        return
    for raw_line in proc.stderr:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if line:
            logger.info("sandbox_log", session_id=session_id, message=line)


class LocalSandboxLauncher:
    """Spawns agent-runtime as a local subprocess for development."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def launch(self, session_id: str, env_vars: dict[str, str]) -> LaunchResult:
        """Spawn agent-runtime subprocess on a random free port."""
        port = _find_free_port()
        endpoint = f"http://localhost:{port}"

        workspace_dir = os.path.join(tempfile.gettempdir(), "cowork-sandbox", session_id)
        os.makedirs(workspace_dir, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "agent_host.main",
            "--transport",
            "http",
            "--port",
            str(port),
            "--workspace-dir",
            workspace_dir,
        ]

        proc_env = os.environ.copy()
        proc_env.update(env_vars)
        proc_env["SANDBOX_LOCAL_MODE"] = "true"

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=self._settings.agent_runtime_path,
                env=proc_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(
                "local_launcher_spawn_failed",
                session_id=session_id,
                error=str(exc),
            )
            raise SandboxProvisionError(f"Failed to spawn agent-runtime: {exc}") from exc

        task_id = f"local:{proc.pid}"
        _processes[task_id] = proc

        # Forward subprocess stderr to structlog in background
        log_thread = threading.Thread(
            target=_stream_subprocess_logs,
            args=(proc, session_id),
            daemon=True,
        )
        log_thread.start()
        _log_threads[task_id] = log_thread

        logger.info(
            "local_sandbox_launched",
            session_id=session_id,
            pid=proc.pid,
            port=port,
            task_id=task_id,
        )

        return LaunchResult(task_id=task_id, endpoint_hint=endpoint)

    async def stop(self, task_id: str) -> None:
        """Send SIGTERM to subprocess, wait up to 10s, then SIGKILL."""
        proc = _processes.pop(task_id, None)
        if proc is None:
            logger.warning("local_sandbox_not_found", task_id=task_id)
            return

        proc.terminate()
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, proc.wait),
                timeout=10,
            )
        except TimeoutError:
            logger.warning("local_sandbox_force_kill", task_id=task_id)
            proc.kill()
            await asyncio.get_event_loop().run_in_executor(None, proc.wait)

        # Wait for log thread to finish draining
        log_thread = _log_threads.pop(task_id, None)
        if log_thread:
            log_thread.join(timeout=2)

        _close_proc_fds(proc)
        logger.info("local_sandbox_stopped", task_id=task_id)

    async def is_healthy(self, task_id: str) -> bool:
        """Check if subprocess is alive."""
        proc = _processes.get(task_id)
        return proc is not None and proc.poll() is None


def _close_proc_fds(proc: Any) -> None:
    """Close stdout/stderr file descriptors to prevent leaks."""
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
