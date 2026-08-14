"""Read-only/safe compatibility probe for the local Codex App Server.

By default this initializes the protocol and lists threads whose recorded cwd
exactly matches --cwd. Optional resume is metadata-only (no turn is started).
Optional start always creates an ephemeral, non-persisted thread and never
submits a prompt, so the probe cannot create sidebar clutter or spend model
tokens.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class AppServerError(RuntimeError):
    pass


class AppServerProbe:
    def __init__(self, timeout: float = 20.0) -> None:
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
                raise AppServerError(f"timed out waiting for {method}") from exc
            message = json.loads(raw)
            if message.get("id") != request_id:
                self.notifications.append(message)
                continue
            if "error" in message:
                error = message["error"]
                raise AppServerError(
                    f"{method} failed ({error.get('code')}): {error.get('message')}"
                )
            return message["result"]
        raise AppServerError(f"timed out waiting for {method}")

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()


def run(cwd: Path, *, resume_thread: str | None, ephemeral_start: bool) -> dict[str, Any]:
    client = AppServerProbe()
    try:
        initialized = client.request("initialize", {
            "clientInfo": {"name": "cortex-app-server-spike", "version": "0.1.0"},
        })
        client.notify("initialized")
        listed = client.request("thread/list", {"cwd": str(cwd), "limit": 100})
        threads = listed["data"]
        result: dict[str, Any] = {
            "server": initialized,
            "cwd": str(cwd),
            "list_by_cwd": {
                "count": len(threads),
                "all_exact_match": all(
                    Path(thread["cwd"]).resolve() == cwd.resolve() for thread in threads
                ),
                "threads": [
                    {
                        "id": thread["id"],
                        "name": thread.get("name"),
                        "status": thread.get("status"),
                        "cwd": thread["cwd"],
                    }
                    for thread in threads
                ],
            },
        }
        if resume_thread:
            resumed = client.request("thread/resume", {"threadId": resume_thread})
            result["resume"] = {
                "id": resumed["thread"]["id"],
                "cwd": resumed["cwd"],
                "turn_count": len(resumed["thread"]["turns"]),
            }
        if ephemeral_start:
            started = client.request(
                "thread/start", {"cwd": str(cwd), "ephemeral": True}
            )
            result["ephemeral_start"] = {
                "id": started["thread"]["id"],
                "cwd": started["cwd"],
                "ephemeral": started["thread"]["ephemeral"],
            }
        time.sleep(0.2)
        while not client._messages.empty():
            client.notifications.append(json.loads(client._messages.get()))
        result["notifications"] = sorted({
            message.get("method")
            for message in client.notifications
            if message.get("method")
        })
        return result
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--resume-thread")
    parser.add_argument(
        "--ephemeral-start",
        action="store_true",
        help="Start a non-persisted empty thread; never submits a model turn.",
    )
    args = parser.parse_args()
    print(json.dumps(
        run(args.cwd.resolve(), resume_thread=args.resume_thread,
            ephemeral_start=args.ephemeral_start),
        indent=2,
    ))


if __name__ == "__main__":
    main()
