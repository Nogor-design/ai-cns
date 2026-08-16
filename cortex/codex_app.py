"""Safe, read-only Codex App Server capability inspection and launch prompts."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any


class CodexAppError(RuntimeError):
    pass


class Client:
    """Small JSONL client used only for protocol initialization and thread listing."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.notifications: list[dict[str, Any]] = []
        self._messages: queue.Queue[str] = queue.Queue()
        self._next_id = 1
        self._process = subprocess.Popen(
            ["codex", "app-server", "--stdio", "--disable", "plugins"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._messages.put(line)

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        })
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                raw = self._messages.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise CodexAppError(f"timed out waiting for {method}") from exc
            message = json.loads(raw)
            if message.get("id") != request_id:
                self.notifications.append(message)
                continue
            if "error" in message:
                error = message["error"]
                raise CodexAppError(
                    f"{method} failed ({error.get('code')}): {error.get('message')}"
                )
            return message["result"]
        raise CodexAppError(f"timed out waiting for {method}")

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


def _supported_methods(timeout: float = 10.0) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="cortex-codex-schema-") as directory:
        proc = subprocess.run(
            [
                "codex", "app-server", "generate-json-schema", "--experimental",
                "--out", directory,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise CodexAppError(proc.stderr[-500:] or "could not inspect App Server schema")
        request_schema = Path(directory) / "ClientRequest.json"
        text = request_schema.read_text(encoding="utf-8")
    candidates = {
        "thread/list", "thread/read", "thread/start", "thread/resume", "turn/start",
    }
    return {method for method in candidates if f'"{method}"' in text}


_INSPECT_CACHE: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}
_INSPECT_INFLIGHT: dict[tuple[str, str | None], threading.Event] = {}
_INSPECT_LOCK = threading.Lock()


def _inspect_uncached(cwd: str, linked_thread_id: str | None) -> dict[str, Any]:
    resolved = str(Path(cwd).resolve())
    if shutil.which("codex") is None:
        return {
            "available": False,
            "reason": "Codex CLI is not installed",
            "methods": {},
            "threads": [],
            "linked_thread_found": False,
        }
    client: Client | None = None
    try:
        methods = _supported_methods()
        client = Client()
        initialized = client.request(
            "initialize",
            {"clientInfo": {"name": "cortex-dashboard", "version": "0.1.0"}},
        )
        client.notify("initialized")
        listed = client.request("thread/list", {
            "cwd": resolved,
            "limit": 100,
            "useStateDbOnly": True,
            "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        })
        threads = [
            {
                "id": thread["id"],
                "name": thread.get("name"),
                "status": thread.get("status"),
                "cwd": thread["cwd"],
            }
            for thread in listed["data"]
        ]
        return {
            "available": True,
            "reason": None,
            "server": initialized.get("userAgent"),
            "methods": {method: method in methods for method in sorted({
                "thread/list", "thread/read", "thread/start", "thread/resume",
                "turn/start",
            })},
            "threads": threads,
            "linked_thread_found": bool(
                linked_thread_id
                and any(thread["id"] == linked_thread_id for thread in threads)
            ),
        }
    except (OSError, subprocess.SubprocessError, CodexAppError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "reason": str(exc),
            "methods": {},
            "threads": [],
            "linked_thread_found": False,
        }
    finally:
        if client is not None:
            client.close()


def inspect(
    cwd: str | Path,
    linked_thread_id: str | None = None,
    *,
    max_age: float = 30.0,
) -> dict[str, Any]:
    """Single-flight capability inspection; never creates/resumes a task or turn."""
    resolved = str(Path(cwd).resolve())
    key = (resolved.casefold(), linked_thread_id)
    leader = False
    with _INSPECT_LOCK:
        cached = _INSPECT_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return deepcopy(cached[1])
        event = _INSPECT_INFLIGHT.get(key)
        if event is None:
            event = threading.Event()
            _INSPECT_INFLIGHT[key] = event
            leader = True
    if not leader:
        event.wait(timeout=15)
        with _INSPECT_LOCK:
            cached = _INSPECT_CACHE.get(key)
            if cached:
                return deepcopy(cached[1])
        return {
            "available": False,
            "reason": "Codex capability check timed out",
            "methods": {},
            "threads": [],
            "linked_thread_found": False,
        }
    try:
        result = _inspect_uncached(resolved, linked_thread_id)
        with _INSPECT_LOCK:
            _INSPECT_CACHE[key] = (time.monotonic(), result)
        return deepcopy(result)
    finally:
        with _INSPECT_LOCK:
            event = _INSPECT_INFLIGHT.pop(key, None)
            if event:
                event.set()


def launch_prompt(project: Any, task: Any) -> str:
    """Build the exact bounded handoff shown before any Codex work is started."""
    allowed_paths = task["allowed_paths"] or "No additional path restriction recorded"
    return f"""Continue Cortex work item {task['id']} in {project['name']}.

Project ID: {project['id']}
Repository: {project['repo_path']}
PM session: {task['pm_session_id'] or 'Not linked'}
Outcome: {task['title']}
Current project goal: {project['current_goal'] or 'Not recorded'}

Task instructions:
{task['brief'] or 'Use the outcome and done-when check as the bounded scope.'}

Done when:
{task['acceptance'] or 'Return concrete evidence for owner review.'}

Allowed paths:
{allowed_paths}

Risk: {task['risk']}
Next action: {task['next_action'] or 'Inspect current repository truth and execute the smallest safe slice.'}
Test command: {project['test_command'] or 'Use the repository test instructions.'}

Work only inside the registered repository and allowed paths. Preserve unrelated
changes. Continue this existing Cortex work item; do not create a duplicate.
Run relevant tests, report files changed and evidence, and do not merge, deploy,
or perform external business actions without explicit owner authority."""
