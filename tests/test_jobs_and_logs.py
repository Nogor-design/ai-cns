"""Durable jobs and live run output."""

from __future__ import annotations

import os

from cortex import ids, jobs, runlog, store


def test_run_log_is_readable_while_it_grows():
    run_id = ids.short_id()
    runlog.start(run_id, header="# header\n")
    text, offset = runlog.read_from(run_id)
    assert text == "# header\n"

    runlog.append(run_id, "first chunk\n")
    text, offset = runlog.read_from(run_id, offset)
    assert text == "first chunk\n"

    # A second read from the same offset returns nothing new.
    text, offset = runlog.read_from(run_id, offset)
    assert text == ""

    runlog.append(run_id, "second chunk\n")
    text, _ = runlog.read_from(run_id, offset)
    assert text == "second chunk\n"


def test_run_log_survives_a_split_multibyte_character():
    """Offsets are bytes, so a read can land mid-character; it must not raise."""
    run_id = ids.short_id()
    runlog.start(run_id, header="")
    runlog.append(run_id, "café ☕\n")
    # 'é' is two bytes; slicing between them must still decode.
    text, _ = runlog.read_from(run_id, 4)
    assert isinstance(text, str)


def test_missing_run_log_reads_empty_rather_than_raising():
    assert runlog.read_from("no-such-run", 0) == ("", 0)
    assert runlog.size("no-such-run") == 0


def test_job_lifecycle_is_persisted(conn):
    job_id = jobs.create(conn, kind="dispatch", label="Working", task_id="t1")
    row = jobs.get(conn, job_id)
    assert row["status"] == "running"
    assert row["pid"] == os.getpid()

    jobs.attach_run(conn, job_id, "run-123")
    jobs.finish(conn, job_id, status="done", result={"exit_code": 0})
    row = jobs.get(conn, job_id)
    assert row["status"] == "done"
    assert row["run_id"] == "run-123"
    assert row["result"] == {"exit_code": 0}
    assert row["completed_at"]


def test_reconcile_closes_jobs_left_by_a_dead_process(conn, project, monkeypatch):
    """A restart must not leave tasks stuck in 'running' forever."""
    task_id = store.create_task(
        conn, project_id=project["id"], title="Long review", type="review"
    )
    store.update_task(conn, task_id, status="running")
    run_id = store.create_run(
        conn, task_id=task_id, project_id=project["id"],
        model="claude:sonnet", execution_mode="headless_cli",
    )
    job_id = jobs.create(conn, kind="dispatch", task_id=task_id)
    jobs.attach_run(conn, job_id, run_id)
    # Simulate the owning dashboard process having exited.
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (os.getpid() + 99_999, job_id))
    conn.commit()
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: pid == os.getpid())

    assert jobs.reconcile(conn) == 1

    assert jobs.get(conn, job_id)["status"] == "interrupted"
    assert store.get_task(conn, task_id)["status"] == "blocked"
    run = store.get_run(conn, run_id)
    assert run["ended_at"] is not None
    assert run["exit_code"] == 1


def test_reconcile_leaves_this_process_jobs_alone(conn):
    job_id = jobs.create(conn, kind="plan")
    assert jobs.reconcile(conn) == 0
    assert jobs.get(conn, job_id)["status"] == "running"


def test_reconcile_leaves_jobs_owned_by_another_live_process_alone(
    conn, monkeypatch
):
    job_id = jobs.create(conn, kind="plan")
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (42_424, job_id))
    conn.commit()
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: pid == 42_424)

    assert jobs.reconcile(conn) == 0
    assert jobs.get(conn, job_id)["status"] == "running"


def test_reconcile_does_not_close_unrelated_manual_runs(conn, project, monkeypatch):
    stale_task_id = store.create_task(
        conn, project_id=project["id"], title="Dashboard review", type="review"
    )
    manual_task_id = store.create_task(
        conn, project_id=project["id"], title="Manual research", type="research"
    )
    store.update_task(conn, stale_task_id, status="running")
    store.update_task(conn, manual_task_id, status="running")
    stale_run_id = store.create_run(
        conn, task_id=stale_task_id, project_id=project["id"],
        model="claude:sonnet", execution_mode="headless_cli",
    )
    manual_run_id = store.create_run(
        conn, task_id=manual_task_id, project_id=project["id"],
        model="web-model", execution_mode="manual",
    )
    job_id = jobs.create(conn, kind="dispatch", task_id=stale_task_id)
    jobs.attach_run(conn, job_id, stale_run_id)
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (91_919, job_id))
    conn.commit()
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: False)

    assert jobs.reconcile(conn) == 1

    assert store.get_run(conn, stale_run_id)["ended_at"] is not None
    assert store.get_task(conn, stale_task_id)["status"] == "blocked"
    assert store.get_run(conn, manual_run_id)["ended_at"] is None
    assert store.get_task(conn, manual_task_id)["status"] == "running"


def test_reconcile_recovers_dispatch_crash_before_run_attachment(
    conn, project, monkeypatch
):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Interrupted review", type="review"
    )
    store.update_task(conn, task_id, status="running")
    job_id = jobs.create(conn, kind="dispatch", task_id=task_id)
    run_id = store.create_run(
        conn, task_id=task_id, project_id=project["id"],
        model="codex:default", execution_mode="headless_cli",
    )
    conn.execute("UPDATE jobs SET pid = ? WHERE id = ?", (73_737, job_id))
    conn.commit()
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: False)

    assert jobs.reconcile(conn) == 1
    assert store.get_run(conn, run_id)["ended_at"] is not None
    assert store.get_task(conn, task_id)["status"] == "blocked"
