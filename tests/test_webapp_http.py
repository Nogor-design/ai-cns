"""HTTP-level dashboard tests against an isolated local server."""

from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from cortex import db, github_projects, jobs, runlog, store, webapp


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


def test_dashboard_schedule_patch_is_attributed_to_the_human_owner(
    dashboard_server, isolated_db, tmp_path
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Roadmap attribution", repo_path=str(tmp_path)
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Schedule this work"
        )

    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}",
        method="PATCH",
        body={
            "start_at": "2026-08-20",
            "target_at": "2026-08-24",
            "due_at": "2026-08-25",
            "milestone": "Portfolio visibility",
            "progress": 40,
        },
    )

    assert status == 200
    assert payload["task"]["start_at"] == "2026-08-20"
    assert payload["task"]["target_at"] == "2026-08-24"
    assert payload["task"]["due_at"] == "2026-08-25"
    assert payload["task"]["milestone"] == "Portfolio visibility"
    assert payload["task"]["progress"] == 40
    with db.connect(isolated_db) as conn:
        event = next(
            row for row in store.list_activity_events(conn, task_id=task_id)
            if row["action"] == "task.updated"
        )
    assert event["actor_type"] == "human"
    assert event["actor_name"] == "owner"
    assert event["source"] == "dashboard"


def test_adapter_owned_github_evidence_cannot_be_forged_over_http(
    dashboard_server, isolated_db, tmp_path
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Mirror Guard", repo_path=str(tmp_path)
        )
        task_id = store.create_task(
            conn,
            project_id=project_id,
            title="Guard adapter evidence",
            github_issue_id="I_original",
            github_issue_number=7,
            github_project_item_id="PVTI_original",
            sync_state='{"fields":{},"fingerprint":"v1:original"}',
        )

    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}",
        method="PATCH",
        body={
            "title": "Allowed title edit",
            "github_issue_id": "I_forged",
            "github_issue_number": 99,
            "github_project_item_id": "PVTI_forged",
            "sync_state": '{"fields":{},"fingerprint":"v1:forged"}',
        },
    )

    assert status == 200
    assert payload["task"]["title"] == "Allowed title edit"
    assert payload["task"]["github_issue_id"] == "I_original"
    assert payload["task"]["github_issue_number"] == 7
    assert payload["task"]["github_project_item_id"] == "PVTI_original"
    assert payload["task"]["sync_state"] == '{"fields":{},"fingerprint":"v1:original"}'


def test_github_preview_is_explicit_read_only_and_returns_exact_cli_command(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Mirror Preview", repo_path=str(tmp_path)
        )
        store.update_project(
            conn,
            project_id,
            github_project_owner="octo",
            github_project_number=4,
            github_project_id="PVT_target",
        )
        task_id = store.create_task(
            conn,
            project_id=project_id,
            title="Preview mirror plan",
            github_issue_id="I_issue",
            github_issue_number=12,
            github_issue_url="https://github.com/octo/demo/issues/12",
        )
        event_count = conn.execute("SELECT count(*) FROM activity_events").fetchone()[0]
        operation_count = conn.execute(
            "SELECT count(*) FROM github_mirror_operations"
        ).fetchone()[0]

    plan = github_projects.MirrorPlan(
        task_id=task_id,
        project_id="PVT_target",
        issue_id="I_issue",
        project_item_id="PVTI_item",
        snapshot_digest="snapshot-v1",
        fingerprint="plan-v1:approved",
        actions=(
            github_projects.MirrorAction(
                "set_project_field", "PVTI_item", field="Status", value="Todo"
            ),
        ),
        conflicts=(),
    )
    snapshot = {
        "project_id": "PVT_target",
        "project_title": "Engineering Mirror",
        "items": [{
            "id": "PVTI_item",
            "content_id": "I_issue",
            "content": {
                "title": "GitHub issue title",
                "state": "OPEN",
                "url": "https://github.com/octo/demo/issues/12",
            },
        }],
    }
    monkeypatch.setattr(
        webapp.github_adapter, "prepare", lambda _task, _owner, _number: (plan, snapshot)
    )

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}/github/preview",
        method="POST",
        body={},
        headers={"X-Cortex-Action-Token": portfolio["action_token"]},
    )

    assert status == 200
    assert payload["mode"] == "read_only_preview"
    assert payload["status"] == "actions_required"
    assert payload["can_apply"] is False
    assert payload["dashboard_apply_enabled"] is False
    assert payload["target"] == {
        "owner": "octo",
        "number": 4,
        "project_id": "PVT_target",
        "title": "Engineering Mirror",
    }
    assert payload["issue"]["title"] == "GitHub issue title"
    assert payload["plan"]["fingerprint"] == "plan-v1:approved"
    assert payload["command"].startswith(
        f".\\scripts\\cortex-portfolio.ps1 github apply {task_id} "
    )
    assert "--approve plan-v1:approved" in payload["command"]
    assert payload["command_kind"] == "apply"
    with db.connect(isolated_db) as conn:
        assert conn.execute("SELECT count(*) FROM activity_events").fetchone()[0] == event_count
        assert conn.execute(
            "SELECT count(*) FROM github_mirror_operations"
        ).fetchone()[0] == operation_count


def test_github_preview_rejects_missing_action_token_before_remote_read(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    called = False

    def prepare(_task, _owner, _number):
        nonlocal called
        called = True
        raise AssertionError("guard must run before GitHub")

    monkeypatch.setattr(webapp.github_adapter, "prepare", prepare)
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Guarded mirror", repo_path=str(tmp_path))
        store.update_project(
            conn, project_id,
            github_project_owner="octo", github_project_number=4,
            github_project_id="PVT_target",
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Guard remote read", github_issue_id="I_issue"
        )
    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/tasks/{task_id}/github/preview",
            method="POST",
            body={},
        )
    assert caught.value.code == 403
    assert called is False


