"""Local web dashboard and JSON API for the Cortex portfolio.

The server intentionally uses the Python standard library and binds to
``127.0.0.1`` by default. SQLite remains the only source of truth; the React
frontend is a management surface over the same records used by the CLI.
"""

from __future__ import annotations

import json
import hmac
import mimetypes
import os
import secrets
import sqlite3
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import (
    codex_app, config, db, dispatcher, git_monitor, health, ids, jobs, pm, policy,
    github_adapter, github_reader, routing, runlog, secrets_scan, store, team, workers,
)

WORKER_NAMES = ("codex", "claude", "gemini", "grok", "ollama", "perplexity")
ACTIVE_TASK_STATUSES = {"open", "assigned", "in_progress", "running", "review", "blocked"}
ROADMAP_TASK_FIELDS = (
    "id", "project_id", "project_name", "project_program", "project_status",
    "title", "status", "priority", "assignee", "milestone", "progress",
    "start_at", "target_at", "due_at", "created_at", "updated_at",
    "completed_at", "blocked_reason", "next_action", "parent_id",
)
# Stable GitHub node IDs and ``sync_state`` are verified adapter evidence. They
# are intentionally absent here so a dashboard form cannot forge concurrency
# history or suppress the mirror planner's conflict detection.
TASK_MUTABLE_FIELDS = {
    "title", "type", "status", "brief", "risk", "complexity", "acceptance",
    "allowed_paths", "budget", "priority", "assignee", "requested_model", "effort", "due_at",
    "parent_id", "milestone", "start_at", "target_at", "progress",
    "blocked_reason", "next_action", "github_issue_url", "codex_thread_id",
}
PROJECT_MUTABLE_FIELDS = {
    "status", "program", "priority", "privacy", "state_mode", "current_goal",
    "test_command", "allowed_workers", "remote_url", "github_owner", "github_repo",
    "codex_project_id",
}


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _activity_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = _row_dict(row)
    if item.get("evidence_json"):
        try:
            item["evidence"] = json.loads(item["evidence_json"])
        except (TypeError, json.JSONDecodeError):
            item["evidence"] = item["evidence_json"]
    else:
        item["evidence"] = None
    return item


def _mirror_operation_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = _row_dict(row)
    for source, target in (
        ("actions_json", "actions"),
        ("completed_actions_json", "completed_actions"),
        ("evidence_json", "evidence"),
    ):
        value = item.get(source)
        if value:
            try:
                item[target] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                item[target] = None
        else:
            item[target] = [] if target != "evidence" else None
    return item


def _mirror_apply_command(task_id: str, fingerprint: str, operation_id: str) -> str:
    return (
        f".\\scripts\\cortex-portfolio.ps1 github apply {task_id} "
        f"--approve {fingerprint} --operation-id {operation_id}"
    )


