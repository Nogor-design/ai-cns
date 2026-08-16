"""Explicit, local-only project removal with a durable audit tombstone."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config, ids, store


COUNT_QUERIES: dict[str, str] = {
    "tasks": "SELECT COUNT(*) FROM tasks WHERE project_id = ?",
    "task_dependencies": """SELECT COUNT(*) FROM task_dependencies
        WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)
           OR depends_on_task_id IN (SELECT id FROM tasks WHERE project_id = ?)""",
    "runs": "SELECT COUNT(*) FROM runs WHERE project_id = ?",
    "decisions": "SELECT COUNT(*) FROM decisions WHERE project_id = ?",
    "activity_events": "SELECT COUNT(*) FROM activity_events WHERE project_id = ?",
    "suggestions": "SELECT COUNT(*) FROM suggestions WHERE project_id = ?",
    "github_mirror_operations": (
        "SELECT COUNT(*) FROM github_mirror_operations WHERE project_id = ?"
    ),
    "jobs": "SELECT COUNT(*) FROM jobs WHERE project_id = ?",
    "project_git_checks": "SELECT COUNT(*) FROM project_git_checks WHERE project_id = ?",
}


def _project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise store.NotFound(f"project not found: {project_id}")
    return row


def _count(conn: sqlite3.Connection, query: str, project_id: str) -> int:
    parameter_count = query.count("?")
    return int(conn.execute(query, (project_id,) * parameter_count).fetchone()[0])


def preview(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Describe the exact local deletion without changing any state."""
    project = _project(conn, project_id)
    counts = {
        name: _count(conn, query, project["id"])
        for name, query in COUNT_QUERIES.items()
    }
    running_jobs = int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id = ? AND status = 'running'",
        (project["id"],),
    ).fetchone()[0])
    running_runs = int(conn.execute(
        "SELECT COUNT(*) FROM runs WHERE project_id = ? AND ended_at IS NULL",
        (project["id"],),
    ).fetchone()[0])
    active_pm_sessions = int(conn.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE project_id = ? AND pm_session_id IS NOT NULL
             AND status IN ('in_progress', 'running')""",
        (project["id"],),
    ).fetchone()[0])
    linked_github_tasks = int(conn.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE project_id = ? AND github_issue_id IS NOT NULL""",
        (project["id"],),
    ).fetchone()[0])
    linked_codex_tasks = int(conn.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE project_id = ? AND codex_thread_id IS NOT NULL""",
        (project["id"],),
    ).fetchone()[0])
    blockers = []
    if running_jobs:
        blockers.append(f"{running_jobs} Cortex job(s) are still running")
    if running_runs:
        blockers.append(f"{running_runs} AI run(s) are still running")
    if active_pm_sessions:
        blockers.append(f"{active_pm_sessions} PM session(s) are still active")
    state_path = config.state_path(project["repo_path"])
    return {
        "project_id": project["id"],
        "project_name": project["name"],
        "repo_path": project["repo_path"],
        "deleted_counts": counts,
        "deleted_total": 1 + sum(counts.values()),
        "blocked": bool(blockers),
        "blockers": blockers,
        "preserved": {
            "repository": project["repo_path"],
            "state_path": str(state_path),
            "state_exists": state_path.is_file(),
            "github_project_configured": bool(project["github_project_id"]),
            "github_linked_tasks": linked_github_tasks,
            "codex_linked_tasks": linked_codex_tasks,
        },
    }


def remove(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    confirm_name: Any,
    acknowledge_permanent: Any,
    actor_type: str = "human",
    actor_name: str = "owner",
    source: str = "dashboard",
) -> dict[str, Any]:
    """Permanently delete one project's Cortex rows, never repository files."""
    project = _project(conn, project_id)
    if not isinstance(confirm_name, str) or confirm_name != project["name"]:
        raise ValueError("type the exact project name to confirm removal")
    if acknowledge_permanent is not True:
        raise ValueError("confirm that Cortex history will be permanently removed")

    try:
        conn.execute("BEGIN IMMEDIATE")
        removal_preview = preview(conn, project_id)
        if removal_preview["blocked"]:
            raise ValueError("project cannot be removed: " + "; ".join(removal_preview["blockers"]))

        removal_id = ids.short_id()
        removed_at = ids.now()
        evidence = {
            "preserved": removal_preview["preserved"],
            "deleted_counts": removal_preview["deleted_counts"],
            "program": project["program"],
            "privacy": project["privacy"],
            "priority": project["priority"],
        }
        conn.execute(
            """INSERT INTO project_removals
               (removal_id, project_id, project_name, repo_path, actor_type,
                actor_name, source, removed_at, deleted_counts_json, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                removal_id, project["id"], project["name"], project["repo_path"],
                actor_type, actor_name, source, removed_at,
                json.dumps(removal_preview["deleted_counts"], sort_keys=True),
                json.dumps(evidence, sort_keys=True),
            ),
        )

        # Remove child records before their project/task parents. Repository,
        # GitHub, and Codex resources are deliberately outside this transaction.
        conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM project_git_checks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM github_mirror_operations WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM activity_events WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM suggestions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM decisions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
        conn.execute(
            """DELETE FROM task_dependencies
               WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)
                  OR depends_on_task_id IN (SELECT id FROM tasks WHERE project_id = ?)""",
            (project_id, project_id),
        )
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        deleted = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount
        if deleted != 1:
            raise store.NotFound(f"project not found: {project_id}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "removal_id": removal_id,
        "project_id": project["id"],
        "project_name": project["name"],
        "removed_at": removed_at,
        "deleted_counts": removal_preview["deleted_counts"],
        "deleted_total": removal_preview["deleted_total"],
        "preserved": removal_preview["preserved"],
    }


def list_activity(conn: sqlite3.Connection, *, limit: int = 100) -> list[sqlite3.Row]:
    """Render removal tombstones in the portfolio-wide evidence timeline."""
    return conn.execute(
        """SELECT removal_id AS id, project_id, project_name,
                  NULL AS task_id, 'Project removed from Cortex' AS task_title,
                  actor_type, actor_name, NULL AS model,
                  'project.removed' AS action,
                  'Removed project from Cortex: ' || project_name AS summary,
                  source, project_id AS source_ref, NULL AS session_id,
                  removed_at AS occurred_at, evidence_json
           FROM project_removals
           ORDER BY removed_at DESC, removal_id DESC
           LIMIT ?""",
        (max(1, min(500, limit)),),
    ).fetchall()
