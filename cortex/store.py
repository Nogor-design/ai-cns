"""Data-access layer: CRUD over projects, tasks, runs, decisions.

Thin functions over the sqlite3 connection so the CLI and higher-level features
(brief, runs, state) stay declarative and the SQL lives in one place.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import ids

TASK_TYPES = {"code", "review", "research", "docs", "data", "planning", "other"}
EXECUTION_MODES = {"agentic_cli", "headless_cli", "api", "manual"}
PROJECT_STATUSES = {"active", "paused", "archived"}
PRIVACY_LEVELS = {"public", "internal", "restricted"}
TASK_RISKS = {"auto", "low", "medium", "high"}
EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}
TASK_STATUSES = {
    "open", "assigned", "in_progress", "running", "review", "blocked",
    "done", "abandoned",
}
STATE_MODES = {"tracked", "deferred"}
SUGGESTION_STATUSES = {"proposed", "converted", "dismissed"}


class NotFound(LookupError):
    pass


# ---------------------------------------------------------------- projects ---
def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    repo_path: str,
    stack: str | None = None,
    current_goal: str | None = None,
    test_command: str | None = None,
    program: str = "general",
    priority: int = 3,
    privacy: str = "internal",
    state_mode: str = "tracked",
    project_id: str | None = None,
) -> str:
    priority = max(1, min(5, priority))
    if privacy not in PRIVACY_LEVELS:
        privacy = "internal"
    if state_mode not in STATE_MODES:
        state_mode = "tracked"
    pid = project_id or ids.slugify(name)
    ts = ids.now()
    conn.execute(
        """INSERT INTO projects
           (id, name, repo_path, stack, status, program, priority, privacy,
            state_mode, current_goal, test_command, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pid, name, repo_path, stack, "active", program, priority, privacy,
            state_mode, current_goal, test_command, ts,
        ),
    )
    conn.commit()
    return pid


def get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        # Allow lookup by name as a convenience.
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (project_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"project not found: {project_id}")
    return row


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects ORDER BY updated_at DESC"
    ).fetchall()


def update_project(conn: sqlite3.Connection, project_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "status" in fields and fields["status"] not in PROJECT_STATUSES:
        raise ValueError(f"invalid project status: {fields['status']}")
    if "privacy" in fields and fields["privacy"] not in PRIVACY_LEVELS:
        raise ValueError(f"invalid privacy level: {fields['privacy']}")
    if "priority" in fields:
        fields["priority"] = max(1, min(5, int(fields["priority"])))
    if "state_mode" in fields and fields["state_mode"] not in STATE_MODES:
        raise ValueError(f"invalid state mode: {fields['state_mode']}")
    fields["updated_at"] = ids.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE projects SET {cols} WHERE id = ?",
        (*fields.values(), project_id),
    )
    conn.commit()


