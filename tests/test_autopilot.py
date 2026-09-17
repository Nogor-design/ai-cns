"""Phase 2: scheduler lease, owner inbox, supervisor, trading guard, recovery."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import (
    autonomy, autopilot, db, dispatcher, ids, inbox, jobs, leases, routing, store,
    supervisor, trading_guard, workers,
)

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
DEAD_PID = 2_000_000_000


# -- leases -------------------------------------------------------------------

def test_lease_is_exclusive_and_fenced(conn):
    first = leases.acquire(conn, "autopilot", "a", now=NOW)
    assert first is not None and first.fence == 1
    assert leases.acquire(conn, "autopilot", "b", now=NOW + timedelta(seconds=30)) is None

    # Once the holder stops renewing, a successor takes over with a new fence
    # and the old holder can no longer renew.
    second = leases.acquire(conn, "autopilot", "b", now=NOW + timedelta(minutes=5))
    assert second is not None and second.fence == 2
    assert not leases.renew(conn, first, now=NOW + timedelta(minutes=5))
    assert not leases.holds(conn, first, now=NOW + timedelta(minutes=5))
    assert leases.holds(conn, second, now=NOW + timedelta(minutes=5))


def test_lease_held_by_dead_process_is_taken_over(conn):
    leases.acquire(conn, "autopilot", "ghost", now=NOW, pid=DEAD_PID,
                   ttl=timedelta(hours=1))
    taken = leases.acquire(conn, "autopilot", "live", now=NOW + timedelta(seconds=5))
    assert taken is not None and taken.fence == 2


def test_second_autopilot_cannot_start(isolated_db):
    first = autopilot.Autopilot(isolated_db, holder="one")
    second = autopilot.Autopilot(isolated_db, holder="two")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()


# -- inbox --------------------------------------------------------------------

def test_inbox_caps_daily_items_and_dedupes(conn):
    inbox.set_daily_cap(conn, 2)
    ids_ = [inbox.add(conn, kind="run_failed", title=f"item {n}", dedupe_key=f"k{n}")
            for n in range(3)]
    assert all(ids_)
    assert inbox.add(conn, kind="run_failed", title="again", dedupe_key="k0") is None
    box = inbox.payload(conn)
    assert (box["open"], box["queued"]) == (2, 1)
    assert inbox.promote(conn) == 0  # no room today

    assert inbox.resolve(conn, ids_[0])
    assert not inbox.resolve(conn, ids_[0])
    # Resolving does not free today's cap: the cap counts questions asked.
    assert inbox.promote(conn) == 0
    conn.execute("UPDATE inbox SET surfaced_at = '2000-01-01T00:00:00Z'")
    assert inbox.promote(conn) == 1
    assert inbox.payload(conn)["queued"] == 0


# -- supervisor ---------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_supervisor_stops_tool_spam_repeats_and_stalls():
    clock = FakeClock()
    capped = supervisor.RunSupervisor(
        supervisor.Limits(max_tool_calls=3, repeat_limit=99), clock=clock
    )
    for n in range(4):
        capped.observe(f"  → read_file(file{n}.py)\n")
    assert "more than 3 tool calls" in capped.check()

    looping = supervisor.RunSupervisor(supervisor.Limits(repeat_limit=3), clock=clock)
    looping.observe("  $ pytest -q\nthinking...\n  $ pytest -q\n")
    assert looping.check() is None
    looping.observe("  $ pytest -q\n")
    assert "repeated the same action 3 times" in looping.check()

    quiet = supervisor.RunSupervisor(supervisor.Limits(stall_seconds=60), clock=clock)
    clock.value += 30
    assert quiet.check() is None
    quiet.observe("still working\n")
    clock.value += 61
    assert "no output for 1 minutes" in quiet.check()


def test_agy_gets_a_tight_tool_budget():
    assert supervisor.limits_for("agy").max_tool_calls <= 30
    assert supervisor.limits_for("unknown-worker") == supervisor.DEFAULT_LIMITS


def test_watchdog_kills_a_running_worker(tmp_path):
    script = (
        "import sys, time\n"
        "print('  $ looking', flush=True)\n"
        "time.sleep(60)\n"
    )
    spec = workers.CommandSpec("test", (sys.executable, "-c", script), False)
    seen: list[str] = []
    calls = {"n": 0}

    def watchdog() -> str | None:
        calls["n"] += 1
        return "enough" if seen else None

    started = datetime.now()
    with pytest.raises(workers.WorkerStopped) as stopped:
        workers._stream_subprocess(
            spec, workspace=tmp_path, brief="", timeout=60,
            emit=seen.append, watchdog=watchdog,
        )
    assert stopped.value.reason == "enough"
    assert "looking" in stopped.value.stdout
    assert (datetime.now() - started).total_seconds() < 30


def test_failure_streak_and_hold(conn, project):
    task_id = store.create_task(conn, project_id=project["id"], title="t")
    for exit_code in (0, 1, 1, 1):
        run_id = store.create_run(conn, task_id=task_id, project_id=project["id"],
                                  model="grok:default", execution_mode="headless_cli")
        store.update_run(conn, run_id, started_by="scheduler", ended_at=ids.now(),
                         exit_code=exit_code)
    assert supervisor.failure_streak(conn, "grok") == 3
    assert supervisor.worker_hold(conn, "grok") is None
    supervisor.hold_worker(conn, "grok")
    assert "on hold" in supervisor.worker_hold(conn, "grok")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert supervisor.worker_hold(conn, "grok", now=later) is None
    supervisor.release_worker(conn, "grok")
    assert supervisor.worker_hold(conn, "grok") is None


# -- trading guard ------------------------------------------------------------

def test_trading_guard_builtin_patterns_without_hub():
    assert trading_guard.refusal({"title": "Backtest the ORB idea in the Strategy Analyzer"})
    assert trading_guard.refusal({"title": "Docs", "brief": "Restart NinjaTrader first"})
    assert trading_guard.refusal({"title": "Market data backfill for NQ"})
    assert trading_guard.refusal({"title": "Review the capacity panel layout"}) is None
    assert trading_guard.refusal({"title": "Summarize open PRs"}) is None


def test_trading_guard_uses_hub_router_but_ignores_half_matches(tmp_path, monkeypatch):
    package = tmp_path / "hub" / "src" / "trading_capability_hub"
    package.mkdir(parents=True)
    (tmp_path / "hub" / "registry").mkdir()
    (tmp_path / "hub" / "registry" / "playbooks.json").write_text("{}", encoding="utf-8")
    (package / "playbooks.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Match:\n"
        "    id: str\n"
        "    score: float\n"
        "class Set:\n"
        "    def route(self, text):\n"
        "        if 'weekly fit' in text:\n"
        "            return [Match('weekly-fit-report', 1.0)]\n"
        "        return [Match('nt-ensure-ready', 0.5)]\n"
        "def load_playbooks(path):\n"
        "    return Set()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORTEX_TRADING_HUB_ROOT", str(tmp_path / "hub"))
    assert "weekly-fit-report" in trading_guard.refusal({"title": "Run the weekly fit"})
    assert trading_guard.refusal({"title": "Open the settings page"}) is None


def test_unattended_refusal_checks_task_text(conn, project):
    task_id = store.create_task(conn, project_id=project["id"], type="review",
                                title="Refresh market data export for the report")
    task = store.get_task(conn, task_id)
    route = routing.effective_route(project, task)
    reason = dispatcher.unattended_refusal(conn, project, route, task)
    assert reason and "trading" in reason


# -- recovery -----------------------------------------------------------------

def test_reconcile_marks_orphaned_runs_unknown_and_blocks_task(conn, project):
    task_id = store.create_task(conn, project_id=project["id"], title="orphan")
    store.update_task(conn, task_id, status="running")
    job_id = jobs.create(conn, kind="dispatch", task_id=task_id,
                         project_id=project["id"], started_by="scheduler")
    run_id = store.create_run(conn, task_id=task_id, project_id=project["id"],
                              model="codex:default", execution_mode="headless_cli")
    jobs.attach_run(conn, job_id, run_id)
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (DEAD_PID, job_id))

    recovered = jobs.reconcile_details(conn)
    assert [item["job_id"] for item in recovered] == [job_id]
    run = store.get_run(conn, run_id)
    assert run["outcome"] == "unknown" and run["ended_at"]
    assert store.get_task(conn, task_id)["status"] == "blocked"


def test_reconcile_treats_silent_heartbeat_as_abandoned(conn, project):
    job_id = jobs.create(conn, kind="dispatch", project_id=project["id"])
    conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?", (job_id,))
    fresh = jobs.create(conn, kind="dispatch", project_id=project["id"])
    conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (ids.now(), fresh))
    conn.commit()
    assert [item["job_id"] for item in jobs.reconcile_details(conn)] == [job_id]


# -- scheduler tick -----------------------------------------------------------

def _review_task(conn, project, title="Review the README"):
    task_id = store.create_task(conn, project_id=project["id"], type="review", title=title)
    store.update_task(conn, task_id, status="assigned")
    return task_id


class FakeDispatch:
    """Stands in for dispatcher.dispatch: creates a run, then finishes it."""

    def __init__(self, exit_code: int = 0, raise_after_run: str | None = None) -> None:
        self.exit_code = exit_code
        self.raise_after_run = raise_after_run
        self.calls: list[dict] = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, conn, task, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["started_by"] == dispatcher.SCHEDULER
        assert callable(kwargs["watchdog"]) and callable(kwargs["observer"])
        run_id = store.create_run(conn, task_id=task["id"], project_id=task["project_id"],
                                  model="grok:default", execution_mode="headless_cli")
        store.update_run(conn, run_id, started_by="scheduler")
        kwargs["on_run_start"](run_id)
        self.release.wait(10)
        store.update_run(conn, run_id, ended_at=ids.now(), exit_code=self.exit_code)
        if self.raise_after_run:
            raise dispatcher.DispatchError(self.raise_after_run, run_id=run_id)
        return dispatcher.DispatchResult(
            run_id=run_id, task_status="done" if self.exit_code == 0 else "blocked",
            worker="grok", model="default", workspace=Path("."),
            exit_code=self.exit_code, files_changed=(), tests_passed=None, violations=(),
        )


def _pilot(isolated_db, conn, project, fake, candidates=None):
    queue = candidates if candidates is not None else [
        _review_task(conn, project, f"Review part {n}") for n in range(2)
    ]

    def pick(conn, *, limit, skipped):
        chosen, rest = queue[:limit], queue[limit:]
        queue[:] = rest
        skipped["held-task"] = "grok window is 80% used"
        return chosen

    pilot = autopilot.Autopilot(
        isolated_db, holder="test", dispatch_fn=fake, candidates_fn=pick,
        refresh_fn=lambda conn, **kwargs: 0,
    )
    assert pilot.acquire()
    return pilot


def _join(pilot):
    for thread in list(pilot._threads.values()):
        thread.join(15)


def test_tick_starts_candidates_and_respects_concurrency(isolated_db, conn, project):
    autopilot.set_max_concurrent(conn, 1)
    fake = FakeDispatch()
    fake.release.clear()
    pilot = _pilot(isolated_db, conn, project, fake)

    report = pilot.tick()
    assert len(report.started) == 1 and report.in_flight == 1
    assert report.skipped == {"held-task": "grok window is 80% used"}
    busy = pilot.tick()
    assert busy.started == [] and "busy" in busy.note

    fake.release.set()
    _join(pilot)
    job = jobs.recent(conn)[0]
    assert job["status"] == "done" and job["started_by"] == "scheduler" and job["run_id"]
    status = autopilot.status(conn)
    assert status["running"] and status["lease"]["detail"]["last_tick"]["note"]
    pilot.release()
    assert not autopilot.status(conn)["running"]


def test_tick_does_nothing_new_while_paused(isolated_db, conn, project):
    autonomy.set_paused(conn, True)
    fake = FakeDispatch()
    pilot = _pilot(isolated_db, conn, project, fake)
    report = pilot.tick()
    assert report.paused and report.started == [] and fake.calls == []
    pilot.release()


def test_failures_go_to_inbox_and_hold_the_worker(isolated_db, conn, project):
    autopilot.set_max_concurrent(conn, 1)
    fake = FakeDispatch(exit_code=1, raise_after_run="stopped by supervisor: no output")
    tasks = [_review_task(conn, project, f"Review {n}") for n in range(3)]
    pilot = _pilot(isolated_db, conn, project, fake, candidates=tasks)
    for _ in range(3):
        pilot.tick()
        _join(pilot)

    kinds = [item["kind"] for item in inbox.items(conn)]
    assert kinds.count("run_failed") == 3
    assert "worker_hold" in kinds
    assert supervisor.worker_hold(conn, "grok")
    pilot.release()


def test_lost_lease_stops_the_tick(isolated_db, conn, project):
    pilot = _pilot(isolated_db, conn, project, FakeDispatch())
    conn.execute("UPDATE leases SET fence = fence + 1")
    conn.commit()
    with pytest.raises(autopilot.LeaseLost):
        pilot.tick()


def test_tick_files_interrupted_runs(isolated_db, conn, project):
    task_id = _review_task(conn, project, "Interrupted review")
    job_id = jobs.create(conn, kind="dispatch", task_id=task_id,
                         project_id=project["id"], started_by="scheduler")
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (DEAD_PID, job_id))
    conn.commit()
    pilot = _pilot(isolated_db, conn, project, FakeDispatch(), candidates=[])
    report = pilot.tick()
    assert report.recovered == 1
    items = inbox.items(conn)
    assert items[0]["kind"] == "interrupted" and "Interrupted review" in items[0]["title"]
    pilot.release()


def test_pid_probe_sees_other_processes_without_signalling_them():
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert jobs._pid_is_running(child.pid)
        assert jobs._pid_is_running(child.pid)  # probing twice must not stop it
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
    assert not jobs._pid_is_running(child.pid)
    assert not jobs._pid_is_running(DEAD_PID)


def test_stopped_runs_are_listed_with_supervisor_evidence(
    isolated_db, conn, project, monkeypatch
):
    """A stopped run keeps what the supervisor counted, for tuning the limits."""
    limits = supervisor.Limits(max_tool_calls=3)
    monkeypatch.setattr(supervisor, "limits_for", lambda worker: limits)
    fake = FakeDispatch(exit_code=1, raise_after_run="stopped by supervisor: tool calls")
    task_id = _review_task(conn, project, "Wandering review")
    pilot = _pilot(isolated_db, conn, project, fake, candidates=[task_id])

    def observe_then_stop(conn_, task, **kwargs):
        watch = kwargs["watchdog"]
        for n in range(limits.max_tool_calls + 5):
            watch.observe(f"  → read_file(f{n}.py)\n")
        assert "more than" in watch.check()  # what workers.execute polls
        return fake(conn_, task, **kwargs)

    pilot._dispatch = observe_then_stop
    pilot.tick()
    _join(pilot)

    stops = autopilot.recent_stops(conn)
    assert len(stops) == 1
    stop = stops[0]
    assert stop["worker"] == "grok" and stop["title"] == "Wandering review"
    assert stop["supervisor"]["tool_calls"] == limits.max_tool_calls + 5
    assert stop["supervisor"]["limits"]["max_tool_calls"] == limits.max_tool_calls
    assert "more than 3 tool calls" in stop["supervisor"]["reason"]
    assert autopilot.status(conn)["recent_stops"][0]["id"] == stop["id"]
    pilot.release()
