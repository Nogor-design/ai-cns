"""Phase 4 cascade: cheap first, stronger on a rejection, and knowing when to stop."""

from __future__ import annotations

import pytest

from cortex import cascade, ids, store, verification


def _task(conn, project, **fields):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Add the parser", type="code", **fields
    )
    return store.get_task(conn, task_id)


def _attempt(conn, project, task, worker, *, gate_status="rejected"):
    run_id = store.create_run(
        conn, task_id=task["id"], project_id=project["id"],
        model=f"{worker}:default", execution_mode="headless_cli",
    )
    store.update_run(conn, run_id, ended_at=ids.now(), exit_code=0)
    if gate_status:
        report = verification.GateReport(
            task_id=task["id"], project_id=project["id"], run_id=run_id,
            status=gate_status,
        )
        report.checks = [verification.Check("review", "fail", "not good enough")]
        verification.record(conn, report)
    return run_id


def test_a_rejected_task_goes_to_a_stronger_worker(conn, project):
    task = _task(conn, project)
    run_id = _attempt(conn, project, task, "ollama")

    rung = cascade.escalate(
        conn, project, task, after="ollama", gate_status="rejected", run_id=run_id
    )

    assert rung is not None and rung.worker == "opencode-local"
    assert rung.attempt == 2
    # The task is queued again, not left blocked for the owner.
    assert store.get_task(conn, task["id"])["status"] == "assigned"
    assert store.get_task(conn, task["id"])["assignee"] == "opencode-local"


def test_an_owner_decision_is_never_escalated(conn, project):
    task = _task(conn, project)
    _attempt(conn, project, task, "opencode", gate_status="needs_owner")

    assert cascade.should_escalate("needs_owner") is False
    assert cascade.escalate(
        conn, project, task, after="opencode", gate_status="needs_owner"
    ) is None


def test_the_ladder_stops_after_two_escalations(conn, project):
    task = _task(conn, project)
    for worker in ("ollama", "opencode", "claude"):
        _attempt(conn, project, task, worker)

    # Three workers have run: the first attempt plus two escalations.
    assert cascade.escalate(
        conn, project, task, after="claude", gate_status="rejected"
    ) is None


def test_a_worker_that_already_tried_is_not_asked_again(conn, project):
    task = _task(conn, project)
    _attempt(conn, project, task, "gemini")
    _attempt(conn, project, task, "opencode")

    rung = cascade.next_rung(conn, project, task, after="opencode")

    assert rung is not None and rung.worker not in {"gemini", "opencode"}
    assert cascade.STRENGTH.index(rung.worker) > cascade.STRENGTH.index("opencode")


def test_the_ladder_respects_the_project_allowlist(conn, project):
    store.update_project(conn, project["id"], allowed_workers='["ollama", "opencode"]')
    allowed = store.get_project(conn, project["id"])
    task = _task(conn, project)
    _attempt(conn, project, task, "opencode")

    # Nothing stronger is permitted to read this repository, so the ladder ends
    # rather than proposing a worker dispatch would refuse.
    assert cascade.next_rung(conn, allowed, task, after="opencode") is None


def test_the_top_of_the_ladder_has_nowhere_to_go(conn, project):
    task = _task(conn, project)
    _attempt(conn, project, task, "codex")

    assert cascade.next_rung(conn, project, task, after="codex") is None


def test_attempts_are_read_back_from_the_runs_not_a_counter(conn, project):
    task = _task(conn, project)
    _attempt(conn, project, task, "ollama")
    _attempt(conn, project, task, "ollama")  # a retry with the same worker
    _attempt(conn, project, task, "opencode")

    assert cascade.attempted_workers(conn, task["id"]) == ["ollama", "opencode"]
    # Two distinct workers means one escalation so far, so one is still allowed.
    assert cascade.next_rung(conn, project, task, after="opencode") is not None


def test_history_shows_each_attempt_and_what_the_gate_said(conn, project):
    task = _task(conn, project)
    _attempt(conn, project, task, "ollama", gate_status="rejected")
    _attempt(conn, project, task, "opencode", gate_status="merged")

    rows = cascade.history(conn, task["id"])

    assert [row["worker"] for row in rows] == ["ollama", "opencode"]
    assert [row["gate_status"] for row in rows] == ["rejected", "merged"]


def test_the_escalation_is_recorded_for_the_owner_to_read(conn, project):
    task = _task(conn, project)
    run_id = _attempt(conn, project, task, "ollama")
    cascade.escalate(
        conn, project, task, after="ollama", gate_status="rejected",
        reason="tests failed on the merged result", run_id=run_id,
    )

    recent = cascade.payload(conn)["recent"]
    assert recent and "ollama was rejected" in recent[0]["summary"]
    assert recent[0]["worker"] == "opencode-local"


# -- the scheduler's use of the ladder -----------------------------------------

def test_the_scheduler_escalates_a_rejected_task_and_leaves_owner_work_alone(
    isolated_db, conn, project, monkeypatch
):
    from pathlib import Path
    from cortex import autopilot, dispatcher

    monkeypatch.setattr(
        "cortex.workers.probe",
        lambda name, **kwargs: {"availability": "ready"},
    )
    task = _task(conn, project, complexity=5, risk="low")
    store.update_task(conn, task["id"], status="assigned", assignee="opencode")
    gate = {"status": "rejected", "summary": "review: not good enough",
            "merge_commit": None}

    def fake_dispatch(conn, task, **kwargs):
        run_id = store.create_run(
            conn, task_id=task["id"], project_id=task["project_id"],
            model="opencode:deepseek", execution_mode="headless_cli",
        )
        kwargs["on_run_start"](run_id)
        store.update_run(conn, run_id, ended_at=ids.now(), exit_code=0)
        return dispatcher.DispatchResult(
            run_id=run_id, task_status="blocked", worker="opencode", model="deepseek",
            workspace=Path("."), exit_code=0, files_changed=("a.py",), tests_passed=1,
            violations=(), verification=gate,
        )

    pilot = autopilot.Autopilot(
        isolated_db, holder="test", dispatch_fn=fake_dispatch,
        candidates_fn=lambda conn, *, limit, skipped: [task["id"]],
        refresh_fn=lambda conn, **kwargs: 0,
    )
    assert pilot.acquire()
    pilot.tick()
    for thread in list(pilot._threads.values()):
        thread.join(15)

    escalated = store.get_task(conn, task["id"])
    assert escalated["status"] == "assigned"
    assert escalated["assignee"] == "gemini"

    # The same run, judged as an owner decision instead, is left alone.
    store.update_task(conn, task["id"], status="assigned", assignee="opencode")
    gate["status"] = "needs_owner"
    pilot.tick()
    for thread in list(pilot._threads.values()):
        thread.join(15)
    pilot.release()
    assert store.get_task(conn, task["id"])["assignee"] == "opencode"
