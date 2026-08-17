"""AI-PM suggestions stay separate from tasks until explicit approval."""

from __future__ import annotations

import json

from cortex import pm, store, workers


def test_plan_falls_back_without_worker_and_caches(conn, project, monkeypatch):
    monkeypatch.setattr(workers, "available", lambda name: False)

    first = pm.plan_project(conn, project, worker="codex")
    second = pm.plan_project(conn, project, worker="codex")

    assert first.source_worker == "deterministic"
    assert len(first.suggestion_ids) == 3
    assert second.cached is True
    assert second.suggestion_ids == first.suggestion_ids
    assert store.list_tasks(conn, project["id"]) == []


def test_restricted_project_requires_explicit_cloud_planning(conn, project, monkeypatch):
    store.update_project(conn, project["id"], privacy="restricted")
    project = store.get_project(conn, project["id"])
    monkeypatch.setattr(workers, "available", lambda name: True)
    called = False

    def fake_ask(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(pm, "_ask_worker", fake_ask)
    result = pm.plan_project(conn, project, worker="codex")
    assert result.source_worker == "deterministic"
    assert called is False


def test_convert_suggestion_creates_one_assigned_task(conn, project):
    suggestion_id = store.create_suggestion(
        conn,
        project_id=project["id"],
        title="Review the release path",
        type="review",
        why="Reduce release risk.",
        acceptance="A ranked release checklist exists.",
        recommended_worker="claude",
        recommended_model="sonnet",
        action="review",
    )

    task_id = store.convert_suggestion(conn, suggestion_id)
    assert store.convert_suggestion(conn, suggestion_id) == task_id
    task = store.get_task(conn, task_id)
    suggestion = store.get_suggestion(conn, suggestion_id)
    assert task["status"] == "assigned"
    assert task["assignee"] == "claude"
    assert suggestion["status"] == "converted"
    assert suggestion["task_id"] == task_id
    assert len(store.list_tasks(conn, project["id"])) == 1


def test_convert_suggestion_copies_phase_criterion_links(conn, project):
    suggestion_id = store.create_suggestion(
        conn,
        project_id=project["id"],
        title="Meet the phase gate",
        phase_id="phase-one",
        exit_criterion_ref="exit-2",
    )

    task_id = store.convert_suggestion(conn, suggestion_id)

    task = store.get_task(conn, task_id)
    assert task["phase_id"] == "phase-one"
    assert task["exit_criterion_ref"] == "exit-2"


def test_codex_jsonl_response_is_parsed():
    proposals = [{
        "title": "Check launch readiness", "type": "review", "why": "Find gaps",
        "brief": "Review the current state.", "acceptance": "A gap list exists",
        "risk": "low", "complexity": 2, "budget": "small", "priority": 1,
        "allowed_paths": ["README.md"],
    }]
    output = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(proposals)
        }}),
    ])
    assert pm._parse_proposals(output) == proposals


def test_codex_planner_is_ephemeral_and_does_not_load_plugins(conn, project, monkeypatch):
    monkeypatch.setattr(workers, "available", lambda name: True)
    captured = {}

    def fake_execute(spec, **kwargs):
        captured["argv"] = spec.argv
        proposal = [{
            "title": "Review readiness", "type": "review", "why": "Find gaps",
            "brief": "Review the repo.", "acceptance": "A gap list exists",
            "risk": "low", "complexity": 2, "priority": 1,
        }]
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(proposal)}}
        return workers.WorkerResult(0, json.dumps(event), "", None)

    monkeypatch.setattr(workers, "execute", fake_execute)
    result = pm.plan_project(conn, project, worker="codex", count=1)
    assert result.source_worker == "codex"
    assert "--ignore-user-config" in captured["argv"]
    assert "--ephemeral" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--disable") + 1] == "plugins"


def test_focus_prefers_human_decision_then_work(conn, project):
    assigned = store.create_task(
        conn, project_id=project["id"], title="Build report", type="code",
        assignee="codex",
    )
    store.update_task(conn, assigned, status="assigned")
    review = store.create_task(
        conn, project_id=project["id"], title="Approve report", type="review",
    )
    store.update_task(conn, review, status="review")

    assert pm.portfolio_focus(conn)["focus"] == {
        "kind": "decision", "id": review, "project_id": project["id"],
    }


def test_focus_treats_owner_assignment_as_human_attention(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Checkpoint the branch",
        type="other", assignee="owner",
    )
    store.update_task(conn, task_id, status="assigned")
    focus = pm.portfolio_focus(conn)
    assert focus["focus"]["kind"] == "decision"
    assert focus["needs_decision_ids"] == [task_id]
    assert focus["working_ids"] == []


def test_focus_does_not_offer_work_with_an_unfinished_dependency(conn, project):
    upstream = store.create_task(
        conn, project_id=project["id"], title="Prepare inputs", type="planning"
    )
    downstream = store.create_task(
        conn, project_id=project["id"], title="Build output", type="code"
    )
    store.add_task_dependency(conn, downstream, upstream)
    focus = pm.portfolio_focus(conn)
    assert upstream in focus["ready_task_ids"]
    assert downstream not in focus["ready_task_ids"]

    store.update_task(conn, upstream, status="done")
    focus = pm.portfolio_focus(conn)
    assert downstream in focus["ready_task_ids"]
