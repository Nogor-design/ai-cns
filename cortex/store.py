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
DEPENDENCY_TYPES = {"blocks"}


class NotFound(LookupError):
    pass


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _insert_activity(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    action: str,
    summary: str,
    task_id: str | None = None,
    actor_type: str = "system",
    actor_name: str | None = "cortex",
    model: str | None = None,
    source: str = "cortex",
    source_ref: str | None = None,
    session_id: str | None = None,
    evidence: Any = None,
    occurred_at: str | None = None,
) -> str:
    event_id = ids.short_id()
    conn.execute(
        """INSERT INTO activity_events
           (id, project_id, task_id, actor_type, actor_name, model, action,
            summary, source, source_ref, session_id, occurred_at, evidence_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, project_id, task_id, actor_type, actor_name, model,
            action, summary, source, source_ref, session_id, occurred_at or ids.now(),
            _json(evidence),
        ),
    )
    return event_id


def create_activity_event(
    conn: sqlite3.Connection, *, commit: bool = True, **fields: Any
) -> str:
    event_id = _insert_activity(conn, **fields)
    if commit:
        conn.commit()
    return event_id


def list_activity_events(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if project_id:
        clauses.append("activity_events.project_id = ?")
        params.append(project_id)
    if task_id:
        clauses.append("activity_events.task_id = ?")
        params.append(task_id)
    if session_id:
        clauses.append("activity_events.session_id = ?")
        params.append(session_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(500, limit)))
    return conn.execute(
        f"""SELECT activity_events.*, projects.name AS project_name,
                   tasks.title AS task_title
            FROM activity_events
            JOIN projects ON projects.id = activity_events.project_id
            LEFT JOIN tasks ON tasks.id = activity_events.task_id
            {where}
            ORDER BY activity_events.occurred_at DESC, activity_events.id DESC
            LIMIT ?""",
        params,
    ).fetchall()


def _parent_would_cycle(
    conn: sqlite3.Connection, task_id: str, parent_id: str
) -> bool:
    current_id: str | None = parent_id
    seen: set[str] = set()
    while current_id:
        if current_id == task_id or current_id in seen:
            return True
        seen.add(current_id)
        row = conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?", (current_id,)
        ).fetchone()
        current_id = row["parent_id"] if row else None
    return False


def _dependency_would_cycle(
    conn: sqlite3.Connection, task_id: str, depends_on_task_id: str
) -> bool:
    row = conn.execute(
        """WITH RECURSIVE upstream(id) AS (
               SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?
               UNION
               SELECT dependency.depends_on_task_id
               FROM task_dependencies AS dependency
               JOIN upstream ON dependency.task_id = upstream.id
           )
           SELECT 1 FROM upstream WHERE id = ? LIMIT 1""",
        (depends_on_task_id, task_id),
    ).fetchone()
    return row is not None


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
    if "allowed_workers" in fields and fields["allowed_workers"] is not None:
        value = fields["allowed_workers"]
        if not isinstance(value, str) or not value.startswith("["):
            from . import policy

            fields["allowed_workers"] = policy.encode(
                value if isinstance(value, (list, tuple, set))
                else str(value).replace(",", "\n").split()
            )
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
    parent_id: str | None = None,
    milestone: str | None = None,
    start_at: str | None = None,
    target_at: str | None = None,
    progress: int = 0,
    blocked_reason: str | None = None,
    next_action: str | None = None,
    github_issue_id: str | None = None,
    github_issue_number: int | None = None,
    github_issue_url: str | None = None,
    github_project_item_id: str | None = None,
    codex_thread_id: str | None = None,
    pm_session_id: str | None = None,
    sync_state: str | None = None,
    actor_type: str = "system",
    actor_name: str | None = None,
    source: str = "cortex",
    commit: bool = True,
) -> str:
    if type not in TASK_TYPES:
        type = "other"
    if risk not in TASK_RISKS:
        risk = "auto"
    if complexity is not None:
        complexity = max(0, min(10, complexity))
    priority = max(1, min(5, int(priority)))
    progress = max(0, min(100, int(progress)))
    if effort not in EFFORT_LEVELS:
        effort = None
    if parent_id:
        parent = get_task(conn, parent_id)
        if parent["project_id"] != project_id:
            raise ValueError("parent task must belong to the same project")
    tid = ids.short_id()
    ts = ids.now()
    conn.execute(
        """INSERT INTO tasks
           (id, project_id, title, type, status, brief, risk, complexity,
            acceptance, allowed_paths, budget, requested_model, effort,
            priority, assignee, due_at, parent_id, milestone, start_at,
            target_at, progress, blocked_reason, next_action, github_issue_id,
            github_issue_number, github_issue_url, github_project_item_id,
            codex_thread_id, pm_session_id, sync_state, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, project_id, title, type, "open", brief, risk, complexity,
            acceptance, allowed_paths, budget, requested_model, effort,
            priority, assignee, due_at, parent_id, milestone, start_at,
            target_at, progress, blocked_reason, next_action, github_issue_id,
            github_issue_number, github_issue_url, github_project_item_id,
            codex_thread_id, pm_session_id, sync_state, ts, ts,
        ),
    )
    _insert_activity(
        conn,
        project_id=project_id,
        task_id=tid,
        actor_type=actor_type,
        actor_name=actor_name or "cortex",
        action="task.created",
        summary=f"Created work item: {title}",
        source=source,
        source_ref=tid,
    )
    if commit:
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


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor_type: str = "system",
    actor_name: str | None = None,
    source: str = "cortex",
    commit: bool = True,
    **fields: Any,
) -> None:
    if not fields:
        return
    current = get_task(conn, task_id)
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
    if "progress" in fields:
        fields["progress"] = max(0, min(100, int(fields["progress"])))
    if "parent_id" in fields and fields["parent_id"]:
        if fields["parent_id"] == task_id:
            raise ValueError("task cannot be its own parent")
        parent = get_task(conn, fields["parent_id"])
        if parent["project_id"] != current["project_id"]:
            raise ValueError("parent task must belong to the same project")
        if _parent_would_cycle(conn, task_id, fields["parent_id"]):
            raise ValueError("parent relationship would create a cycle")
    if fields.get("status") == "done":
        fields.setdefault("completed_at", ids.now())
        fields["progress"] = 100
    elif "status" in fields and current["status"] == "done":
        fields.setdefault("completed_at", None)
    fields["updated_at"] = ids.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
    )
    changes = {
        key: {"from": current[key], "to": value}
        for key, value in fields.items()
        if key != "updated_at" and key in current.keys() and current[key] != value
    }
    if changes:
        action = "task.status_changed" if "status" in changes else "task.updated"
        if "status" in changes:
            summary = f"Changed {current['title']} from {changes['status']['from']} to {changes['status']['to']}"
        else:
            summary = f"Updated work item: {current['title']}"
        _insert_activity(
            conn,
            project_id=current["project_id"],
            task_id=task_id,
            actor_type=actor_type,
            actor_name=actor_name or "cortex",
            action=action,
            summary=summary,
            source=source,
            source_ref=task_id,
            evidence=changes,
        )
    if commit:
        conn.commit()