# ------------------------------------------------------------------- tasks ---
def create_task(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    type: str = "other",
    brief: str | None = None,
    risk: str = "auto",
    complexity: int | None = None,
    acceptance: str | None = None,
    allowed_paths: str | None = None,
    budget: str | None = None,
    requested_model: str | None = None,
    effort: str | None = None,
    priority: int = 3,
    assignee: str | None = None,
    due_at: str | None = None,
) -> str:
    if type not in TASK_TYPES:
        type = "other"
    if risk not in TASK_RISKS:
        risk = "auto"
    if complexity is not None:
        complexity = max(0, min(10, complexity))
    priority = max(1, min(5, int(priority)))
    if effort not in EFFORT_LEVELS:
        effort = None
    tid = ids.short_id()
    ts = ids.now()
    conn.execute(
        """INSERT INTO tasks
           (id, project_id, title, type, status, brief, risk, complexity,
            acceptance, allowed_paths, budget, requested_model, effort,
            priority, assignee, due_at,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, project_id, title, type, "open", brief, risk, complexity,
            acceptance, allowed_paths, budget, requested_model, effort,
            priority, assignee, due_at, ts, ts,
        ),
    )
    conn.commit()
    return tid


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"task not found: {task_id}")
    return row


def list_tasks(
    conn: sqlite3.Connection, project_id: str, status: str | None = None
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status = ? ORDER BY created_at",
            (project_id, status),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()


def list_all_tasks(
    conn: sqlite3.Connection, *, include_done: bool = False
) -> list[sqlite3.Row]:
    where = "" if include_done else "WHERE tasks.status NOT IN ('done', 'abandoned')"
    return conn.execute(
        f"""SELECT tasks.*, projects.name AS project_name,
                   projects.program AS project_program,
                   projects.status AS project_status
            FROM tasks JOIN projects ON projects.id = tasks.project_id
            {where}
            ORDER BY tasks.priority, tasks.updated_at DESC"""
    ).fetchall()


def update_task(conn: sqlite3.Connection, task_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "risk" in fields and fields["risk"] not in TASK_RISKS:
        raise ValueError(f"invalid task risk: {fields['risk']}")
    if "complexity" in fields and fields["complexity"] is not None:
        fields["complexity"] = max(0, min(10, int(fields["complexity"])))
    if "status" in fields and fields["status"] not in TASK_STATUSES:
        raise ValueError(f"invalid task status: {fields['status']}")
    if "priority" in fields:
        fields["priority"] = max(1, min(5, int(fields["priority"])))
    if "effort" in fields and fields["effort"] not in EFFORT_LEVELS | {None}:
        raise ValueError(f"invalid effort: {fields['effort']}")
    fields["updated_at"] = ids.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
    )
    conn.commit()


# ------------------------------------------------------------ suggestions ---
def create_suggestion(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    type: str = "other",
    why: str | None = None,
    brief: str | None = None,
    risk: str = "auto",
    complexity: int | None = None,
    acceptance: str | None = None,
    allowed_paths: str | None = None,
    budget: str | None = None,
    effort: str | None = None,
    priority: int = 3,
    recommended_worker: str | None = None,
    recommended_model: str | None = None,
    action: str | None = None,
    reviewer: str | None = None,
    requires_approval: bool = False,
    source_worker: str = "deterministic",
    source_model: str | None = None,
) -> str:
    if type not in TASK_TYPES:
        type = "other"
    if risk not in TASK_RISKS:
        risk = "auto"
    if complexity is not None:
        complexity = max(0, min(10, int(complexity)))
    priority = max(1, min(5, int(priority)))
    if effort not in EFFORT_LEVELS:
        effort = None
    sid = ids.short_id()
    ts = ids.now()
    conn.execute(
        """INSERT INTO suggestions
           (id, project_id, title, type, status, why, brief, risk, complexity,
            acceptance, allowed_paths, budget, effort, priority, recommended_worker,
            recommended_model, action, reviewer, requires_approval,
            source_worker, source_model, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sid, project_id, title, type, "proposed", why, brief, risk,
            complexity, acceptance, allowed_paths, budget, effort, priority,
            recommended_worker, recommended_model, action, reviewer,
            int(requires_approval), source_worker, source_model, ts, ts,
        ),
    )
    conn.commit()
    return sid


def get_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"suggestion not found: {suggestion_id}")
    return row


