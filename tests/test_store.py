"""Schema, CRUD, and the model_task_history VIEW."""

from __future__ import annotations

import json

import pytest

from cortex import ids, store


def test_slugify():
    assert ids.slugify("Demo Project") == "demo-project"
    assert ids.slugify("  Weird!! Name ") == "weird-name"
    assert ids.slugify("") == "project"


def test_project_crud_and_lookup_by_name(conn, git_repo):
    pid = store.create_project(conn, name="My Repo", repo_path=str(git_repo))
    assert pid == "my-repo"
    by_id = store.get_project(conn, "my-repo")
    by_name = store.get_project(conn, "My Repo")
    assert by_id["id"] == by_name["id"] == "my-repo"
    with pytest.raises(store.NotFound):
        store.get_project(conn, "nope")


def test_task_type_coerced(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="bogus")
    assert store.get_task(conn, tid)["type"] == "other"
    tid2 = store.create_task(conn, project_id=project["id"], title="t2", type="code")
    assert store.get_task(conn, tid2)["type"] == "code"


def test_task_assignment_controls_round_trip(conn, project):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="bounded review",
        priority=1,
        assignee="codex",
        due_at="2026-08-05",
    )
    store.update_task(conn, tid, status="assigned", priority=2)
    row = store.get_task(conn, tid)
    assert row["assignee"] == "codex"
    assert row["status"] == "assigned"
    assert row["priority"] == 2
    assert row["due_at"] == "2026-08-05"


def test_list_all_tasks_includes_project_metadata(conn, project):
    store.create_task(conn, project_id=project["id"], title="portfolio task")
    row = store.list_all_tasks(conn)[0]
    assert row["project_name"] == project["name"]
    assert row["project_program"] == project["program"]