def github_mirror_preview(
    conn: sqlite3.Connection, task_id: str
) -> dict[str, Any]:
    """Build an explicit-click, read-only GitHub mirror readiness preview."""
    task = store.get_task(conn, task_id)
    project = store.get_project(conn, task["project_id"])
    latest_row = store.latest_github_mirror_operation(conn, task["id"])
    latest = _mirror_operation_dict(latest_row)
    owner = project["github_project_owner"]
    number = project["github_project_number"]
    base: dict[str, Any] = {
        "mode": "read_only_preview",
        "task_id": task["id"],
        "project_id": project["id"],
        "can_apply": False,
        "dashboard_apply_enabled": False,
        "target": None,
        "issue": {
            "id": task["github_issue_id"],
            "number": task["github_issue_number"],
            "url": task["github_issue_url"],
        },
        "plan": None,
        "operation": latest,
        "command": None,
        "command_kind": None,
        "note": (
            "This on-demand preview only reads GitHub. The dashboard cannot apply "
            "the plan; copying a command does not run it."
        ),
    }
    if not owner or number is None or not project["github_project_id"]:
        base.update(
            status="unconfigured",
            summary="This Cortex project has no verified GitHub Project target.",
        )
        return base
    base["target"] = {
        "owner": str(owner),
        "number": int(number),
        "project_id": project["github_project_id"],
        "title": None,
    }
    if not task["github_issue_id"]:
        base.update(
            status="unlinked",
            summary="Link one explicit existing GitHub issue before planning a mirror update.",
        )
        return base

    recoverable = latest and latest["status"] in {"applying", "interrupted"}
    try:
        plan, snapshot = github_adapter.prepare(task, str(owner), int(number))
    except (github_reader.GitHubProjectError, github_adapter.MirrorApplyError) as exc:
        if not recoverable:
            raise
        base.update(
            status="recoverable",
            summary=(
                f"Mirror operation {latest['operation_id']} can be resumed, but the "
                "fresh GitHub read is currently unavailable."
            ),
            command=_mirror_apply_command(
                task["id"], latest["plan_fingerprint"], latest["operation_id"]
            ),
            command_kind="resume",
            read_error=str(exc),
        )
        return base
    if snapshot["project_id"] != project["github_project_id"]:
        raise ValueError("configured GitHub Project node ID changed")
    base["target"]["title"] = snapshot.get("project_title")
    base["plan"] = plan.as_dict()
    remote_item = next(
        (
            item for item in snapshot.get("items", [])
            if item.get("content_id") == task["github_issue_id"]
        ),
        None,
    )
    if remote_item:
        content = remote_item.get("content") or {}
        base["issue"].update(
            title=content.get("title"),
            state=content.get("state"),
            url=content.get("url") or task["github_issue_url"],
        )

    if recoverable:
        base.update(
            status="recoverable",
            summary=(
                f"Mirror operation {latest['operation_id']} can be safely resumed "
                "with its original approval."
            ),
            command=_mirror_apply_command(
                task["id"], latest["plan_fingerprint"], latest["operation_id"]
            ),
            command_kind="resume",
        )
    elif not plan.safe_to_apply:
        base.update(
            status="conflict",
            summary="GitHub or Cortex evidence changed; review the conflicts before applying.",
        )
    elif plan.actions:
        operation_id = github_adapter.suggested_operation_id(plan.fingerprint)
        base.update(
            status="actions_required",
            summary=f"{len(plan.actions)} verified mirror action(s) are ready for CLI approval.",
            command=_mirror_apply_command(task["id"], plan.fingerprint, operation_id),
            command_kind="apply",
        )
    else:
        base.update(
            status="converged",
            summary="Cortex and the linked GitHub Project item are already in sync.",
        )
    return base


