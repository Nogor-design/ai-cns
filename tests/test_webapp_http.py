"""HTTP-level dashboard tests against an isolated local server."""

from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from cortex import db, jobs, runlog, store, webapp


@pytest.fixture
def dashboard_server(isolated_db, tmp_path):
    static_root = tmp_path / "dashboard"
    static_root.mkdir()
    (static_root / "index.html").write_text(
        "<!doctype html><title>Cortex test</title><main>Cortex test</main>",
        encoding="utf-8",
    )
    server = webapp.CortexDashboardServer(
        ("127.0.0.1", 0),
        webapp.DashboardHandler,
        database_path=isolated_db,
        static_root=static_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request_json(base: str, path: str, *, method: str = "GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base + path,
        data=data,
        method=method,
        headers={
            **({"Content-Type": "application/json"} if data else {}),
            **(headers or {}),
        },
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_health_and_static_app_are_served(dashboard_server):
    status, payload = request_json(dashboard_server, "/api/health")
    assert status == 200
    assert payload["ok"] is True

    with urlopen(dashboard_server + "/", timeout=5) as response:
        html = response.read().decode("utf-8")
    assert response.status == 200
    assert "Cortex test" in html


def test_jobs_are_visible_through_collection_and_detail_endpoints(
    dashboard_server, isolated_db
):
    with db.connect(isolated_db) as conn:
        job_id = jobs.create(conn, kind="plan", label="Planning")
        jobs.finish(conn, job_id, status="done", result={"suggestions": 3})

    status, payload = request_json(dashboard_server, "/api/jobs")
    assert status == 200
    assert payload["jobs"][0]["id"] == job_id
    assert payload["jobs"][0]["result"] == {"suggestions": 3}

    status, payload = request_json(dashboard_server, f"/api/jobs/{job_id}")
    assert status == 200
    assert payload["job"]["status"] == "done"


def test_run_output_endpoint_supports_incremental_polling(
    dashboard_server, isolated_db, tmp_path
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="HTTP Demo", repo_path=str(tmp_path), stack="Python"
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Stream output", type="review"
        )
        run_id = store.create_run(
            conn, task_id=task_id, project_id=project_id,
            model="codex:default", execution_mode="headless_cli",
        )

    runlog.start(run_id, "first\n")
    status, first = request_json(
        dashboard_server, f"/api/runs/{run_id}/output?offset=0"
    )
    assert status == 200
    assert first["text"] == "first\n"
    assert first["running"] is True

    runlog.append(run_id, "second\n")
    with db.connect(isolated_db) as conn:
        store.update_run(conn, run_id, ended_at="2026-08-02T12:00:00Z", exit_code=0)
    status, second = request_json(
        dashboard_server,
        f"/api/runs/{run_id}/output?offset={first['offset']}",
    )
    assert status == 200
    assert second["text"] == "second\n"
    assert second["running"] is False
    assert second["exit_code"] == 0


def test_unknown_job_returns_json_404(dashboard_server):
    with pytest.raises(HTTPError) as caught:
        request_json(dashboard_server, "/api/jobs/not-a-job")
    assert caught.value.code == 404
    payload = json.loads(caught.value.read().decode("utf-8"))
    assert payload == {"error": "job not found"}


def test_unknown_api_route_does_not_fall_back_to_the_react_app(dashboard_server):
    with pytest.raises(HTTPError) as caught:
        request_json(dashboard_server, "/api/not-a-route")
    assert caught.value.code == 404
    assert caught.value.headers.get_content_type() == "application/json"
    payload = json.loads(caught.value.read().decode("utf-8"))
    assert payload == {"error": "route not found"}


def test_project_allowlist_can_be_updated_over_http(
    dashboard_server, isolated_db, tmp_path
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Policy Demo", repo_path=str(tmp_path), privacy="restricted"
        )

    status, payload = request_json(
        dashboard_server,
        f"/api/projects/{project_id}",
        method="PATCH",
        body={"allowed_workers": ["claude", "ollama"]},
    )
    assert status == 200
    assert json.loads(payload["project"]["allowed_workers"]) == ["claude", "ollama"]

    status, portfolio = request_json(dashboard_server, "/api/portfolio")
    assert status == 200
    project = next(
        item for item in portfolio["projects"] if item["project_id"] == project_id
    )
    assert project["allowed_workers"] == ["claude", "ollama"]
    assert project["allowlist_configured"] is True


def test_activity_endpoint_filters_by_project_task_and_session(
    dashboard_server, isolated_db, tmp_path
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Activity Demo", repo_path=str(tmp_path), stack="Python"
        )
        task_id = store.create_task(conn, project_id=project_id, title="Trace me")
        store.create_activity_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            actor_type="agent",
            actor_name="gemini",
            action="review.completed",
            summary="Review complete",
            session_id="session-one",
            evidence={"accepted": True},
        )

    status, payload = request_json(
        dashboard_server,
        f"/api/activity?project={project_id}&task={task_id}&session=session-one",
    )
    assert status == 200
    assert len(payload["activity"]) == 1
    assert payload["activity"][0]["actor_name"] == "gemini"
    assert payload["activity"][0]["evidence"] == {"accepted": True}


def test_codex_preview_returns_copy_fallback_and_records_activity(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    capability = {
        "available": True,
        "reason": None,
        "server": "Codex Desktop test",
        "methods": {
            "thread/list": True, "thread/start": True,
            "thread/resume": True, "turn/start": True,
        },
        "threads": [],
        "linked_thread_found": False,
    }
    monkeypatch.setattr(webapp.codex_app, "inspect", lambda _cwd, _thread: capability)
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Codex Preview", repo_path=str(tmp_path), current_goal="Ship safely"
        )
        task_id = store.create_task(
            conn,
            project_id=project_id,
            title="Preview a bounded handoff",
            acceptance="Prompt includes the done-when check",
            pm_session_id="pm-session",
        )

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}/codex/preview",
        method="POST",
        body={},
        headers={"X-Cortex-Action-Token": portfolio["action_token"]},
    )
    assert status == 200
    assert payload["mode"] == "copy_prompt"
    assert payload["start_enabled"] is False
    assert payload["can_start_turn"] is False
    assert payload["can_navigate"] is False
    assert "Preview a bounded handoff" in payload["prompt"]
    assert payload["capability"] == capability
    with db.connect(isolated_db) as conn:
        event = next(
            row for row in store.list_activity_events(conn, task_id=task_id)
            if row["action"] == "codex.launch_previewed"
        )
    assert event["action"] == "codex.launch_previewed"
    assert event["session_id"] == "pm-session"