def test_runs_json_files_changed(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    rid = store.create_run(
        conn, task_id=tid, project_id=project["id"], model="m", execution_mode="api"
    )
    store.update_run(conn, rid, files_changed=["a.py", "b.py"], diff_size=10)
    row = store.get_run(conn, rid)
    assert row["files_changed"] == '["a.py", "b.py"]'
    assert row["diff_size"] == 10


def test_model_task_history_view(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    for passed, outcome in ((1, "survived"), (1, "reverted"), (0, "accepted")):
        rid = store.create_run(
            conn, task_id=tid, project_id=project["id"],
            model="codex", execution_mode="agentic_cli",
        )
        store.update_run(conn, rid, tests_passed=passed, outcome=outcome, diff_size=20)
    rows = store.model_task_history(conn, project["id"], task_type="code")
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "codex"
    assert r["attempts"] == 3
    assert r["tests_passed"] == 2
    assert r["survived"] == 2
    assert r["avg_diff_size"] == 20


def test_decisions(conn, project):
    store.create_decision(
        conn, project_id=project["id"], decision="use sqlite", rationale="boring", source="manual"
    )
    rows = store.recent_decisions(conn, project["id"])
    assert rows[0]["decision"] == "use sqlite"


def test_pm_sessions_are_distinct_attributed_and_filterable(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Manage the slice", actor_name="owner",
        blocked_reason="Waiting on approval",
    )
    _, first_session = store.start_pm_session(
        conn, project_id=project["id"], task_id=task_id, title="First pass",
        owner="codex", codex_thread_id="thread-1",
    )
    assert store.get_task(conn, task_id)["blocked_reason"] is None
    store.create_activity_event(
        conn,
        project_id=project["id"],
        task_id=task_id,
        actor_type="agent",
        actor_name="gemini",
        action="delegation.completed",
        summary="Reviewed the records schema",
        session_id=first_session,
        evidence="plain text is still encoded as JSON",
    )
    store.close_pm_session(
        conn, task_id, status="in_progress", summary="First pass complete",
        session_id=first_session,
    )
    _, second_session = store.start_pm_session(
        conn, project_id=project["id"], task_id=task_id, title="Second pass",
        owner="codex", codex_thread_id="thread-1",
    )

    assert first_session != second_session
    first_events = store.list_activity_events(conn, session_id=first_session)
    assert {row["action"] for row in first_events} >= {
        "pm.session_started", "pm.session_closed", "delegation.completed",
    }
    delegated = next(row for row in first_events if row["action"] == "delegation.completed")
    assert delegated["actor_name"] == "gemini"
    assert json.loads(delegated["evidence_json"]) == "plain text is still encoded as JSON"


def test_five_pm_sessions_across_three_projects_reconstruct_without_chat(conn, project):
    project_ids = [project["id"]]
    for number in (2, 3):
        project_ids.append(store.create_project(
            conn,
            name=f"Portfolio Project {number}",
            repo_path=f"C:/portfolio/project-{number}",
        ))
    sessions = []
    for number in range(5):
        project_id = project_ids[number % 3]
        task_id, session_id = store.start_pm_session(
            conn,
            project_id=project_id,
            title=f"PM slice {number + 1}",
            owner="codex" if number % 2 == 0 else "gemini",
            next_action=f"Advance slice {number + 1}",
            codex_thread_id=f"thread-{number + 1}",
        )
        store.close_pm_session(
            conn,
            task_id,
            status="done",
            summary=f"Accepted slice {number + 1}",
            session_id=session_id,
        )
        sessions.append((project_id, task_id, session_id))

    assert len({session_id for _, _, session_id in sessions}) == 5
    assert len({project_id for project_id, _, _ in sessions}) == 3
    for project_id, task_id, session_id in sessions:
        events = store.list_activity_events(
            conn, project_id, task_id=task_id, session_id=session_id
        )
        assert {event["action"] for event in events} == {
            "pm.session_closed", "pm.session_started",
        }
        assert all(event["actor_name"] for event in events)


def test_pm_start_rolls_back_task_when_session_event_fails(conn, project, monkeypatch):
    task_id = store.create_task(conn, project_id=project["id"], title="Atomic PM")
    original = store._insert_activity

    def fail_session_event(connection, **fields):
        if fields.get("action") == "pm.session_started":
            raise RuntimeError("simulated event failure")
        return original(connection, **fields)

    monkeypatch.setattr(store, "_insert_activity", fail_session_event)
    with pytest.raises(RuntimeError, match="simulated"):
        store.start_pm_session(
            conn, project_id=project["id"], task_id=task_id, title="Must rollback"
        )
    task = store.get_task(conn, task_id)
    assert task["status"] == "open"
    assert task["pm_session_id"] is None


def test_task_hierarchy_and_dependencies_reject_indirect_cycles(conn, project):
    first = store.create_task(conn, project_id=project["id"], title="First")
    second = store.create_task(
        conn, project_id=project["id"], title="Second", parent_id=first
    )
    third = store.create_task(
        conn, project_id=project["id"], title="Third", parent_id=second
    )
    with pytest.raises(ValueError, match="cycle"):
        store.update_task(conn, first, parent_id=third)

    store.add_task_dependency(conn, second, first)
    store.add_task_dependency(conn, third, second)
    with pytest.raises(ValueError, match="cycle"):
        store.add_task_dependency(conn, first, third)
    store.remove_task_dependency(conn, third, second)
    assert store.list_task_dependencies(conn, third) == []
    assert "task.dependency_removed" in {
        row["action"] for row in store.list_activity_events(conn, task_id=third)
    }


def test_done_forces_full_progress_and_completion_time(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Finish me", progress=30
    )
    store.update_task(conn, task_id, status="done", progress=30)
    task = store.get_task(conn, task_id)
    assert task["progress"] == 100
    assert task["completed_at"]


def test_github_node_ids_can_only_link_to_one_cortex_task(conn, project):
    first = store.create_task(
        conn,
        project_id=project["id"],
        title="First linked task",
        github_issue_id="I_same_issue",
        github_project_item_id="PVTI_same_item",
    )
    with pytest.raises(ValueError, match="github_issue_id.*already linked"):
        store.create_task(
            conn,
            project_id=project["id"],
            title="Duplicate issue task",
            github_issue_id="I_same_issue",
        )

    second = store.create_task(
        conn, project_id=project["id"], title="Second unlinked task"
    )
    with pytest.raises(ValueError, match="github_project_item_id.*already linked"):
        store.update_task(conn, second, github_project_item_id="PVTI_same_item")

    store.update_task(conn, first, github_issue_id="I_same_issue")
    assert store.get_task(conn, first)["github_issue_id"] == "I_same_issue"
