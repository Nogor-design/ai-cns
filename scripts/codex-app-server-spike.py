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
import time
from pathlib import Path

from cortex.codex_app import Client


def run(cwd: Path, *, resume_thread: str | None, ephemeral_start: bool) -> dict[str, Any]:
    client = Client(timeout=20.0)
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
