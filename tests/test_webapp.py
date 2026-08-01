"""Dashboard payload tests: the UI must expose facts, routes, and assignments."""

from __future__ import annotations

from cortex import store, webapp


def test_portfolio_payload_separates_recommendation_from_assignment(conn, project):
    recommended_id = store.create_task(
        conn,
        project_id=project["id"],
        title="Summarize the README",
        type="docs",
        priority=2,
    )
    assigned_id = store.create_task(
        conn,
        project_id=project["id"],
        title="Review architecture decisions",
        type="review",
        complexity=4,
        assignee="claude",
        priority=1,
    )
    store.update_task(conn, assigned_id, status="assigned")

    payload = webapp.portfolio_payload(conn)
    tasks = {task["id"]: task for task in payload["tasks"]}
    assert tasks[recommended_id]["assignee"] is None
    assert tasks[recommended_id]["recommended_worker"] == "ollama"
    assert tasks[assigned_id]["assignee"] == "claude"
    assert payload["summary"]["unassigned"] == 1
    assert payload["workers"]


def test_portfolio_payload_keeps_paused_projects_recoverable(conn, project):
    store.update_project(conn, project["id"], status="paused")
    payload = webapp.portfolio_payload(conn)
    row = next(item for item in payload["projects"] if item["project_id"] == project["id"])
    assert row["status"] == "paused"
    assert payload["summary"]["paused_projects"] == 1


def test_portfolio_payload_exposes_recommendations_separately(conn, project):
    suggestion_id = store.create_suggestion(
        conn,
        project_id=project["id"],
        title="Plan the next slice",
        type="planning",
        why="Keep scope bounded.",
        acceptance="One accepted slice.",
        recommended_worker="ollama",
        recommended_model="phi4:14b",
        action="review",
    )

    payload = webapp.portfolio_payload(conn)
    assert payload["summary"]["recommendations"] == 1
    assert payload["suggestions"][0]["id"] == suggestion_id
    assert payload["projects"][0]["suggestion_count"] == 1
    assert payload["tasks"] == []


def test_portfolio_payload_includes_latest_run_evidence(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Inspect the release", type="review"
    )
    run_id = store.create_run(
        conn,
        task_id=task_id,
        project_id=project["id"],
        model="codex:default",
        execution_mode="headless_cli",
    )
    store.update_run(
        conn,
        run_id,
        ended_at="2026-08-01T20:00:00Z",
        exit_code=0,
        response="Release evidence captured.",
    )
    store.update_task(conn, task_id, status="review")

    payload = webapp.portfolio_payload(conn)
    assert payload["runs"][0]["id"] == run_id
    assert payload["runs"][0]["state"] == "completed"
    assert payload["runs"][0]["model"] == "codex:default"
    assert payload["tasks"][0]["latest_run"]["response"] == "Release evidence captured."


def test_portfolio_payload_exposes_expert_team_and_execution_controls(conn, project):
    task_id = store.create_task(
        conn,
        project_id=project["id"],
        title="Adversarially review the plan",
        type="review",
        assignee="grok",
        requested_model="grok-4.5",
        effort="medium",
    )
    store.update_task(conn, task_id, status="assigned")

    payload = webapp.portfolio_payload(conn)
    task = next(row for row in payload["tasks"] if row["id"] == task_id)
    grok = next(row for row in payload["team"] if row["name"] == "grok")
    assert task["execution_worker"] == "grok"
    assert task["execution_model"] == "grok-4.5"
    assert task["execution_effort"] == "medium"
    assert grok["state"] in {"queued", "missing", "needs_auth"}
    assert "usage" in payload
    assert "model_performance" in payload
