"""Watch unattended runs and stop the ones that are wasting quota.

Runaway runs are the largest avoidable token cost: an agent that re-reads the
same file, wanders across a disk (Antigravity used 26 steps and 134k tokens on a
vague brief during Phase 1), or sits silent until the timeout. The supervisor
sees the same rendered event stream the dashboard shows and stops a run when:

- it goes silent for longer than its stall limit;
- it exceeds its tool-call budget;
- it repeats one action many times in a short window.

Across runs, a worker that fails several unattended runs in a row is put on
hold so the scheduler stops feeding it work until someone looks.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, ids, settings

HOLD_PREFIX = "autopilot.hold."
FAILURE_STREAK = 3
HOLD_DURATION = timedelta(hours=1)
HEARTBEAT_EVERY = 30.0
_ACTION_MARKERS = ("→", "$ ")


@dataclass(frozen=True)
class Limits:
    max_seconds: int = 1800
    stall_seconds: int = 900
    max_tool_calls: int = 80
    repeat_window: int = 12
    repeat_limit: int = 5


DEFAULT_LIMITS = Limits()
WORKER_LIMITS: dict[str, Limits] = {
    # Wanders on vague briefs and its file tools are not workspace-confined.
    "agy": Limits(max_seconds=1200, stall_seconds=600, max_tool_calls=25),
    "opencode": Limits(max_seconds=1200, stall_seconds=600, max_tool_calls=60),
    # Local prefill of opencode's ~18k-token prompt is slow on a split model.
    "opencode-local": Limits(max_seconds=1800, stall_seconds=900, max_tool_calls=40),
    # High-effort Codex can think silently for a long time.
    "codex": Limits(max_seconds=1800, stall_seconds=1200, max_tool_calls=80),
    "ollama": Limits(max_seconds=1200, stall_seconds=600, max_tool_calls=0),
}


def limits_for(worker: str) -> Limits:
    return WORKER_LIMITS.get(worker, DEFAULT_LIMITS)


class RunSupervisor:
    """A watchdog for one run: feed it output with ``observe``, poll ``check``."""

    def __init__(
        self,
        limits: Limits,
        *,
        database_path: str | Path | None = None,
        job_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self.database_path = database_path
        self.job_id = job_id
        self._clock = clock
        self._lock = threading.Lock()
        self._last_output = clock()
        self._last_heartbeat = float("-inf")
        self._conn: sqlite3.Connection | None = None
        self.tool_calls = 0
        self._recent: deque[str] = deque(maxlen=limits.repeat_window)
        self.stopped_reason: str | None = None

    def observe(self, text: str) -> None:
        """Called from the output thread with each rendered chunk."""
        with self._lock:
            self._last_output = self._clock()
            for line in text.splitlines():
                action = line.strip()
                if action.startswith(_ACTION_MARKERS):
                    self.tool_calls += 1
                    self._recent.append(action)

    def check(self) -> str | None:
        """Polled from the run thread; returns a reason to stop, or None."""
        self._heartbeat()
        with self._lock:
            reason = self._reason()
        if reason:
            self.stopped_reason = reason
        return reason

    __call__ = check

    def _reason(self) -> str | None:
        limits = self.limits
        if limits.max_tool_calls and self.tool_calls > limits.max_tool_calls:
            return f"used more than {limits.max_tool_calls} tool calls"
        if self._recent:
            action, count = Counter(self._recent).most_common(1)[0]
            if count >= limits.repeat_limit:
                return f"repeated the same action {count} times ({action[:80]})"
        silent = self._clock() - self._last_output
        if silent > limits.stall_seconds:
            return f"no output for {int(silent // 60)} minutes"
        return None

    def _heartbeat(self) -> None:
        if not self.job_id or self.database_path is None:
            return
        now = self._clock()
        if now - self._last_heartbeat < HEARTBEAT_EVERY:
            return
        self._last_heartbeat = now
        try:
            if self._conn is None:
                self._conn = db.connect(self.database_path)
            self._conn.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (ids.now(), self.job_id)
            )
            self._conn.commit()
        except sqlite3.Error:
            pass  # a busy database must not stop a healthy run

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def worker_hold(conn: sqlite3.Connection, worker: str, *, now: datetime | None = None) -> str | None:
    """Why ``worker`` is on hold, or None."""
    until = settings.get(conn, HOLD_PREFIX + worker)
    if not until:
        return None
    moment = now or datetime.now(timezone.utc)
    try:
        expires = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if expires <= moment:
        return None
    return (
        f"{worker} is on hold until {until} after {FAILURE_STREAK} failed unattended "
        "runs in a row."
    )


def hold_worker(
    conn: sqlite3.Connection, worker: str, *, duration: timedelta = HOLD_DURATION,
    now: datetime | None = None,
) -> str:
    moment = now or datetime.now(timezone.utc)
    until = (moment + duration).strftime("%Y-%m-%dT%H:%M:%SZ")
    settings.set_value(conn, HOLD_PREFIX + worker, until)
    return until


def release_worker(conn: sqlite3.Connection, worker: str) -> None:
    settings.set_value(conn, HOLD_PREFIX + worker, "")


def failure_streak(conn: sqlite3.Connection, worker: str, *, window: int = 10) -> int:
    """Consecutive failed unattended runs for ``worker``, newest first."""
    rows = conn.execute(
        """SELECT exit_code FROM runs
           WHERE started_by = 'scheduler' AND model LIKE ? AND ended_at IS NOT NULL
           ORDER BY ended_at DESC, rowid DESC LIMIT ?""",
        (f"{worker}:%", window),
    ).fetchall()
    streak = 0
    for row in rows:
        if row["exit_code"] in (None, 0):
            break
        streak += 1
    return streak
