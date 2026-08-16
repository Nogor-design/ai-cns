from __future__ import annotations

import json

import pytest

from cortex import ids, jobs, project_removal, store


def _seed_project_history(conn, repo):
    project_id = store.create_project(
        conn,
        name="Removable Project",
        repo_path=str(repo),
        record_activity=True,
    )
    store.update_project(conn, project_id, github_project_id="PVT_project")
    upstream = store.create_task(conn, project_id=project_id, title="First task")
    task_id = store.create_task(
        conn, project_id=project_id, title="Linked task",
        github_issue_id="I_issue", codex_thread_id="thread-1",
    )
    store.add_task_dependency(conn, task_id, upstream)
    run_id = store.create_run(
        conn,
        task_id=task_id,
        project_id=project_id,
        model="codex:default",
        execution_mode="headless_cli",
    )
    store.update_run(conn, run_id, ended_at=ids.now(), exit_code=0)
    store.create_decision(
        conn, project_id=project_id, decision="Remove after verification"
    )
    store.create_suggestion(conn, project_id=project_id, title="Suggested work")
    store.upsert_git_check(conn, project_id, is_git=True, branch="main")
    job_id = jobs.create(conn, kind="plan", task_id=task_id, project_id=project_id)
    jobs.finish(conn, job_id, status="done")
    now = ids.now()
    conn.execute(
        """INSERT INTO github_mirror_operations
           (operation_id, task_id, project_id, plan_fingerprint,
            github_project_id, status, actor, actions_json,
            completed_actions_json, created_at, updated_at)
           VALUES (?,?,?,?,?,'verified','owner','[]','[]',?,?)""",
        ("mirror-op", task_id, project_id, "fingerprint", "PVT_project", now, now),
    )
    conn.commit()
    return project_id


def test_preview_reports_deleted_and_preserved_scope(conn, tmp_path):
    repo = tmp_path / "removable"
    repo.mkdir()
    state_path = repo / ".cortex" / "state.md"
    state_path.parent.mkdir()
    state_path.write_text("# Keep me\n", encoding="utf-8")
    project_id = _seed_project_history(conn, repo)

    result = project_removal.preview(conn, project_id)

    assert result["project_name"] == "Removable Project"
    assert result["deleted_counts"]["tasks"] == 2
    assert result["deleted_counts"]["task_dependencies"] == 1
    assert result["deleted_counts"]["runs"] == 1
    assert result["deleted_counts"]["github_mirror_operations"] == 1
    assert result["blocked"] is False
    assert result["preserved"] == {
        "repository": str(repo),
        "state_path": str(state_path),
        "state_exists": True,
        "github_project_configured": True,
        "github_linked_tasks": 1,
        "codex_linked_tasks": 1,
    }


def test_remove_is_transactional_preserves_repository_and_keeps_audit(conn, tmp_path):
    repo = tmp_path / "removable"
    repo.mkdir()
    marker = repo / "owner-file.txt"
    marker.write_text("untouched\n", encoding="utf-8")
    project_id = _seed_project_history(conn, repo)

    result = project_removal.remove(
        conn,
        project_id,
        confirm_name="Removable Project",
        acknowledge_permanent=True,
    )

    assert result["project_id"] == project_id
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    assert conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone() is None
    for table in (
        "tasks", "runs", "decisions", "activity_events", "suggestions",
        "github_mirror_operations", "jobs", "project_git_checks",
    ):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
        ).fetchone()[0] == 0
    tombstone = conn.execute(
        "SELECT * FROM project_removals WHERE removal_id = ?",
        (result["removal_id"],),
    ).fetchone()
    assert tombstone["actor_name"] == "owner"
    assert json.loads(tombstone["deleted_counts_json"])["tasks"] == 2
    activity = project_removal.list_activity(conn)[0]
    assert activity["action"] == "project.removed"
    assert activity["project_name"] == "Removable Project"


@pytest.mark.parametrize(
    ("confirm_name", "acknowledge", "message"),
    [
        ("removable project", True, "exact project name"),
        ("Removable Project", False, "permanently removed"),
        ("Removable Project", "true", "permanently removed"),
    ],
)
def test_remove_requires_exact_explicit_confirmation(
    conn, tmp_path, confirm_name, acknowledge, message
):
    repo = tmp_path / "removable"
    repo.mkdir()
    project_id = store.create_project(
        conn, name="Removable Project", repo_path=str(repo)
    )

    with pytest.raises(ValueError, match=message):
        project_removal.remove(
            conn,
            project_id,
            confirm_name=confirm_name,
            acknowledge_permanent=acknowledge,
        )
    assert store.get_project(conn, project_id)["name"] == "Removable Project"


def test_remove_refuses_active_jobs_and_runs(conn, tmp_path):
    repo = tmp_path / "busy"
    repo.mkdir()
    project_id = store.create_project(conn, name="Busy Project", repo_path=str(repo))
    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Working",
        pm_session_id="session-1",
    )
    store.update_task(conn, task_id, status="running")
    jobs.create(conn, kind="dispatch", task_id=task_id, project_id=project_id)
    store.create_run(
        conn,
        task_id=task_id,
        project_id=project_id,
        model="codex:default",
        execution_mode="headless_cli",
    )

    result = project_removal.preview(conn, project_id)
    assert result["blocked"] is True
    assert len(result["blockers"]) == 3
    with pytest.raises(ValueError, match="still running"):
        project_removal.remove(
            conn,
            project_id,
            confirm_name="Busy Project",
            acknowledge_permanent=True,
        )
    assert store.get_project(conn, project_id)["name"] == "Busy Project"
