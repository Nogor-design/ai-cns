"""Phase 3 admission: which write work the scheduler may start on its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex import autopilot, dispatcher, ids, integration, store, team, verification


@pytest.fixture
def write_task(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Implement the parser",
        type="code", risk="low", complexity=5,
        acceptance="parser.py parses the sample", allowed_paths='["parser.py"]',
    )
    store.update_task(conn, task_id, status="assigned", assignee="codex")
    return store.get_task(conn, task_id)


def _ready(monkeypatch, worker="codex"):
    monkeypatch.setattr(
        "cortex.workers.probe",
        lambda name, **kwargs: {"availability": "ready" if name == worker else "missing"},
    )
    monkeypatch.setattr(
        "cortex.workers.probe_cached",
        lambda name, **kwargs: {"availability": "ready" if name == worker else "missing"},
    )
    monkeypatch.setattr("cortex.capacity.admit", lambda conn, worker: type(
        "A", (), {"allowed": True, "reason": "ok", "windows": ()}
    )())


def test_write_work_waits_unless_the_project_is_in_integration_mode(
    conn, project, write_task, monkeypatch
):
    _ready(monkeypatch)
    skipped: dict[str, str] = {}

    # Default mode is read_only: the write task is not offered at all.
    assert team.safe_start_candidates(conn, skipped=skipped) == []

    store.update_project(conn, project["id"], autonomy_mode="integration")
    assert team.safe_start_candidates(conn) == [write_task["id"]]


def test_high_risk_write_work_still_waits_for_the_owner(conn, project, monkeypatch):
    _ready(monkeypatch)
    store.update_project(conn, project["id"], autonomy_mode="integration")
    task_id = store.create_task(
        conn, project_id=project["id"],
        title="Rotate the production database credentials", type="code", risk="high",
    )
    store.update_task(conn, task_id, status="assigned", assignee="codex")

    assert team.safe_start_candidates(conn) == []


def test_protected_projects_never_get_an_unattended_write(conn, git_repo, monkeypatch):
    _ready(monkeypatch)
    project_id = store.create_project(
        conn, name="NinjaTrader strategy lab", repo_path=str(git_repo),
    )
    # The mode cannot even be set: protection is decided in code, not settings.
    with pytest.raises(ValueError):
        store.update_project(
            conn, project_id,
            autonomy_mode=__import__("cortex.autonomy", fromlist=["x"]).validate_mode_change(
                store.get_project(conn, project_id), "integration"
            ),
        )
    task_id = store.create_task(
        conn, project_id=project_id, title="Implement the exit rule", type="code", risk="low",
    )
    store.update_task(conn, task_id, status="assigned", assignee="codex")
    assert team.safe_start_candidates(conn) == []


def test_merge_allowed_follows_the_owners_mode(conn, project):
    assert verification.merge_allowed(project) is False
    store.update_project(conn, project["id"], autonomy_mode="integration")
    assert verification.merge_allowed(store.get_project(conn, project["id"])) is True


def test_scheduler_passes_allow_write_only_for_write_routes(
    isolated_db, conn, project, write_task, monkeypatch
):
    _ready(monkeypatch)
    store.update_project(conn, project["id"], autonomy_mode="integration")
    calls: list[dict] = []

    def fake_dispatch(conn, task, **kwargs):
        calls.append(kwargs)
        run_id = store.create_run(
            conn, task_id=task["id"], project_id=task["project_id"],
            model="codex:default", execution_mode="headless_cli",
        )
        kwargs["on_run_start"](run_id)
        store.update_run(conn, run_id, ended_at=ids.now(), exit_code=0)
        return dispatcher.DispatchResult(
            run_id=run_id, task_status="review", worker="codex", model="default",
            workspace=Path("."), exit_code=0, files_changed=("parser.py",),
            tests_passed=1, violations=(),
            verification={"status": "merged", "summary": "Merged.", "merge_commit": "abc123"},
        )

    pilot = autopilot.Autopilot(
        isolated_db, holder="test", dispatch_fn=fake_dispatch,
        candidates_fn=lambda conn, *, limit, skipped: [write_task["id"]],
        refresh_fn=lambda conn, **kwargs: 0,
    )
    assert pilot.acquire()
    report = pilot.tick()
    for thread in list(pilot._threads.values()):
        thread.join(15)
    pilot.release()

    assert report.started[0]["action"] == "implement"
    assert calls[0]["allow_write"] is True
    # The gate's verdict is carried into the job record the owner reads.
    from cortex import jobs
    assert jobs.recent(conn)[0]["result"]["verification"]["merge_commit"] == "abc123"


def test_integration_branch_is_the_base_for_write_worktrees(conn, project, git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    from cortex import gitutil, worktrees

    workspace = integration.ensure(git_repo, project["id"])
    (workspace.path / "already-verified.py").write_text("ok = True\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "-C", str(workspace.path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace.path), "-c", "user.email=c@x", "-c", "user.name=C",
         "commit", "-q", "-m", "earlier verified work"], check=True, capture_output=True,
    )

    tree = worktrees.ensure(git_repo, project["id"], "task1", base=integration.BRANCH)

    # A new write run starts from what the gate already accepted, not from main.
    assert (tree.path / "already-verified.py").exists()
    assert gitutil.branch(tree.path) == f"cortex/{project['id']}/task1"