def add_task_dependency(
    conn: sqlite3.Connection,
    task_id: str,
    depends_on_task_id: str,
    *,
    type: str = "blocks",
    actor_name: str | None = None,
    source: str = "cortex",
) -> None:
    if task_id == depends_on_task_id:
        raise ValueError("task cannot depend on itself")
    if type not in DEPENDENCY_TYPES:
        raise ValueError(f"invalid dependency type: {type}")
    task = get_task(conn, task_id)
    upstream = get_task(conn, depends_on_task_id)
    if task["project_id"] != upstream["project_id"]:
        raise ValueError("dependency tasks must belong to the same project")
    if _dependency_would_cycle(conn, task_id, depends_on_task_id):
        raise ValueError("dependency would create a cycle")
    conn.execute(
        """INSERT INTO task_dependencies
           (task_id, depends_on_task_id, type, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(task_id, depends_on_task_id) DO UPDATE SET type=excluded.type""",
        (task_id, depends_on_task_id, type, ids.now()),
    )
    _insert_activity(
        conn,
        project_id=task["project_id"],
        task_id=task_id,
        actor_name=actor_name,
        action="task.dependency_added",
        summary=f"{task['title']} is blocked by {upstream['title']}",
        source=source,
        source_ref=depends_on_task_id,
    )
    conn.commit()


def list_task_dependencies(
    conn: sqlite3.Connection, task_id: str | None = None
) -> list[sqlite3.Row]:
    where = "WHERE dependency.task_id = ?" if task_id else ""
    params = (task_id,) if task_id else ()
    return conn.execute(
        f"""SELECT dependency.*, task.title AS task_title,
                   upstream.title AS depends_on_title
            FROM task_dependencies AS dependency
            JOIN tasks AS task ON task.id = dependency.task_id
            JOIN tasks AS upstream ON upstream.id = dependency.depends_on_task_id
            {where}
            ORDER BY dependency.created_at""",
        params,
    ).fetchall()


