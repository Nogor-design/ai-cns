"""Local web dashboard and JSON API for the Cortex portfolio.

The server intentionally uses the Python standard library and binds to
``127.0.0.1`` by default. SQLite remains the only source of truth; the React
frontend is a management surface over the same records used by the CLI.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import config, db, health, routing, store, workers

WORKER_NAMES = ("codex", "claude", "gemini", "grok", "ollama", "perplexity")
ACTIVE_TASK_STATUSES = {"open", "assigned", "in_progress", "running", "review", "blocked"}
TASK_MUTABLE_FIELDS = {
    "title", "type", "status", "brief", "risk", "complexity", "acceptance",
    "allowed_paths", "budget", "priority", "assignee", "due_at",
}
PROJECT_MUTABLE_FIELDS = {
    "status", "program", "priority", "privacy", "state_mode", "current_goal",
    "test_command",
}


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def portfolio_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the complete, evidence-backed dashboard snapshot."""
    all_project_rows = store.list_projects(conn)
    project_rows = [row for row in all_project_rows if row["status"] != "archived"]
    projects_by_id = {row["id"]: row for row in project_rows}
    health_rows = {
        row.project_id: row
        for row in health.inspect_portfolio(
            conn, include_paused=True, live_git=False
        )
    }
    task_rows = [
        row for row in store.list_all_tasks(conn, include_done=False)
        if row["project_id"] in projects_by_id
    ]

    task_payloads: list[dict[str, Any]] = []
    project_task_index: dict[str, list[dict[str, Any]]] = {
        row["id"]: [] for row in project_rows
    }
    recommended_counts = {name: 0 for name in WORKER_NAMES}
    assigned_counts: dict[str, int] = {}

    for task in task_rows:
        project = projects_by_id[task["project_id"]]
        route = routing.route_task(project, task)
        item = _row_dict(task)
        item["route"] = asdict(route)
        item["recommended_worker"] = route.worker
        item["recommended_model"] = route.model
        task_payloads.append(item)
        project_task_index[task["project_id"]].append(item)
        if task["assignee"]:
            key = str(task["assignee"]).lower()
            assigned_counts[key] = assigned_counts.get(key, 0) + 1
        elif task["status"] in {"open", "assigned"}:
            recommended_counts[route.worker] = recommended_counts.get(route.worker, 0) + 1

    project_payloads: list[dict[str, Any]] = []
    for project in project_rows:
        snapshot = health_rows[project["id"]]
        item = snapshot.to_dict()
        item.update(
            current_goal=project["current_goal"],
            stack=project["stack"],
            test_command=project["test_command"],
            updated_at=project["updated_at"],
        )
        active_tasks = project_task_index[project["id"]]
        item["tasks"] = active_tasks
        item["task_count"] = len(active_tasks)
        item["assignees"] = sorted(
            {str(task["assignee"]) for task in active_tasks if task["assignee"]}
        )
        item["recommendations"] = sorted(
            {
                task["recommended_worker"]
                for task in active_tasks
                if not task["assignee"] and task["status"] == "open"
            }
        )
        project_payloads.append(item)

    active_tasks = [
        task for task in task_payloads
        if task["status"] in ACTIVE_TASK_STATUSES
        and projects_by_id[task["project_id"]]["status"] == "active"
    ]
    summary = {
        "active_projects": sum(1 for row in project_rows if row["status"] == "active"),
        "paused_projects": sum(1 for row in project_rows if row["status"] == "paused"),
        "archived_projects": sum(1 for row in all_project_rows if row["status"] == "archived"),
        "needs_review": sum(1 for task in active_tasks if task["status"] == "review"),
        "blocked": sum(1 for task in active_tasks if task["status"] == "blocked"),
        "unassigned": sum(
            1 for task in active_tasks
            if task["status"] == "open" and not task["assignee"]
        ),
        "local_queue": sum(
            1 for task in active_tasks if task["route"]["worker"] == "ollama"
        ),
        "cloud_queue": sum(
            1 for task in active_tasks if task["route"]["worker"] != "ollama"
        ),
        "attention_projects": sum(
            1 for project in project_payloads
            if project["status"] == "active" and project["health"] != "clean"
        ),
    }

    worker_payloads = []
    for name in WORKER_NAMES:
        if name == "perplexity":
            availability = "manual"
        else:
            availability = "ready" if workers.available(name) else "missing"
        worker_payloads.append(
            {
                "name": name,
                "availability": availability,
                "assigned": assigned_counts.get(name, 0),
                "recommended": recommended_counts.get(name, 0),
            }
        )

    return {
        "generated_at": _now_iso(),
        "database": str(config.db_path()),
        "summary": summary,
        "projects": project_payloads,
        "tasks": task_payloads,
        "workers": worker_payloads,
    }


def _now_iso() -> str:
    from .ids import now

    return now()


class CortexDashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        database_path: Path,
        static_root: Path,
    ) -> None:
        super().__init__(address, handler)
        self.database_path = database_path
        self.static_root = static_root


class DashboardHandler(BaseHTTPRequestHandler):
    server: CortexDashboardServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"dashboard: {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/portfolio":
            with db.connect(self.server.database_path) as conn:
                self._json(HTTPStatus.OK, portfolio_payload(conn))
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "database": str(self.server.database_path)})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/tasks":
            self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            return
        try:
            body = self._body()
            project_id = str(body.pop("project_id"))
            title = str(body.pop("title")).strip()
            if not title:
                raise ValueError("title is required")
            allowed = {
                key: value for key, value in body.items()
                if key in TASK_MUTABLE_FIELDS - {"status", "title"}
            }
            with db.connect(self.server.database_path) as conn:
                store.get_project(conn, project_id)
                task_id = store.create_task(
                    conn, project_id=project_id, title=title, **allowed
                )
                if body.get("status"):
                    store.update_task(conn, task_id, status=body["status"])
                task = _row_dict(store.get_task(conn, task_id))
            self._json(HTTPStatus.CREATED, {"task": task})
        except (KeyError, TypeError, ValueError, store.NotFound) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PATCH(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "api":
            self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            return
        kind, item_id = parts[1], parts[2]
        try:
            body = self._body()
            with db.connect(self.server.database_path) as conn:
                if kind == "tasks":
                    store.get_task(conn, item_id)
                    fields = {key: value for key, value in body.items() if key in TASK_MUTABLE_FIELDS}
                    store.update_task(conn, item_id, **fields)
                    payload = {"task": _row_dict(store.get_task(conn, item_id))}
                elif kind == "projects":
                    store.get_project(conn, item_id)
                    fields = {key: value for key, value in body.items() if key in PROJECT_MUTABLE_FIELDS}
                    store.update_project(conn, item_id, **fields)
                    payload = {"project": _row_dict(store.get_project(conn, item_id))}
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                    return
            self._json(HTTPStatus.OK, payload)
        except (TypeError, ValueError, store.NotFound) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be JSON under 1 MB")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("JSON body must be an object")
        return payload

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, requested_path: str) -> None:
        root = self.server.static_root.resolve()
        relative = requested_path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "dashboard build is missing; run npm install && npm run build in dashboard"},
            )
            return
        data = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    database_path: Path | None = None,
    static_root: Path | None = None,
) -> None:
    database_path = database_path or config.db_path()
    static_root = static_root or Path(__file__).resolve().parent.parent / "dashboard" / "dist"
    server = CortexDashboardServer(
        (host, port), DashboardHandler,
        database_path=database_path,
        static_root=static_root,
    )
    url = f"http://{host}:{port}"
    print(f"Cortex Portfolio: {url}")
    print(f"Database: {database_path}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