def list_suggestions(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    *,
    status: str | None = "proposed",
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if project_id:
        clauses.append("suggestions.project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("suggestions.status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(
        f"""SELECT suggestions.*, projects.name AS project_name,
                   projects.program AS project_program,
                   projects.status AS project_status
            FROM suggestions JOIN projects ON projects.id = suggestions.project_id
            {where}
            ORDER BY suggestions.priority, suggestions.created_at DESC""",
        params,
    ).fetchall()


def update_suggestion(
    conn: sqlite3.Connection, suggestion_id: str, **fields: Any
) -> None:
    if not fields:
        return
    if "status" in fields and fields["status"] not in SUGGESTION_STATUSES:
        raise ValueError(f"invalid suggestion status: {fields['status']}")
    allowed = {
        "title", "type", "status", "why", "brief", "risk", "complexity",
        "acceptance", "allowed_paths", "budget", "effort", "priority", "task_id",
    }
    unexpected = set(fields) - allowed
    if unexpected:
        raise ValueError(f"invalid suggestion fields: {', '.join(sorted(unexpected))}")
    fields["updated_at"] = ids.now()
    cols = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE suggestions SET {cols} WHERE id = ?",
        (*fields.values(), suggestion_id),
    )
    conn.commit()


def convert_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> str:
    """Atomically turn one proposed suggestion into an assigned task."""
    suggestion = get_suggestion(conn, suggestion_id)
    if suggestion["status"] == "converted" and suggestion["task_id"]:
        return str(suggestion["task_id"])
    if suggestion["status"] != "proposed":
        raise ValueError(f"suggestion is {suggestion['status']}, not proposed")
    task_id = create_task(
        conn,
        project_id=suggestion["project_id"],
        title=suggestion["title"],
        type=suggestion["type"],
        brief=suggestion["brief"],
        risk=suggestion["risk"],
        complexity=suggestion["complexity"],
        acceptance=suggestion["acceptance"],
        allowed_paths=suggestion["allowed_paths"],
        budget=suggestion["budget"],
        requested_model=suggestion["recommended_model"],
        effort=suggestion["effort"],
        priority=suggestion["priority"],
        assignee=suggestion["recommended_worker"],
    )
    update_task(conn, task_id, status="assigned")
    update_suggestion(conn, suggestion_id, status="converted", task_id=task_id)
    return task_id


# -------------------------------------------------------------------- runs ---
def create_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    project_id: str,
    model: str | None,
    execution_mode: str,
    git_before: str | None = None,
    captured_via: str | None = None,
    effort: str | None = None,
) -> str:
    rid = ids.short_id()
    conn.execute(
        """INSERT INTO runs
           (id, task_id, project_id, model, execution_mode, started_at,
            git_before, captured_via, outcome, effort)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            rid, task_id, project_id, model, execution_mode, ids.now(),
            git_before, captured_via, "unknown", effort,
        ),
    )
    conn.commit()
    return rid


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"run not found: {run_id}")
    return row


def update_run(conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "files_changed" in fields and not isinstance(fields["files_changed"], str):
        fields["files_changed"] = json.dumps(fields["files_changed"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id)
    )
    conn.commit()


def list_runs(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()


def unresolved_runs(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM runs
           WHERE project_id = ? AND execution_mode = 'agentic_cli'
             AND ended_at IS NOT NULL
             AND (outcome IS NULL OR outcome = 'unknown')""",
        (project_id,),
    ).fetchall()


# --------------------------------------------------------------- decisions ---
def create_decision(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    decision: str,
    rationale: str | None = None,
    source: str = "manual",
) -> str:
    did = ids.short_id()
    conn.execute(
        """INSERT INTO decisions (id, project_id, ts, decision, rationale, source)
           VALUES (?,?,?,?,?,?)""",
        (did, project_id, ids.now(), decision, rationale, source),
    )
    conn.commit()
    return did


def recent_decisions(
    conn: sqlite3.Connection, project_id: str, limit: int = 10
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decisions WHERE project_id = ? ORDER BY ts DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()


# ----------------------------------------------------------------- history ---
def model_task_history(
    conn: sqlite3.Connection, project_id: str, task_type: str | None = None
) -> list[sqlite3.Row]:
    if task_type:
        return conn.execute(
            "SELECT * FROM model_task_history WHERE project_id = ? AND task_type = ?"
            " ORDER BY last_used DESC",
            (project_id, task_type),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM model_task_history WHERE project_id = ? ORDER BY last_used DESC",
        (project_id,),
    ).fetchall()


# ------------------------------------------------------------- Git checks ---
def upsert_git_check(conn: sqlite3.Connection, project_id: str, **fields: Any) -> None:
    values = {
        "is_git": int(bool(fields.get("is_git", False))),
        "branch": fields.get("branch"),
        "modified": int(fields.get("modified", 0)),
        "untracked": int(fields.get("untracked", 0)),
        "ahead": fields.get("ahead"),
        "behind": fields.get("behind"),
        "last_commit_date": fields.get("last_commit_date"),
        "last_commit_sha": fields.get("last_commit_sha"),
        "last_commit_subject": fields.get("last_commit_subject"),
        "fetched": int(bool(fields.get("fetched", False))),
        "note": fields.get("note"),
        "checked_at": fields.get("checked_at") or ids.now(),
    }
    conn.execute(
        """INSERT INTO project_git_checks
           (project_id, is_git, branch, modified, untracked, ahead, behind,
            last_commit_date, last_commit_sha, last_commit_subject, fetched,
            note, checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET
             is_git=excluded.is_git, branch=excluded.branch,
             modified=excluded.modified, untracked=excluded.untracked,
             ahead=excluded.ahead, behind=excluded.behind,
             last_commit_date=excluded.last_commit_date,
             last_commit_sha=excluded.last_commit_sha,
             last_commit_subject=excluded.last_commit_subject,
             fetched=excluded.fetched, note=excluded.note,
             checked_at=excluded.checked_at""",
        (project_id, *values.values()),
    )
    conn.commit()


def list_git_checks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM project_git_checks").fetchall()