def remove_task_dependency(
    conn: sqlite3.Connection,
    task_id: str,
    depends_on_task_id: str,
    *,
    actor_name: str | None = None,
    source: str = "cortex",
) -> None:
    task = get_task(conn, task_id)
    upstream = get_task(conn, depends_on_task_id)
    cursor = conn.execute(
        "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
        (task_id, depends_on_task_id),
    )
    if cursor.rowcount == 0:
        raise NotFound(f"dependency not found: {task_id} -> {depends_on_task_id}")
    _insert_activity(
        conn,
        project_id=task["project_id"],
        task_id=task_id,
        actor_name=actor_name or "cortex",
        action="task.dependency_removed",
        summary=f"Removed blocker {upstream['title']} from {task['title']}",
        source=source,
        source_ref=depends_on_task_id,
    )
    conn.commit()


def start_pm_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    task_id: str | None = None,
    owner: str = "codex",
    acceptance: str | None = None,
    next_action: str | None = None,
    codex_thread_id: str | None = None,
    source: str = "codex-pm",
) -> tuple[str, str]:
    session_id = ids.short_id()
    try:
        if task_id:
            task = get_task(conn, task_id)
            if task["project_id"] != project_id:
                raise ValueError("PM task must belong to the selected project")
            updates: dict[str, Any] = {
                "status": "running", "assignee": owner,
                "pm_session_id": session_id,
            }
            if acceptance is not None:
                updates["acceptance"] = acceptance
            if next_action is not None:
                updates["next_action"] = next_action
            if codex_thread_id is not None:
                updates["codex_thread_id"] = codex_thread_id
            update_task(
                conn, task_id, actor_type="agent", actor_name=owner, source=source,
                commit=False, **updates,
            )
        else:
            task_id = create_task(
                conn,
                project_id=project_id,
                title=title,
                type="planning",
                acceptance=acceptance,
                next_action=next_action,
                codex_thread_id=codex_thread_id,
                pm_session_id=session_id,
                assignee=owner,
                actor_type="agent",
                actor_name=owner,
                source=source,
                commit=False,
            )
            update_task(
                conn, task_id, status="running", actor_type="agent", actor_name=owner,
                source=source, commit=False,
            )
        create_activity_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            actor_type="agent",
            actor_name=owner,
            action="pm.session_started",
            summary=f"PM session started: {title}",
            source=source,
            source_ref=codex_thread_id or task_id,
            session_id=session_id,
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return task_id, session_id


def close_pm_session(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    summary: str,
    owner: str = "codex",
    next_action: str | None = None,
    evidence: Any = None,
    source: str = "codex-pm",
    session_id: str | None = None,
) -> str:
    if status not in {"done", "review", "blocked", "in_progress"}:
        raise ValueError("PM close status must be done, review, blocked, or in_progress")
    task = get_task(conn, task_id)
    updates: dict[str, Any] = {"status": status}
    if next_action is not None:
        updates["next_action"] = next_action
    if status == "blocked" and summary:
        updates["blocked_reason"] = summary
    session_id = session_id or task["pm_session_id"] or ids.short_id()
    try:
        update_task(
            conn, task_id, actor_type="agent", actor_name=owner, source=source,
            commit=False, **updates,
        )
        create_activity_event(
            conn,
            project_id=task["project_id"],
            task_id=task_id,
            actor_type="agent",
            actor_name=owner,
            action="pm.session_closed",
            summary=summary,
            source=source,
            source_ref=task["codex_thread_id"] or task_id,
            session_id=session_id,
            evidence=evidence,
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return session_id


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
    _insert_activity(
        conn,
        project_id=project_id,
        task_id=task_id,
        actor_name=str(model or "").split(":")[0] or None,
        model=model,
        action="run.started",
        summary=f"Started execution with {model or 'default model'}",
        source="cortex-run",
        source_ref=rid,
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
    current = get_run(conn, run_id)
    if "files_changed" in fields and not isinstance(fields["files_changed"], str):
        fields["files_changed"] = json.dumps(fields["files_changed"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id)
    )
    if fields.get("ended_at") and not current["ended_at"]:
        exit_code = fields.get("exit_code")
        outcome = fields.get("outcome") or current["outcome"]
        summary = "Execution completed"
        if exit_code not in {None, 0}:
            summary = f"Execution failed with exit code {exit_code}"
        elif outcome and outcome != "unknown":
            summary = f"Execution completed: {outcome}"
        _insert_activity(
            conn,
            project_id=current["project_id"],
            task_id=current["task_id"],
            actor_name=str(current["model"] or "").split(":")[0] or None,
            model=current["model"],
            action="run.completed",
            summary=summary,
            source="cortex-run",
            source_ref=run_id,
            evidence={"exit_code": exit_code, "outcome": outcome},
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
    _insert_activity(
        conn,
        project_id=project_id,
        actor_type="human" if source == "manual" else "agent",
        actor_name=source,
        action="decision.recorded",
        summary=decision,
        source=source,
        source_ref=did,
        evidence={"rationale": rationale} if rationale else None,
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
