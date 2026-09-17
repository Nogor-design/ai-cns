"""Database leases: at most one holder per name, with fencing.

The scheduler may be started twice by accident (a scheduled task plus a manual
terminal, or a restart before the old process has died). A lease row makes the
second one wait. Every takeover increments ``fence``; a holder checks its fence
before starting work, so a process that was presumed dead and replaced cannot
keep dispatching alongside its successor. This mirrors the CNS runtime's fenced
recovery, reduced to what one SQLite file and one machine need.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import ids
from .jobs import _pid_is_running

SCHEDULER_LEASE = "autopilot"


@dataclass(frozen=True)
class Lease:
    name: str
    holder: str
    fence: int


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def acquire(
    conn: sqlite3.Connection,
    name: str,
    holder: str,
    *,
    ttl: timedelta = timedelta(minutes=3),
    now: datetime | None = None,
    pid: int | None = None,
    detail: dict[str, Any] | None = None,
) -> Lease | None:
    """Take the lease if it is free, expired, or held by a dead process."""
    moment = now or datetime.now(timezone.utc)
    pid = os.getpid() if pid is None else pid
    if conn.in_transaction:
        conn.commit()
    # IMMEDIATE takes the write lock before reading, so two schedulers starting
    # together cannot both see the lease as free.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM leases WHERE name = ?", (name,)).fetchone()
        if row is not None and row["holder"] != holder:
            expires = _parse(row["expires_at"])
            live = expires is not None and expires > moment
            if live and _pid_is_running(row["pid"]):
                conn.rollback()
                return None
        fence = (row["fence"] if row is not None else 0) + 1
        conn.execute(
            """INSERT INTO leases
                   (name, holder, pid, fence, acquired_at, heartbeat_at, expires_at, detail_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   holder = excluded.holder, pid = excluded.pid, fence = excluded.fence,
                   acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at,
                   expires_at = excluded.expires_at, detail_json = excluded.detail_json""",
            (
                name, holder, pid, fence, _stamp(moment), _stamp(moment),
                _stamp(moment + ttl), json.dumps(detail or {}),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return Lease(name, holder, fence)


def renew(
    conn: sqlite3.Connection,
    lease: Lease,
    *,
    ttl: timedelta = timedelta(minutes=3),
    now: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Extend the lease; False means it was taken over and work must stop."""
    moment = now or datetime.now(timezone.utc)
    cursor = conn.execute(
        """UPDATE leases SET heartbeat_at = ?, expires_at = ?,
               detail_json = COALESCE(?, detail_json)
           WHERE name = ? AND holder = ? AND fence = ?""",
        (
            _stamp(moment), _stamp(moment + ttl),
            json.dumps(detail) if detail is not None else None,
            lease.name, lease.holder, lease.fence,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def holds(conn: sqlite3.Connection, lease: Lease, *, now: datetime | None = None) -> bool:
    """Whether ``lease`` is still current: same fence and not expired."""
    moment = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM leases WHERE name = ?", (lease.name,)).fetchone()
    if row is None or row["holder"] != lease.holder or row["fence"] != lease.fence:
        return False
    expires = _parse(row["expires_at"])
    return expires is not None and expires > moment


def release(conn: sqlite3.Connection, lease: Lease) -> None:
    conn.execute(
        "UPDATE leases SET expires_at = ? WHERE name = ? AND holder = ? AND fence = ?",
        (ids.now(), lease.name, lease.holder, lease.fence),
    )
    conn.commit()


def describe(conn: sqlite3.Connection, name: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM leases WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    moment = now or datetime.now(timezone.utc)
    expires = _parse(row["expires_at"])
    try:
        detail = json.loads(row["detail_json"] or "{}")
    except json.JSONDecodeError:
        detail = {}
    return {
        "holder": row["holder"],
        "pid": row["pid"],
        "fence": row["fence"],
        "acquired_at": row["acquired_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "active": bool(expires and expires > moment and _pid_is_running(row["pid"])),
        "detail": detail,
    }
