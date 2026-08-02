"""Durable record of background work started by the dashboard.

Jobs used to live in a dict on the server object, so restarting the dashboard
lost every in-flight run while its task stayed marked ``running`` forever. They
are rows now: a restart can see what was interrupted and say so, instead of
leaving the UI showing agents that no longer exist.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from . import ids

ACTIVE = "running"
TERMINAL = {"done", "failed", "interrupted"}


def create(
    conn: sqlite3.Connection,
    *,
    kind: str,
    label: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
) -> str:
    job_id = ids.short_id()
    conn.execute(
        """INSERT INTO jobs
           (id, kind, status, task_id, project_id, label, pid, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, kind, ACTIVE, task_id, project_id, label, os.getpid(), ids.now()),
    )
    conn.commit()
    return job_id


def finish(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    result: Any = None,
    error: str | None = None,
    run_id: str | None = None,
) -> None:
    conn.execute(
        """UPDATE jobs
           SET status = ?, result_json = ?, error = ?, run_id = COALESCE(?, run_id),
               completed_at = ?
           WHERE id = ?""",
        (
            status,
            json.dumps(result, default=str) if result is not None else None,
            error,
            run_id,
            ids.now(),
            job_id,
        ),
    )
    conn.commit()


def attach_run(conn: sqlite3.Connection, job_id: str, run_id: str) -> None:
    """Link a job to the run it started, so the UI can tail the right log."""
    conn.execute("UPDATE jobs SET run_id = ? WHERE id = ?", (run_id, job_id))
    conn.commit()


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _payload(row) if row else None


def recent(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_payload(row) for row in rows]


def reconcile(conn: sqlite3.Connection) -> int:
    """Close out jobs and runs abandoned by a previous dashboard process.

    A job marked running whose owning process is gone cannot make progress. Its
    task would otherwise sit in `running` forever and be skipped by every
    scheduler, so it is returned to `blocked` where a person can see it.
    """
    active = conn.execute(
        "SELECT * FROM jobs WHERE status = ?", (ACTIVE,)
    ).fetchall()
    stale = [row for row in active if not _pid_is_running(row["pid"])]
    if not stale:
        return 0
    now = ids.now()
    for row in stale:
        conn.execute(
            """UPDATE jobs SET status = 'interrupted', error = ?, completed_at = ?
               WHERE id = ?""",
            ("Dashboard restarted while this job was running.", now, row["id"]),
        )
        run_ids: list[str] = []
        if row["run_id"]:
            run_ids.append(str(row["run_id"]))
        elif row["kind"] == "dispatch" and row["task_id"]:
            # Dispatch creates the run immediately before attaching it to the
            # durable job. If the process dies in that small window, recover
            # only the matching headless run; manual and agentic CLI runs are
            # intentionally left open for their owner to finish later.
            run_ids.extend(
                str(candidate["id"])
                for candidate in conn.execute(
                    """SELECT id FROM runs
                       WHERE task_id = ? AND execution_mode = 'headless_cli'
                         AND ended_at IS NULL AND started_at >= ?""",
                    (row["task_id"], row["created_at"]),
                ).fetchall()
            )
        for run_id in run_ids:
            conn.execute(
                """UPDATE runs SET ended_at = ?, exit_code = COALESCE(exit_code, 1),
                       human_note = COALESCE(human_note, ?)
                   WHERE id = ? AND ended_at IS NULL""",
                (now, "Interrupted by a dashboard restart.", run_id),
            )
        if row["kind"] == "dispatch" and row["task_id"]:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, row["task_id"]),
            )
    conn.commit()
    return len(stale)


def _pid_is_running(pid: int | None) -> bool:
    """Return whether a job's recorded owner process still exists."""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        # The process exists but this account cannot signal it.
        return True
    except OSError:
        return False
    return True


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    if item.get("result_json"):
        try:
            item["result"] = json.loads(item["result_json"])
        except json.JSONDecodeError:
            item["result"] = None
    else:
        item["result"] = None
    item.pop("result_json", None)
    return item
