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
    project_id: str | None = None,
) -> str:
    priority = max(1, min(5, priority))
    if privacy not in PRIVACY_LEVELS:
        privacy = "internal"
    pid = project_id or ids.slugify(name)
    ts = ids.now()
    conn.execute(
        """INSERT INTO projects
           (id, name, repo_path, stack, status, program, priority, privacy,
            current_goal, test_command, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pid, name, repo_path, stack, "active", program, priority, privacy,
            current_goal, test_command, ts,
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
) -> str:
    if type not in TASK_TYPES:
        type = "other"
    if risk not in TASK_RISKS:
        risk = "auto"
    if complexity is not None:
        complexity = max(0, min(10, complexity))
    tid = ids.short_id()
    ts = ids.now()
    conn.execute(
        """INSERT INTO tasks
           (id, project_id, title, type, status, brief, risk, complexity,
            acceptance, allowed_paths, budget, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, project_id, title, type, "open", brief, risk, complexity,
            acceptance, allowed_paths, budget, ts, ts,
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


def update_task(conn: sqlite3.Connection, task_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "risk" in fields and fields["risk"] not in TASK_RISKS:
        raise ValueError(f"invalid task risk: {fields['risk']}")
    if "complexity" in fields and fields["complexity"] is not None:
        fields["complexity"] = max(0, min(10, int(fields["complexity"])))
    fields["updated_at"] = ids.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
    )
    conn.commit()


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
) -> str:
    rid = ids.short_id()
    conn.execute(
        """INSERT INTO runs
           (id, task_id, project_id, model, execution_mode, started_at,
            git_before, captured_via, outcome)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            rid, task_id, project_id, model, execution_mode, ids.now(),
            git_before, captured_via, "unknown",
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