def test_codex_preview_rejects_missing_action_token_before_probe(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    called = False

    def inspect(_cwd, _thread):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(webapp.codex_app, "inspect", inspect)
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Guarded", repo_path=str(tmp_path))
        task_id = store.create_task(
            conn, project_id=project_id, title="Guard me", acceptance="Guard passes"
        )
    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/tasks/{task_id}/codex/preview",
            method="POST",
            body={},
        )
    assert caught.value.code == 403
    assert called is False


@pytest.mark.parametrize(
    ("privacy", "acceptance", "expected"),
    [
        ("restricted", "Safe result exists", "not allowed"),
        ("internal", None, "done-when"),
        ("internal", "Never expose sk-123456789012345678901234", "privacy scan"),
    ],
)
def test_codex_preview_blocks_policy_scope_and_secrets(
    dashboard_server, isolated_db, tmp_path, monkeypatch,
    privacy, acceptance, expected,
):
    monkeypatch.setattr(
        webapp.codex_app,
        "inspect",
        lambda _cwd, _thread: pytest.fail("blocked previews must not probe App Server"),
    )
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name=f"Blocked {privacy}", repo_path=str(tmp_path), privacy=privacy
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Blocked preview", acceptance=acceptance
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/tasks/{task_id}/codex/preview",
            method="POST",
            body={},
            headers={"X-Cortex-Action-Token": portfolio["action_token"]},
        )
    assert caught.value.code == 400
    assert expected in caught.value.read().decode("utf-8")