def test_github_preview_rejects_cross_site_origin_before_remote_read(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    called = False

    def prepare(_task, _owner, _number):
        nonlocal called
        called = True
        raise AssertionError("origin guard must run before GitHub")

    monkeypatch.setattr(webapp.github_adapter, "prepare", prepare)
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Origin guard", repo_path=str(tmp_path))
        store.update_project(
            conn, project_id,
            github_project_owner="octo", github_project_number=4,
            github_project_id="PVT_target",
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Reject cross-site", github_issue_id="I_issue"
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/tasks/{task_id}/github/preview",
            method="POST",
            body={},
            headers={
                "Origin": "https://attacker.example",
                "X-Cortex-Action-Token": portfolio["action_token"],
            },
        )
    assert caught.value.code == 403
    assert called is False


@pytest.mark.parametrize(
    ("configured", "linked", "expected"),
    [(False, False, "unconfigured"), (True, False, "unlinked")],
)
def test_github_preview_explains_setup_state_without_remote_read(
    dashboard_server, isolated_db, tmp_path, monkeypatch,
    configured, linked, expected,
):
    monkeypatch.setattr(
        webapp.github_adapter,
        "prepare",
        lambda *_args: pytest.fail("setup-state preview must not read GitHub"),
    )
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name=f"Setup {expected}", repo_path=str(tmp_path))
        if configured:
            store.update_project(
                conn, project_id,
                github_project_owner="octo", github_project_number=4,
                github_project_id="PVT_target",
            )
        task_id = store.create_task(
            conn,
            project_id=project_id,
            title="Explain setup",
            github_issue_id="I_issue" if linked else None,
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}/github/preview",
        method="POST",
        body={},
        headers={"X-Cortex-Action-Token": portfolio["action_token"]},
    )
    assert status == 200
    assert payload["status"] == expected
    assert payload["plan"] is None
    assert payload["command"] is None


def test_github_preview_surfaces_durable_recovery_command(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Recover mirror", repo_path=str(tmp_path))
        store.update_project(
            conn, project_id,
            github_project_owner="octo", github_project_number=4,
            github_project_id="PVT_target",
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Resume mirror", github_issue_id="I_issue"
        )
        store.claim_github_mirror_operation(
            conn,
            operation_id="ghm-recover-123",
            task_id=task_id,
            project_id=project_id,
            plan_fingerprint="plan-v1:original",
            github_project_id="PVT_target",
            github_issue_id="I_issue",
            github_project_item_id="PVTI_item",
            actor="codex",
            session_id=None,
            actions=[{"kind": "set_project_field", "field": "Status"}],
        )
        store.update_github_mirror_operation(
            conn,
            "ghm-recover-123",
            status="interrupted",
            completed_actions=[{"index": 0, "kind": "set_project_field"}],
            evidence={"remote_writes": 1, "recoverable": True},
            error="connection ended after verification",
        )

    current_plan = github_projects.MirrorPlan(
        task_id=task_id,
        project_id="PVT_target",
        issue_id="I_issue",
        project_item_id="PVTI_item",
        snapshot_digest="snapshot-after-interruption",
        fingerprint="plan-v1:fresh-different",
        actions=(),
        conflicts=(),
    )
    monkeypatch.setattr(
        webapp.github_adapter,
        "prepare",
        lambda *_args: (
            current_plan,
            {"project_id": "PVT_target", "project_title": "Engineering Mirror", "items": []},
        ),
    )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    _, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}/github/preview",
        method="POST",
        body={},
        headers={"X-Cortex-Action-Token": portfolio["action_token"]},
    )
    assert payload["status"] == "recoverable"
    assert payload["command_kind"] == "resume"
    assert "--approve plan-v1:original" in payload["command"]
    assert "--operation-id ghm-recover-123" in payload["command"]
    assert payload["operation"]["completed_actions"] == [
        {"index": 0, "kind": "set_project_field"}
    ]
    assert payload["operation"]["evidence"] == {
        "recoverable": True, "remote_writes": 1
    }


def test_github_preview_preserves_recovery_evidence_when_fresh_read_fails(
    dashboard_server, isolated_db, tmp_path, monkeypatch
):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Offline recovery", repo_path=str(tmp_path))
        store.update_project(
            conn, project_id,
            github_project_owner="octo", github_project_number=4,
            github_project_id="PVT_target",
        )
        task_id = store.create_task(
            conn, project_id=project_id, title="Recover offline", github_issue_id="I_issue"
        )
        store.claim_github_mirror_operation(
            conn,
            operation_id="ghm-offline-123",
            task_id=task_id,
            project_id=project_id,
            plan_fingerprint="plan-v1:offline",
            github_project_id="PVT_target",
            github_issue_id="I_issue",
            github_project_item_id="PVTI_item",
            actor="codex",
            session_id=None,
            actions=[{"kind": "set_project_field", "field": "Status"}],
        )
        store.update_github_mirror_operation(
            conn, "ghm-offline-123", status="interrupted", error="network ended"
        )
    monkeypatch.setattr(
        webapp.github_adapter,
        "prepare",
        lambda *_args: (_ for _ in ()).throw(
            webapp.github_reader.GitHubProjectError("GitHub CLI is offline")
        ),
    )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    status, payload = request_json(
        dashboard_server,
        f"/api/tasks/{task_id}/github/preview",
        method="POST",
        body={},
        headers={"X-Cortex-Action-Token": portfolio["action_token"]},
    )
    assert status == 200
    assert payload["status"] == "recoverable"
    assert payload["plan"] is None
    assert payload["read_error"] == "GitHub CLI is offline"
    assert "--operation-id ghm-offline-123" in payload["command"]


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
