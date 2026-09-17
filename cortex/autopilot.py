"""The unattended scheduler: keep approved read-only work moving.

One process at a time holds the ``autopilot`` lease. Each tick it:

1. renews the lease (and stops if another process took it over);
2. recovers work abandoned by a dead process, marking it ``unknown`` and filing
   an inbox item instead of replaying it;
3. surfaces queued inbox items if today's cap has room;
4. unless paused, refreshes quota and starts up to ``max_concurrent`` runs from
   ``team.safe_start_candidates``. That applies the project rules, trading
   guard, quota reserve, local lane and worker holds. Each run gets a supervisor.

Starts are sequential: each waits until its run row exists, so the next
admission check counts it. That closes the unknown-quota race Phase 1 noted.
Pausing stops new starts; in-flight runs finish or hit their limits.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import (
    autonomy, capacity, config, db, dispatcher, ids, inbox, jobs, leases, routing,
    settings, store, supervisor, team,
)

MAX_CONCURRENT_KEY = "autopilot.max_concurrent"
DEFAULT_MAX_CONCURRENT = 3
INTERVAL_KEY = "autopilot.interval_seconds"
DEFAULT_INTERVAL = 60
LEASE_TTL = timedelta(minutes=3)
RUN_START_WAIT = 120.0


class LeaseLost(RuntimeError):
    """Another scheduler took over; this one must stop starting work."""


@dataclass
class TickReport:
    at: str
    paused: bool = False
    started: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    recovered: int = 0
    promoted: int = 0
    in_flight: int = 0
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at, "paused": self.paused, "started": self.started,
            "skipped": self.skipped, "recovered": self.recovered,
            "promoted": self.promoted, "in_flight": self.in_flight, "note": self.note,
        }


def max_concurrent(conn: sqlite3.Connection) -> int:
    return max(1, min(6, int(settings.get_float(conn, MAX_CONCURRENT_KEY, DEFAULT_MAX_CONCURRENT))))


def set_max_concurrent(conn: sqlite3.Connection, value: int) -> int:
    value = max(1, min(6, int(value)))
    settings.set_value(conn, MAX_CONCURRENT_KEY, str(value))
    return value


def interval_seconds(conn: sqlite3.Connection) -> int:
    return max(15, min(3600, int(settings.get_float(conn, INTERVAL_KEY, DEFAULT_INTERVAL))))


class Autopilot:
    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        holder: str | None = None,
        dispatch_fn: Callable[..., Any] = dispatcher.dispatch,
        candidates_fn: Callable[..., list[str]] = team.safe_start_candidates,
        refresh_fn: Callable[..., Any] = capacity.refresh,
    ) -> None:
        self.database_path = Path(database_path) if database_path else config.db_path()
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{ids.short_id()[:6]}"
        self.lease: leases.Lease | None = None
        self._dispatch = dispatch_fn
        self._candidates = candidates_fn
        self._refresh = refresh_fn
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.last_report: TickReport | None = None

    # -- lease ---------------------------------------------------------------

    def acquire(self) -> bool:
        with db.connect(self.database_path) as conn:
            self.lease = leases.acquire(
                conn, leases.SCHEDULER_LEASE, self.holder, ttl=LEASE_TTL,
                detail={"started_at": ids.now()},
            )
        return self.lease is not None

    def release(self) -> None:
        if self.lease is None:
            return
        with db.connect(self.database_path) as conn:
            leases.release(conn, self.lease)
        self.lease = None

    def _renew(self, conn: sqlite3.Connection, report: TickReport | None = None) -> None:
        assert self.lease is not None
        detail = {"last_tick": report.as_dict() if report else None, "in_flight": self.in_flight()}
        if not leases.renew(conn, self.lease, ttl=LEASE_TTL, detail=detail):
            raise LeaseLost("another Cortex scheduler holds the lease")

    # -- tick ----------------------------------------------------------------

    def in_flight(self) -> list[str]:
        with self._lock:
            for job_id in [j for j, t in self._threads.items() if not t.is_alive()]:
                del self._threads[job_id]
            return list(self._threads)

    def tick(self) -> TickReport:
        if self.lease is None:
            raise LeaseLost("tick called without holding the scheduler lease")
        report = TickReport(at=ids.now())
        with db.connect(self.database_path) as conn:
            self._renew(conn)
            for item in jobs.reconcile_details(conn):
                report.recovered += 1
                self._file_interrupted(conn, item)
            report.promoted = inbox.promote(conn)
            running = self.in_flight()
            report.in_flight = len(running)
            if autonomy.is_paused(conn):
                report.paused = True
                report.note = "Autonomy is paused; no new work was started."
                self._finish_tick(conn, report)
                return report
            free = max_concurrent(conn) - len(running)
            if free <= 0:
                report.note = "All scheduler slots are busy."
                self._finish_tick(conn, report)
                return report
            try:
                self._refresh(conn, probe_stale=True, force=False)
            except Exception as exc:  # stale quota is handled by admission
                report.note = f"Quota refresh failed: {exc}"
            task_ids = self._candidates(conn, limit=free, skipped=report.skipped)
        for task_id in task_ids:
            with db.connect(self.database_path) as conn:
                if not leases.holds(conn, self.lease):
                    raise LeaseLost("scheduler lease expired during the tick")
                started = self._start(conn, task_id)
            if started:
                report.started.append(started)
        report.in_flight = len(self.in_flight())
        with db.connect(self.database_path) as conn:
            self._finish_tick(conn, report)
        return report

    def _finish_tick(self, conn: sqlite3.Connection, report: TickReport) -> None:
        self.last_report = report
        self._renew(conn, report)

    # -- runs ----------------------------------------------------------------

    def _start(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
        route = routing.effective_route(project, task)
        job_id = jobs.create(
            conn, kind="dispatch", label="Autopilot", task_id=task_id,
            project_id=project["id"], started_by=dispatcher.SCHEDULER,
        )
        run_started = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(job_id, task_id, route.worker, run_started),
            name=f"autopilot-{job_id}",
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        # Wait for the run row (or an early failure) before admitting the next
        # candidate, so capacity checks see this start.
        run_started.wait(RUN_START_WAIT)
        return {"job_id": job_id, "task_id": task_id, "worker": route.worker,
                "project": project["name"], "title": task["title"]}

    def _run(self, job_id: str, task_id: str, worker: str, run_started: threading.Event) -> None:
        limits = supervisor.limits_for(worker)
        watch = supervisor.RunSupervisor(
            limits, database_path=self.database_path, job_id=job_id
        )
        conn = db.connect(self.database_path)
        run_id: str | None = None

        def on_start(new_run_id: str) -> None:
            nonlocal run_id
            run_id = new_run_id
            jobs.attach_run(conn, job_id, new_run_id)
            run_started.set()

        try:
            task = store.get_task(conn, task_id)
            result = self._dispatch(
                conn, task, started_by=dispatcher.SCHEDULER, on_run_start=on_start,
                watchdog=watch, timeout=limits.max_seconds,
                observer=watch.observe,
            )
            outcome = {"exit_code": result.exit_code, "task_status": result.task_status}
            jobs.finish(conn, job_id, status="done" if result.exit_code == 0 else "failed",
                        result=outcome, run_id=result.run_id)
            if result.exit_code != 0:
                self._file_failure(conn, task, worker, result.run_id,
                                   f"{worker} exited with code {result.exit_code}.")
        except dispatcher.DispatchError as exc:
            run_id = exc.run_id or run_id
            jobs.finish(conn, job_id, status="failed", error=str(exc), run_id=run_id)
            if run_id:
                task = store.get_task(conn, task_id)
                evidence = watch.snapshot() if watch.stopped_reason else None
                if evidence:
                    store.create_activity_event(
                        conn, project_id=task["project_id"], task_id=task_id,
                        actor_type="system", actor_name="cortex-scheduler",
                        action="run.supervisor_stopped",
                        summary=f"Supervisor stopped {worker}: {watch.stopped_reason}"[:500],
                        source="cortex-scheduler", source_ref=run_id, evidence=evidence,
                    )
                self._file_failure(conn, task, worker, run_id, str(exc))
            else:
                # Refused before a run existed (quota moved, guard fired): the
                # task stays assigned and will be reconsidered next tick.
                store.create_activity_event(
                    conn, project_id=store.get_task(conn, task_id)["project_id"],
                    task_id=task_id, actor_type="system", actor_name="cortex-scheduler",
                    action="run.unattended_refused", summary=str(exc)[:500],
                    source="cortex-scheduler", source_ref=job_id,
                )
        except Exception as exc:  # never let one run kill the scheduler
            jobs.finish(conn, job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                        run_id=run_id)
            inbox.add(
                conn, kind="scheduler_error",
                title=f"Scheduler error while running a task: {type(exc).__name__}",
                detail=str(exc), task_id=task_id, run_id=run_id,
                dedupe_key=f"job:{job_id}",
            )
        finally:
            run_started.set()
            watch.close()
            conn.close()

    def _file_failure(
        self, conn: sqlite3.Connection, task: sqlite3.Row, worker: str,
        run_id: str, reason: str,
    ) -> None:
        # The run records the worker that actually ran; prefer it to the route.
        run = conn.execute("SELECT model FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run and run["model"]:
            worker = str(run["model"]).split(":", 1)[0]
        inbox.add(
            conn, kind="run_failed",
            title=f"Unattended {worker} run did not finish: {task['title']}",
            detail=reason, project_id=task["project_id"], task_id=task["id"],
            run_id=run_id, dedupe_key=f"run:{run_id}",
        )
        streak = supervisor.failure_streak(conn, worker)
        if streak >= supervisor.FAILURE_STREAK and not supervisor.worker_hold(conn, worker):
            until = supervisor.hold_worker(conn, worker)
            inbox.add(
                conn, kind="worker_hold",
                title=f"{worker} put on hold after {streak} failed unattended runs",
                detail=f"No new unattended {worker} work until {until}. Last failure: {reason}",
                run_id=run_id, dedupe_key=f"hold:{worker}:{until[:13]}",
            )

    def _file_interrupted(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        if item["kind"] != "dispatch" or not item["task_id"]:
            return
        task = conn.execute("SELECT title FROM tasks WHERE id = ?", (item["task_id"],)).fetchone()
        inbox.add(
            conn, kind="interrupted",
            title=f"Run interrupted and not retried: {task['title'] if task else item['task_id']}",
            detail=(
                "The process running this task stopped before it finished. Its outcome "
                "is unknown; the task is blocked until you requeue it."
            ),
            project_id=item["project_id"], task_id=item["task_id"],
            run_id=(item["run_ids"] or [None])[0],
            dedupe_key=f"interrupted:{item['job_id']}",
        )

    # -- loop ----------------------------------------------------------------

    def run_forever(
        self, stop: threading.Event, *, interval: int | None = None,
        on_tick: Callable[[TickReport], None] | None = None,
    ) -> None:
        try:
            while not stop.is_set():
                report = self.tick()
                if on_tick:
                    on_tick(report)
                with db.connect(self.database_path) as conn:
                    wait = interval or interval_seconds(conn)
                stop.wait(wait)
        finally:
            # Let in-flight runs finish under their own limits before letting go.
            for thread in list(self._threads.values()):
                thread.join()
            self.release()


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    lease = leases.describe(conn, leases.SCHEDULER_LEASE)
    running = conn.execute(
        """SELECT j.id, j.task_id, j.run_id, j.created_at, j.heartbeat_at,
                  t.title, p.name AS project_name, r.model
           FROM jobs j
           LEFT JOIN tasks t ON t.id = j.task_id
           LEFT JOIN projects p ON p.id = j.project_id
           LEFT JOIN runs r ON r.id = j.run_id
           WHERE j.status = 'running' AND j.started_by = 'scheduler'
           ORDER BY j.created_at""",
    ).fetchall()
    recent = conn.execute(
        """SELECT occurred_at, action, summary, model FROM activity_events
           WHERE actor_name = 'cortex-scheduler'
           ORDER BY occurred_at DESC LIMIT 15""",
    ).fetchall()
    stops = recent_stops(conn)
    holds = {
        worker: reason
        for worker in (*capacity.METERED, *capacity.COUNTED, *capacity.LOCAL)
        if (reason := supervisor.worker_hold(conn, worker))
    }
    return {
        "lease": lease,
        "running": lease is not None and bool(lease["active"]),
        "paused": autonomy.is_paused(conn),
        "max_concurrent": max_concurrent(conn),
        "interval_seconds": interval_seconds(conn),
        "in_flight": [{key: row[key] for key in row.keys()} for row in running],
        "recent": [{key: row[key] for key in row.keys()} for row in recent],
        "holds": holds,
        "recent_stops": stops,
        "inbox": inbox.payload(conn),
    }


def recent_stops(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    """Unattended runs that did not finish cleanly, newest first.

    This is the evidence for tuning ``supervisor.WORKER_LIMITS``: it pairs each
    stop with what the supervisor had counted when it intervened.
    """
    rows = conn.execute(
        """SELECT r.id, r.model, r.started_at, r.ended_at, r.exit_code, r.human_note,
                  t.title, p.name AS project_name, e.evidence_json
           FROM runs r
           LEFT JOIN tasks t ON t.id = r.task_id
           LEFT JOIN projects p ON p.id = r.project_id
           LEFT JOIN activity_events e
                  ON e.source_ref = r.id AND e.action = 'run.supervisor_stopped'
           WHERE r.started_by = 'scheduler' AND r.ended_at IS NOT NULL
             AND (r.exit_code IS NULL OR r.exit_code != 0)
           ORDER BY r.ended_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    stops = []
    for row in rows:
        item = {key: row[key] for key in row.keys() if key != "evidence_json"}
        item["worker"] = str(row["model"] or "").split(":", 1)[0] or None
        try:
            item["supervisor"] = json.loads(row["evidence_json"]) if row["evidence_json"] else None
        except json.JSONDecodeError:
            item["supervisor"] = None
        stops.append(item)
    return stops


def dumps(report: TickReport) -> str:
    return json.dumps(report.as_dict(), indent=2)
