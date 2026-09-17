"""The owner's decision inbox, capped per day.

Unattended work only helps if it does not turn into a stream of questions. Items
beyond the daily cap are kept as ``queued`` and surface on a later day, while
the scheduler keeps working on tasks that need no answer. A ``dedupe_key`` makes
asking the same question twice a no-op.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import ids, settings

CAP_KEY = "inbox.daily_cap"
DEFAULT_CAP = 8
OPEN = "open"
QUEUED = "queued"
CLOSED = {"resolved", "dismissed"}


def daily_cap(conn: sqlite3.Connection) -> int:
    return max(1, int(settings.get_float(conn, CAP_KEY, DEFAULT_CAP)))


def set_daily_cap(conn: sqlite3.Connection, value: int) -> int:
    value = max(1, min(50, int(value)))
    settings.set_value(conn, CAP_KEY, str(value))
    return value


def surfaced_today(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM inbox WHERE surfaced_at >= ?", (ids.today() + "T00:00:00Z",)
    ).fetchone()[0]


def add(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    detail: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    dedupe_key: str | None = None,
) -> str | None:
    """File an item; returns its id, or None when the same key already exists."""
    if dedupe_key:
        existing = conn.execute(
            "SELECT id FROM inbox WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        if existing:
            return None
    now = ids.now()
    room = surfaced_today(conn) < daily_cap(conn)
    item_id = ids.short_id()
    conn.execute(
        """INSERT INTO inbox (id, kind, status, title, detail, project_id, task_id,
                              run_id, dedupe_key, created_at, surfaced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, kind, OPEN if room else QUEUED, title, (detail or "")[:4000] or None,
            project_id, task_id, run_id, dedupe_key, now, now if room else None,
        ),
    )
    conn.commit()
    return item_id


def promote(conn: sqlite3.Connection) -> int:
    """Surface queued items, oldest first, while today's cap has room."""
    room = daily_cap(conn) - surfaced_today(conn)
    if room <= 0:
        return 0
    rows = conn.execute(
        "SELECT id FROM inbox WHERE status = ? ORDER BY created_at LIMIT ?", (QUEUED, room)
    ).fetchall()
    now = ids.now()
    for row in rows:
        conn.execute(
            "UPDATE inbox SET status = ?, surfaced_at = ? WHERE id = ?", (OPEN, now, row["id"])
        )
    conn.commit()
    return len(rows)


def resolve(
    conn: sqlite3.Connection, item_id: str, *, status: str = "resolved", note: str | None = None
) -> bool:
    if status not in CLOSED:
        raise ValueError(f"status must be one of {sorted(CLOSED)}")
    cursor = conn.execute(
        """UPDATE inbox SET status = ?, resolved_at = ?, resolution = ?
           WHERE id = ? AND status IN (?, ?)""",
        (status, ids.now(), note, item_id, OPEN, QUEUED),
    )
    conn.commit()
    return cursor.rowcount == 1


def items(conn: sqlite3.Connection, *, include_closed: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    where = "" if include_closed else "WHERE i.status IN ('open', 'queued')"
    rows = conn.execute(
        f"""SELECT i.*, p.name AS project_name, t.title AS task_title
            FROM inbox i
            LEFT JOIN projects p ON p.id = i.project_id
            LEFT JOIN tasks t ON t.id = i.task_id
            {where}
            ORDER BY CASE i.status WHEN 'open' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                     i.created_at DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def payload(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = items(conn)
    return {
        "items": rows,
        "open": sum(1 for row in rows if row["status"] == OPEN),
        "queued": sum(1 for row in rows if row["status"] == QUEUED),
        "daily_cap": daily_cap(conn),
        "surfaced_today": surfaced_today(conn),
    }
