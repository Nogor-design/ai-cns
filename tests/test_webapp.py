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