def portfolio_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the complete, evidence-backed dashboard snapshot."""
    all_project_rows = store.list_projects(conn)
    project_rows = [row for row in all_project_rows if row["status"] != "archived"]
    projects_by_id = {row["id"]: row for row in project_rows}
    health_rows = {
        row.project_id: row
        for row in health.inspect_portfolio(
            conn, include_paused=True, live_git=False, scan_activity=False
        )
    }
    git_checks = {row["project_id"]: _row_dict(row) for row in store.list_git_checks(conn)}
    all_task_rows = [
        row for row in store.list_all_tasks(conn, include_done=True)
        if row["project_id"] in projects_by_id
    ]
    task_rows = [
        row for row in all_task_rows
        if row["status"] not in {"done", "abandoned"}
    ]
    suggestion_rows = [
        row for row in store.list_suggestions(conn, status="proposed")
        if row["project_id"] in projects_by_id
    ]
    run_rows = conn.execute(
        """SELECT runs.*, tasks.title AS task_title, tasks.status AS task_status,
                  projects.name AS project_name
           FROM runs
           JOIN tasks ON tasks.id = runs.task_id
           JOIN projects ON projects.id = runs.project_id
           ORDER BY runs.started_at DESC
           LIMIT 30"""
    ).fetchall()
    run_payloads: list[dict[str, Any]] = []
    latest_run_by_task: dict[str, dict[str, Any]] = {}
    for run in run_rows:
        item = _row_dict(run)
        item["state"] = (
            "running" if not run["ended_at"]
            else "failed" if run["exit_code"] not in {None, 0}
            else "completed"
        )
        item["log_bytes"] = runlog.size(run["id"])
        run_payloads.append(item)
        latest_run_by_task.setdefault(run["task_id"], item)

    dependency_rows = store.list_task_dependencies(conn)
    dependencies_by_task: dict[str, list[dict[str, Any]]] = {}
    for dependency in dependency_rows:
        item = _row_dict(dependency)
        dependencies_by_task.setdefault(dependency["task_id"], []).append(item)

    task_statuses = {row["id"]: row["status"] for row in all_task_rows}
    roadmap_payloads: list[dict[str, Any]] = []
    for task in all_task_rows:
        item = {field: task[field] for field in ROADMAP_TASK_FIELDS}
        item["dependencies"] = []
        for dependency in dependencies_by_task.get(task["id"], []):
            upstream_status = task_statuses.get(dependency["depends_on_task_id"])
            item["dependencies"].append({
                "task_id": dependency["task_id"],
                "depends_on_task_id": dependency["depends_on_task_id"],
                "depends_on_title": dependency["depends_on_title"],
                "depends_on_status": upstream_status,
                "type": dependency["type"],
                "satisfied": upstream_status in {"done", "abandoned"},
            })
        roadmap_payloads.append(item)

    activity_payloads = [
        _activity_dict(event) for event in store.list_activity_events(conn, limit=100)
    ]

    task_payloads: list[dict[str, Any]] = []
    project_task_index: dict[str, list[dict[str, Any]]] = {
        row["id"]: [] for row in project_rows
    }
    project_suggestion_index: dict[str, list[dict[str, Any]]] = {
        row["id"]: [] for row in project_rows
    }
    recommended_counts = {name: 0 for name in WORKER_NAMES}
    assigned_counts: dict[str, int] = {}

    for task in task_rows:
        project = projects_by_id[task["project_id"]]
        route = routing.route_task(project, task)
        # What dispatch will actually do, so the card and the run agree.
        effective = routing.effective_route(project, task)
        item = _row_dict(task)
        item["route"] = asdict(route)
        item["recommended_worker"] = route.worker
        item["recommended_model"] = route.model
        item["execution_worker"] = effective.worker
        item["execution_model"] = task["requested_model"] or effective.model
        item["execution_effort"] = task["effort"] or effective.effort
        item["policy_note"] = effective.policy_note
        item["policy_blocked_reason"] = effective.blocked_reason
        item["latest_run"] = latest_run_by_task.get(task["id"])
        item["dependencies"] = dependencies_by_task.get(task["id"], [])
        task_payloads.append(item)
        project_task_index[task["project_id"]].append(item)
        if task["assignee"]:
            key = str(task["assignee"]).lower()
            assigned_counts[key] = assigned_counts.get(key, 0) + 1
        elif task["status"] in {"open", "assigned"}:
            recommended_counts[route.worker] = recommended_counts.get(route.worker, 0) + 1

    suggestion_payloads: list[dict[str, Any]] = []
    for suggestion in suggestion_rows:
        item = _row_dict(suggestion)
        item["requires_approval"] = bool(item["requires_approval"])
        suggestion_payloads.append(item)
        project_suggestion_index[suggestion["project_id"]].append(item)

    project_payloads: list[dict[str, Any]] = []
    for project in project_rows:
        snapshot = health_rows[project["id"]]
        item = snapshot.to_dict()
        item.update(
            current_goal=project["current_goal"],
            stack=project["stack"],
            test_command=project["test_command"],
            updated_at=project["updated_at"],
            remote_url=project["remote_url"],
            github_owner=project["github_owner"],
            github_repo=project["github_repo"],
            codex_project_id=project["codex_project_id"],
            allowed_workers=list(policy.allowed_workers(project)),
            allowlist_configured=policy.is_configured(project),
        )
        item["git_check"] = git_checks.get(project["id"])
        if item["git_check"]:
            checked = item["git_check"]
            item.update(
                is_git=bool(checked["is_git"]),
                branch=checked["branch"],
                modified=checked["modified"],
                untracked=checked["untracked"],
                ahead=checked["ahead"],
                behind=checked["behind"],
                last_commit_date=checked["last_commit_date"],
                last_commit_sha=checked["last_commit_sha"],
                last_commit_subject=checked["last_commit_subject"],
                git_status_available=True,
                git_status_note=checked["note"],
            )
        active_tasks = project_task_index[project["id"]]
        item["tasks"] = active_tasks
        item["suggestions"] = project_suggestion_index[project["id"]]
        item["task_count"] = len(active_tasks)
        item["suggestion_count"] = len(item["suggestions"])
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
        "recommendations": len(suggestion_payloads),
        "working": sum(
            1 for task in active_tasks
            if task["status"] in {"assigned", "in_progress", "running"}
            and str(task["assignee"] or "").lower() not in {"owner", "perplexity"}
        ),
        "needs_decision": sum(
            1 for task in active_tasks
            if task["status"] in {"review", "blocked"}
            or str(task["assignee"] or "").lower() in {"owner", "perplexity"}
        ),
        "running_runs": sum(1 for run in run_payloads if run["state"] == "running"),
        "recent_runs": len(run_payloads),
        "git_dirty": sum(
            1 for project in project_payloads
            if project["status"] == "active" and project["modified"] + project["untracked"] > 0
        ),
        "git_ahead": sum(
            1 for project in project_payloads
            if project["status"] == "active" and (project["ahead"] or 0) > 0
        ),
        "git_behind": sum(
            1 for project in project_payloads
            if project["status"] == "active" and (project["behind"] or 0) > 0
        ),
        "git_unchecked": sum(
            1 for project in project_payloads
            if project["status"] == "active" and not project["git_check"]
        ),
        "git_no_upstream": sum(
            1 for project in project_payloads
            if project["status"] == "active" and project["git_check"]
            and project["is_git"] and project["git_check"]["fetched"]
            and project["ahead"] is None and project["behind"] is None
        ),
    }

    expert_payloads = team.team_payload(conn, task_payloads, run_payloads)
    usage = team.usage_totals(run_payloads)
    summary["tracked_tokens"] = usage["total_tokens"]

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
        "roadmap_tasks": roadmap_payloads,
        "suggestions": suggestion_payloads,
        "runs": run_payloads,
        "activity": activity_payloads,
        "dependencies": [_row_dict(row) for row in dependency_rows],
        "workers": worker_payloads,
        "team": expert_payloads,
        "usage": usage,
        "model_performance": team.performance_payload(conn),
        "focus": pm.portfolio_focus(conn),
    }


def _now_iso() -> str:
    from .ids import now

    return now()


class CortexDashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # HTTPServer enables SO_REUSEADDR, which on Windows lets a second process
    # bind a port that is already being listened on. The old server keeps
    # serving, so a restart appears to succeed while still running stale code.
    # Failing to bind is the honest outcome.
    allow_reuse_address = os.name != "nt"

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
        self.action_token = secrets.token_urlsafe(32)
        with db.connect(database_path) as conn:
            interrupted = jobs.reconcile(conn)
        if interrupted:
            print(f"dashboard: closed {interrupted} job(s) left by a previous run")

    def start_job(
        self,
        kind: str,
        target: Any,
        *,
        label: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Record a job, then run it on a background thread.

        ``target`` receives the job id so a dispatch can associate itself with
        the run it creates while that run is still in progress.
        """
        with db.connect(self.database_path) as conn:
            job_id = jobs.create(
                conn, kind=kind, label=label, task_id=task_id, project_id=project_id
            )

        database_path = self.database_path

        def run() -> None:
            try:
                result = target(job_id)
                status, error = "done", None
            except Exception as exc:  # worker failures are reported to the local UI
                result, status, error = None, "failed", str(exc)
            try:
                with db.connect(database_path) as conn:
                    jobs.finish(conn, job_id, status=status, result=result, error=error)
            except sqlite3.Error as exc:
                print(f"dashboard: could not record job {job_id}: {exc}")

        threading.Thread(target=run, name=f"cortex-{kind}-{job_id}", daemon=True).start()
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with db.connect(self.database_path) as conn:
            return jobs.get(conn, job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        with db.connect(self.database_path) as conn:
            return jobs.recent(conn, limit=30)


class DashboardHandler(BaseHTTPRequestHandler):
    server: CortexDashboardServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"dashboard: {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/portfolio":
            with db.connect(self.server.database_path) as conn:
                payload = portfolio_payload(conn)
                payload["action_token"] = self.server.action_token
                self._json(HTTPStatus.OK, payload)
            return
        if path == "/api/activity":
            params = parse_qs(urlparse(self.path).query)
            project = params.get("project", [None])[0]
            task_id = params.get("task", [None])[0]
            session_id = params.get("session", [None])[0]
            try:
                limit = max(1, min(500, int(params.get("limit", ["100"])[0])))
            except ValueError:
                limit = 100
            with db.connect(self.server.database_path) as conn:
                project_id = None
                if project:
                    try:
                        project_id = store.get_project(conn, project)["id"]
                    except store.NotFound as exc:
                        self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                        return
                events = store.list_activity_events(
                    conn,
                    project_id,
                    task_id=task_id,
                    session_id=session_id,
                    limit=limit,
                )
                self._json(
                    HTTPStatus.OK,
                    {"activity": [_activity_dict(event) for event in events]},
                )
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "database": str(self.server.database_path)})
            return
        if path == "/api/jobs":
            self._json(HTTPStatus.OK, {"jobs": self.server.list_jobs()})
            return
        parts = [part for part in unquote(path).split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = self.server.get_job(parts[2])
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
            else:
                self._json(HTTPStatus.OK, {"job": job})
            return
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "output":
            self._run_output(parts[2], urlparse(self.path).query)
            return
        if path.startswith("/api/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            return
        self._serve_static(path)

    def _run_output(self, run_id: str, query: str) -> None:
        """Return live worker output written after a byte offset.

        Polling with an explicit offset rather than streaming keeps this on the
        same simple request model as the rest of the API, and means a dropped
        connection costs one poll instead of restarting the transcript.
        """
        params = parse_qs(query)
        try:
            offset = max(0, int(params.get("offset", ["0"])[0]))
        except ValueError:
            offset = 0
        with db.connect(self.server.database_path) as conn:
            row = conn.execute(
                "SELECT id, ended_at, exit_code FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
            return
        text, new_offset = runlog.read_from(run_id, offset)
        self._json(
            HTTPStatus.OK,
            {
                "run_id": run_id,
                "text": text,
                "offset": new_offset,
                "running": row["ended_at"] is None,
                "exit_code": row["exit_code"],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        parts = [part for part in path.split("/") if part]
        try:
            body = self._body()
            if path == "/api/continue":
                project_id = body.get("project_id")
                with db.connect(self.server.database_path) as conn:
                    if project_id:
                        project_id = store.get_project(conn, str(project_id))["id"]
                    focus = pm.portfolio_focus(conn, project_id)
                if focus["focus"]["kind"] != "plan":
                    self._json(HTTPStatus.OK, focus)
                    return
                project_id = focus["focus"]["project_id"]
                job_id = self._start_plan_job(
                    project_id,
                    worker=str(body.get("worker") or "codex"),
                    model=str(body.get("model") or "default"),
                    force=bool(body.get("force", False)),
                    allow_cloud=bool(body.get("allow_cloud", False)),
                )
                self._json(HTTPStatus.ACCEPTED, {"job_id": job_id, "focus": focus})
                return
            if path == "/api/git/refresh":
                job_id = self._start_git_job(fetch=bool(body.get("fetch", False)))
                self._json(HTTPStatus.ACCEPTED, {"job_id": job_id})
                return
            if path == "/api/team/keep-working":
                limit = int(body.get("limit", 3))
                with db.connect(self.server.database_path) as conn:
                    task_ids = team.safe_start_candidates(conn, limit=limit)
                job_ids = [
                    self._start_dispatch_job(
                        task_id, allow_write=False, approve_high_risk=False
                    )
                    for task_id in task_ids
                ]
                self._json(
                    HTTPStatus.ACCEPTED,
                    {"started": len(job_ids), "task_ids": task_ids, "job_ids": job_ids},
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "plan":
                with db.connect(self.server.database_path) as conn:
                    project_id = store.get_project(conn, parts[2])["id"]
                job_id = self._start_plan_job(
                    project_id,
                    worker=str(body.get("worker") or "codex"),
                    model=str(body.get("model") or "default"),
                    force=bool(body.get("force", False)),
                    allow_cloud=bool(body.get("allow_cloud", False)),
                )
                self._json(HTTPStatus.ACCEPTED, {"job_id": job_id})
                return
            if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "approve":
                with db.connect(self.server.database_path) as conn:
                    task_id = store.convert_suggestion(conn, parts[2])
                    task = _row_dict(store.get_task(conn, task_id))
                if not bool(body.get("start", False)):
                    self._json(HTTPStatus.CREATED, {"task": task})
                    return
                job_id = self._start_dispatch_job(
                    task_id,
                    allow_write=bool(body.get("allow_write", False)),
                    approve_high_risk=bool(body.get("approve_high_risk", False)),
                )
                self._json(HTTPStatus.ACCEPTED, {"task": task, "job_id": job_id})
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "start":
                with db.connect(self.server.database_path) as conn:
                    task_id = store.get_task(conn, parts[2])["id"]
                job_id = self._start_dispatch_job(
                    task_id,
                    allow_write=bool(body.get("allow_write", False)),
                    approve_high_risk=bool(body.get("approve_high_risk", False)),
                )
                self._json(HTTPStatus.ACCEPTED, {"task_id": task_id, "job_id": job_id})
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "tasks"]
                and parts[3:] == ["github", "preview"]
            ):
                if not self._allow_local_action():
                    return
                with db.connect(self.server.database_path) as conn:
                    payload = github_mirror_preview(conn, parts[2])
                self._json(HTTPStatus.OK, payload)
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "tasks"]
                and parts[3:] == ["codex", "preview"]
            ):
                if not self._allow_local_action():
                    return
                with db.connect(self.server.database_path) as conn:
                    task = store.get_task(conn, parts[2])
                    project = store.get_project(conn, task["project_id"])
                    if not policy.is_allowed(project, "codex"):
                        raise ValueError("Codex is not allowed to read this project")
                    if not task["acceptance"]:
                        raise ValueError("add a done-when check before copying a Codex prompt")
                    prompt = codex_app.launch_prompt(project, task)
                    findings = secrets_scan.scan(prompt, use_ollama=False)
                    if findings:
                        kinds = ", ".join(sorted({finding.kind for finding in findings}))
                        raise ValueError(f"Codex prompt blocked by local privacy scan: {kinds}")
                    capability = codex_app.inspect(
                        project["repo_path"], task["codex_thread_id"]
                    )
                    store.create_activity_event(
                        conn,
                        project_id=project["id"],
                        task_id=task["id"],
                        actor_type="human",
                        actor_name="owner",
                        action="codex.launch_previewed",
                        summary=f"Previewed Codex handoff for {task['title']}",
                        source="dashboard",
                        source_ref=task["codex_thread_id"] or task["id"],
                        session_id=task["pm_session_id"],
                        evidence={
                            "available": capability["available"],
                            "linked_thread_found": capability["linked_thread_found"],
                            "methods": capability["methods"],
                        },
                    )
                self._json(
                    HTTPStatus.OK,
                    {
                        "task_id": task["id"],
                        "codex_thread_id": task["codex_thread_id"],
                        "prompt": prompt,
                        "capability": capability,
                        "mode": "copy_prompt",
                        "start_enabled": False,
                        "can_start_turn": False,
                        "can_navigate": False,
                    },
                )
                return
            if path != "/api/tasks":
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                return
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
        except (
            KeyError, TypeError, ValueError, store.NotFound,
            github_reader.GitHubProjectError, github_adapter.MirrorApplyError,
        ) as exc:
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
                    store.update_task(
                        conn,
                        item_id,
                        actor_type="human",
                        actor_name="owner",
                        source="dashboard",
                        **fields,
                    )
                    payload = {"task": _row_dict(store.get_task(conn, item_id))}
                elif kind == "projects":
                    store.get_project(conn, item_id)
                    fields = {key: value for key, value in body.items() if key in PROJECT_MUTABLE_FIELDS}
                    store.update_project(conn, item_id, **fields)
                    payload = {"project": _row_dict(store.get_project(conn, item_id))}
                elif kind == "suggestions":
                    store.get_suggestion(conn, item_id)
                    status = body.get("status")
                    if status not in {"dismissed", "proposed"}:
                        raise ValueError("suggestion status must be dismissed or proposed")
                    store.update_suggestion(conn, item_id, status=status)
                    payload = {"suggestion": _row_dict(store.get_suggestion(conn, item_id))}
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                    return
            self._json(HTTPStatus.OK, payload)
        except (TypeError, ValueError, store.NotFound) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _start_plan_job(
        self, project_id: str, *, worker: str, model: str, force: bool,
        allow_cloud: bool,
    ) -> str:
        database_path = self.server.database_path

        def target(job_id: str) -> dict[str, Any]:
            with db.connect(database_path) as conn:
                project = store.get_project(conn, project_id)
                return asdict(pm.plan_project(
                    conn, project, worker=worker, model=model, force=force,
                    allow_cloud=allow_cloud,
                ))

        return self.server.start_job(
            "plan", target, label="Planning", project_id=project_id
        )

    def _start_dispatch_job(
        self, task_id: str, *, allow_write: bool, approve_high_risk: bool
    ) -> str:
        database_path = self.server.database_path

        def target(job_id: str) -> dict[str, Any]:
            with db.connect(database_path) as conn:
                task = store.get_task(conn, task_id)
                # Publish the run id as soon as it exists so the dashboard can
                # start tailing output while the worker is still thinking.
                result = dispatcher.dispatch(
                    conn, task, allow_write=allow_write,
                    approve_high_risk=approve_high_risk,
                    on_run_start=lambda run_id: jobs.attach_run(conn, job_id, run_id),
                )
                payload = asdict(result)
                payload["workspace"] = str(payload["workspace"])
                return payload

        with db.connect(database_path) as conn:
            project_id = store.get_task(conn, task_id)["project_id"]
        return self.server.start_job(
            "dispatch", target, label="Working", task_id=task_id,
            project_id=project_id,
        )

    def _start_git_job(self, *, fetch: bool) -> str:
        database_path = self.server.database_path

        def target(job_id: str) -> dict[str, Any]:
            with db.connect(database_path) as conn:
                rows = git_monitor.refresh_portfolio(conn, fetch=fetch)
                return {"checked": len(rows), "fetched": fetch}

        return self.server.start_job("git", target, label="Checking GitHub")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be JSON under 1 MB")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("JSON body must be an object")
        return payload

    def _allow_local_action(self) -> bool:
        """Protect subprocess-backed actions from cross-site/browser requests."""
        local_names = {"127.0.0.1", "localhost", "::1"}
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname
        origin_header = self.headers.get("Origin")
        origin = urlparse(origin_header).hostname if origin_header else None
        supplied = self.headers.get("X-Cortex-Action-Token", "")
        if host not in local_names or (origin is not None and origin not in local_names):
            self._json(HTTPStatus.FORBIDDEN, {"error": "local dashboard origin required"})
            return False
        if not hmac.compare_digest(supplied, self.server.action_token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid action token"})
            return False
        return True

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
