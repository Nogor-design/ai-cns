"""HTTP-level dashboard tests against an isolated local server."""

from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from cortex import (
    db, github_projects, jobs, project_blueprints, project_registration,
    runlog, store, webapp,
)


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


def test_blueprint_preview_approval_and_read_only_projection_are_guarded(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "blueprint-http"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Blueprint HTTP", repo_path=str(repo)
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    token = portfolio["action_token"]
    project = next(item for item in portfolio["projects"] if item["project_id"] == project_id)
    assert project["blueprint"]["status"] == "missing"

    markdown = "# Project Blueprint\n\n" + "\n\n".join(
        f"## {heading}\nEvidence for {heading.lower()}."
        for heading in project_blueprints.REQUIRED_SECTIONS
    ) + "\n"
    phases = [{
        "name": "Foundation", "status": "active",
        "outcome": "The approved phase projection is visible.",
        "exit_criteria": ["Portfolio payload exposes the phase"],
    }]
    headers = {"X-Cortex-Action-Token": token}
    status, prepared = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/preview",
        method="POST",
        body={"markdown": markdown, "phases": phases, "plan_basis": {"source": "test"}},
        headers=headers,
    )
    assert status == 200
    assert not (repo / ".cortex" / "blueprint.md").exists()
    preview = prepared["preview"]

    status, approved = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/approve",
        method="POST",
        body={
            "markdown": preview["markdown"],
            "phases": preview["phases"],
            "plan_basis": preview["plan_basis"],
            "preview_fingerprint": preview["preview_fingerprint"],
            "expected_current_hash": preview["expected_current_hash"],
        },
        headers=headers,
    )
    assert status == 200
    assert approved["blueprint"]["status"] == "approved"

    _, refreshed = request_json(dashboard_server, "/api/portfolio")
    project = next(item for item in refreshed["projects"] if item["project_id"] == project_id)
    assert project["blueprint"]["phases"][0]["name"] == "Foundation"


def test_blueprint_http_approval_requires_a_saved_server_preview(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "blueprint-http-saved-preview"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Saved preview required", repo_path=str(repo)
        )
        prepared = project_blueprints.preview(
            conn,
            project_id,
            markdown="# Project Blueprint\n\n" + "\n\n".join(
                f"## {heading}\nEvidence for {heading.lower()}."
                for heading in project_blueprints.REQUIRED_SECTIONS
            ) + "\n",
            phases=[{
                "name": "Foundation",
                "status": "active",
                "outcome": "Only a saved server preview can be approved.",
                "exit_criteria": ["Approval reloads stored content"],
            }],
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}

    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/blueprint/approve",
            method="POST",
            body={
                "markdown": prepared["markdown"],
                "phases": prepared["phases"],
                "plan_basis": prepared["plan_basis"],
                "preview_fingerprint": prepared["preview_fingerprint"],
                "expected_current_hash": prepared["expected_current_hash"],
            },
            headers=headers,
        )
    assert caught.value.code == 400
    payload = json.loads(caught.value.read().decode("utf-8"))
    assert payload["error"] == "no saved blueprint approval preview is ready"
    assert not (repo / ".cortex" / "blueprint.md").exists()


def test_blueprint_content_endpoint_returns_approved_markdown(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "blueprint-content"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Blueprint content HTTP", repo_path=str(repo)
        )

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}
    markdown = "# Project Blueprint\n\n" + "\n\n".join(
        f"## {heading}\nEvidence for {heading.lower()}."
        for heading in project_blueprints.REQUIRED_SECTIONS
    ) + "\n"
    phases = [{
        "name": "Baseline",
        "status": "active",
        "outcome": "Approved markdown is readable from source document API.",
        "exit_criteria": ["Exit criterion is linked to evidence"],
    }]

    status, preview = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/preview",
        method="POST",
        body={
            "markdown": markdown,
            "phases": phases,
            "plan_basis": {"source": "test"},
        },
        headers=headers,
    )
    assert status == 200

    status, payload = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/approve",
        method="POST",
        body={
            "markdown": preview["preview"]["markdown"],
            "phases": preview["preview"]["phases"],
            "plan_basis": preview["preview"]["plan_basis"],
            "preview_fingerprint": preview["preview"]["preview_fingerprint"],
            "expected_current_hash": preview["preview"]["expected_current_hash"],
        },
        headers=headers,
    )
    assert status == 200
    assert payload["blueprint"]["status"] == "approved"

    status, content = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/content",
        headers=headers,
    )
    assert status == 200
    assert content["status"] == "approved"
    assert content["exists"] is True
    assert content["path"] == str((repo / ".cortex" / "blueprint.md").resolve())
    assert "## " + project_blueprints.REQUIRED_SECTIONS[0] in content["content"]


def test_blueprint_content_endpoint_requires_action_token(dashboard_server, isolated_db, tmp_path):
    repo = tmp_path / "blueprint-content-token"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Blueprint content token", repo_path=str(repo)
        )

    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/blueprint/content",
        )
    assert caught.value.code == 403


def test_blueprint_content_endpoint_responds_404_for_missing_file(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "blueprint-content-missing"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Blueprint content missing", repo_path=str(repo)
        )

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}
    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/blueprint/content",
            headers=headers,
        )
    payload = json.loads(caught.value.read().decode("utf-8"))
    assert caught.value.code == 404
    assert payload["error"] == "blueprint file not found"


def test_portfolio_payload_exposes_phase_rows_in_the_roadmap_payload_with_dependencies(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "roadmap-phase-payload"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Roadmap phase payload", repo_path=str(repo)
        )
        preview = project_blueprints.preview(
            conn,
            project_id,
            markdown="# Project Blueprint\n\n" + "\n\n".join(
                f"## {heading}\nEvidence for {heading.lower()}."
                for heading in project_blueprints.REQUIRED_SECTIONS
            ) + "\n",
            phases=[
                {
                    "name": "Foundation",
                    "status": "active",
                    "outcome": "Foundation is approved.",
                    "entry_criteria": ["Project is available"],
                    "exit_criteria": ["Foundation is confirmed"],
                },
                {
                    "name": "Execution",
                    "status": "planned",
                    "outcome": "Execution follows foundation.",
                    "entry_criteria": ["Foundation complete"],
                    "exit_criteria": ["Execution is complete"],
                    "depends_on_ordinals": [1],
                },
            ],
        )
        approved = project_blueprints.approve(
            conn,
            project_id,
            markdown=preview["markdown"],
            phases=preview["phases"],
            plan_basis=preview["plan_basis"],
            preview_fingerprint=preview["preview_fingerprint"],
            expected_current_hash=preview["expected_current_hash"],
        )

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    roadmap = portfolio["roadmap_tasks"]
    rows = {item["id"]: item for item in roadmap}
    phase_rows = [item for item in roadmap if item.get("layer") == "phases"]

    assert len(phase_rows) == 2
    assert rows[approved["phases"][0]["id"]]["layer"] == "phases"
    assert rows[approved["phases"][1]["id"]]["layer"] == "phases"
    assert rows[approved["phases"][1]["id"]]["dependencies"] == [{
        "task_id": approved["phases"][1]["id"],
        "depends_on_task_id": approved["phases"][0]["id"],
        "depends_on_title": approved["phases"][0]["name"],
        "depends_on_status": "active",
        "type": "phase",
        "satisfied": False,
    }]


def test_guided_blueprint_draft_endpoints_save_and_resume_without_provider_contact(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "guided-blueprint-http"
    repo.mkdir()
    (repo / "README.md").write_text("# Guided\n", encoding="utf-8")
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Guided HTTP", repo_path=str(repo))
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}
    answers = {
        "users_and_problem": "Operators need a trusted project handoff.",
        "desired_outcome": "The owner can verify the current delivery gate.",
        "non_goals": "Do not dispatch or complete work automatically.",
        "constraints": "Discovery stays local during onboarding.",
        "open_decision": "No open decision currently.",
        "phase_name": "Trustworthy onboarding",
        "phase_outcome": "The blueprint and active phase are visible.",
        "phase_exit_criteria": "Exact preview is approved\nPhase rail is visible",
    }

    status, saved = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/draft",
        method="POST",
        body={"answers": answers, "stage": "planning"},
        headers=headers,
    )

    assert status == 200
    assert saved["draft"]["answers"] == answers
    assert saved["draft"]["planning_task_id"]
    assert saved["draft"]["discovery"]["bounded_characters"] > 0
    assert not (repo / ".cortex" / "blueprint.md").exists()

    status, prepared = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/draft-preview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    assert prepared["preview"]["plan_basis"]["provider_contacted"] is False
    assert prepared["preview"]["writes"]["create_tasks"] is False

    # Every request opens a new database connection. Reloading the draft proves
    # the exact browser approval payload is durable rather than component state.
    status, resumed = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/draft",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    assert resumed["draft"]["stage"] == "review"
    assert resumed["draft"]["preview"] == prepared["preview"]

    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/blueprint/approve",
            method="POST",
            body={"preview_fingerprint": "wrong-preview"},
            headers=headers,
        )
    assert caught.value.code == 400

    status, approved = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/blueprint/approve",
        method="POST",
        body={"preview_fingerprint": prepared["preview"]["preview_fingerprint"]},
        headers=headers,
    )
    assert status == 200
    assert approved["blueprint"]["status"] == "approved"
    assert (repo / ".cortex" / "blueprint.md").exists()
    with db.connect(isolated_db) as conn:
        project = store.get_project(conn, project_id)
        assert project["blueprint_status"] == "approved"
        assert conn.execute(
            "SELECT 1 FROM project_blueprint_drafts WHERE project_id=?",
            (project_id,),
        ).fetchone() is None


def test_phase_decomposition_http_preview_approval_and_task_conversion(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "phase-decomposition-http"
    repo.mkdir()
    markdown = "# Project Blueprint\n\n" + "\n\n".join(
        f"## {heading}\nEvidence for {heading.lower()}."
        for heading in project_blueprints.REQUIRED_SECTIONS
    ) + "\n"
    phases = [{
        "name": "Approved slice",
        "status": "active",
        "outcome": "The phase can be decomposed without automatic execution.",
        "exit_criteria": ["One criterion-linked task reaches review"],
    }]
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Phase decomposition HTTP", repo_path=str(repo)
        )
        prepared = project_blueprints.preview(
            conn, project_id, markdown=markdown, phases=phases
        )
        blueprint = project_blueprints.approve(
            conn,
            project_id,
            markdown=prepared["markdown"],
            phases=prepared["phases"],
            plan_basis=prepared["plan_basis"],
            preview_fingerprint=prepared["preview_fingerprint"],
            expected_current_hash=None,
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/decomposition-preview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    phase_preview = response["preview"]
    assert phase_preview["writes"]["create_tasks"] is False
    assert len(phase_preview["suggestions"]) == 1

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/decomposition-approve",
        method="POST",
        body={"preview_fingerprint": phase_preview["preview_fingerprint"]},
        headers=headers,
    )
    assert status == 201
    suggestion_id = response["decomposition"]["suggestion_ids"][0]
    with db.connect(isolated_db) as conn:
        assert store.list_tasks(conn, project_id) == []

    _, refreshed = request_json(dashboard_server, "/api/portfolio")
    suggestion = next(item for item in refreshed["suggestions"] if item["id"] == suggestion_id)
    assert suggestion["phase_name"] == "Approved slice"
    assert suggestion["exit_criterion_ref"] == "exit-1"

    status, response = request_json(
        dashboard_server,
        f"/api/suggestions/{suggestion_id}/approve",
        method="POST",
        body={"start": False},
    )
    assert status == 201
    assert response["task"]["phase_id"] == blueprint["current_phase_id"]
    assert response["task"]["exit_criterion_ref"] == "exit-1"
    task_id = response["task"]["id"]
    with db.connect(isolated_db) as conn:
        store.update_task(conn, task_id, status="review")

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/evidence-overview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    criterion = response["evidence"]["criteria"][0]
    assert criterion["can_accept"] is True
    assert response["evidence"]["progress"]["percent"] == 0

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/evidence-approve",
        method="POST",
        body={
            "exit_criterion_ref": "exit-1",
            "preview_fingerprint": criterion["preview_fingerprint"],
        },
        headers=headers,
    )
    assert status == 201
    assert response["acceptance"]["created"] is True

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/review-preview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    review_preview = response["preview"]
    assert review_preview["can_transition"] is True
    assert review_preview["progress"]["percent"] == 100

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/review-approve",
        method="POST",
        body={"preview_fingerprint": review_preview["preview_fingerprint"]},
        headers=headers,
    )
    assert status == 200
    assert response["blueprint"]["phases"][0]["status"] == "review"
    assert response["blueprint"]["phases"][0]["completed_at"] is None

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/completion-preview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    completion_preview = response["preview"]
    assert completion_preview["phase_id"] == blueprint["current_phase_id"]
    assert completion_preview["can_transition"] is True

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phase/completion-approve",
        method="POST",
        body={"preview_fingerprint": completion_preview["preview_fingerprint"]},
        headers=headers,
    )
    assert status == 200
    assert response["blueprint"]["phases"][0]["status"] == "complete"
    assert response["blueprint"]["phases"][0]["completed_at"] is not None


def test_phase_dependency_update_endpoint_writes_updated_phase_projection(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "phase-dependency-update-http"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(conn, name="Phase dependency HTTP", repo_path=str(repo))
        prepared = project_blueprints.preview(
            conn,
            project_id,
            markdown="# Project Blueprint\n\n" + "\n\n".join(
                f"## {heading}\nEvidence for {heading.lower()}."
                for heading in project_blueprints.REQUIRED_SECTIONS
            ) + "\n",
            phases=[
                {
                    "name": "Foundation",
                    "status": "active",
                    "outcome": "The blueprint baseline is approved.",
                    "entry_criteria": ["Project is available"],
                    "exit_criteria": ["Baseline is stable"],
                },
                {
                    "name": "Execution",
                    "status": "planned",
                    "outcome": "Execution can start from the baseline.",
                    "entry_criteria": ["Foundation complete"],
                    "exit_criteria": ["Execution is complete"],
                },
            ],
        )
        blueprint = project_blueprints.approve(
            conn,
            project_id,
            markdown=prepared["markdown"],
            phases=prepared["phases"],
            plan_basis=prepared["plan_basis"],
            preview_fingerprint=prepared["preview_fingerprint"],
            expected_current_hash=prepared["expected_current_hash"],
        )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}
    planned_id = blueprint["phases"][1]["id"]
    foundation_id = blueprint["phases"][0]["id"]

    status, response = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/phases/{planned_id}/dependencies",
        method="POST",
        body={"depends_on_ordinals": [1]},
        headers=headers,
    )
    assert status == 200
    updated = next(phase for phase in response["blueprint"]["phases"] if phase["id"] == planned_id)
    assert [dep["depends_on_ordinal"] for dep in updated["depends_on"]] == [1]
    with db.connect(isolated_db) as conn:
        persisted = conn.execute(
            """
            SELECT depends_on_phase_id
            FROM phase_dependencies
            WHERE phase_id=?
            """,
            (planned_id,),
        ).fetchone()
    assert persisted is not None
    assert persisted[0] == foundation_id


def test_phase_dependency_update_endpoint_requires_action_token(dashboard_server, isolated_db, tmp_path):
    repo = tmp_path / "phase-dependency-update-token"
    repo.mkdir()
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Phase dependency HTTP token", repo_path=str(repo)
        )
        prepared = project_blueprints.preview(
            conn,
            project_id,
            markdown="# Project Blueprint\n\n" + "\n\n".join(
                f"## {heading}\nEvidence for {heading.lower()}."
                for heading in project_blueprints.REQUIRED_SECTIONS
            ) + "\n",
            phases=[
                {
                    "name": "Foundation",
                    "status": "active",
                    "outcome": "The approved baseline is present.",
                    "entry_criteria": ["Project is available"],
                    "exit_criteria": ["Stable"],
                },
                {
                    "name": "Execution",
                    "status": "planned",
                    "outcome": "Execution can start from the baseline.",
                    "entry_criteria": ["Foundation complete"],
                    "exit_criteria": ["Execution complete"],
                },
            ],
        )
        blueprint = project_blueprints.approve(
            conn,
            project_id,
            markdown=prepared["markdown"],
            phases=prepared["phases"],
            plan_basis=prepared["plan_basis"],
            preview_fingerprint=prepared["preview_fingerprint"],
            expected_current_hash=prepared["expected_current_hash"],
        )
    planned_id = blueprint["phases"][1]["id"]

    with pytest.raises(HTTPError) as caught:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/phases/{planned_id}/dependencies",
            method="POST",
            body={"depends_on_ordinals": [1]},
        )
    assert caught.value.code == 403


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


def test_guided_project_preview_and_registration_require_local_action_token(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "guided-http"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='guided-http'\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    status, portfolio = request_json(dashboard_server, "/api/portfolio")
    assert status == 200
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}

    with pytest.raises(HTTPError) as missing_token:
        request_json(
            dashboard_server, "/api/projects/preview", method="POST",
            body={"repo_path": str(repo)},
        )
    assert missing_token.value.code == 403

    status, payload = request_json(
        dashboard_server, "/api/projects/preview", method="POST",
        body={"repo_path": str(repo)}, headers=headers,
    )
    assert status == 200
    assert payload["preview"]["stack"] == "Python"
    assert payload["preview"]["test_command"] == "python -m pytest -q"

    status, created = request_json(
        dashboard_server, "/api/projects", method="POST", headers=headers,
        body={
            "repo_path": str(repo),
            "name": "Guided HTTP",
            "program": "portfolio intake",
            "priority": 2,
            "privacy": "restricted",
            "stack": payload["preview"]["stack"],
            "test_command": payload["preview"]["test_command"],
            "current_goal": "Make registration dependable",
            "allowed_workers": ["ollama"],
            "track_state": True,
        },
    )
    assert status == 201
    assert created["project"]["id"] == "guided-http"
    assert created["state_created"] is True
    assert (repo / ".cortex" / "state.md").is_file()
    with db.connect(isolated_db) as conn:
        project = store.get_project(conn, "guided-http")
        assert json.loads(project["allowed_workers"]) == ["ollama"]
        event = store.list_activity_events(conn, "guided-http")[0]
        assert event["actor_name"] == "owner"
        assert event["source"] == "dashboard"


def test_folder_browser_requires_local_action_token_and_returns_path(
    dashboard_server, tmp_path, monkeypatch
):
    repo = tmp_path / "picked-http"
    repo.mkdir()
    monkeypatch.setattr(
        project_registration,
        "choose_project_folder",
        lambda initial_path=None: str(repo.resolve()),
    )
    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}

    with pytest.raises(HTTPError) as missing_token:
        request_json(
            dashboard_server,
            "/api/projects/browse",
            method="POST",
            body={"initial_path": str(tmp_path)},
        )
    assert missing_token.value.code == 403

    status, payload = request_json(
        dashboard_server,
        "/api/projects/browse",
        method="POST",
        body={"initial_path": str(tmp_path)},
        headers=headers,
    )
    assert status == 200
    assert payload == {"selected": True, "repo_path": str(repo.resolve())}


def test_guarded_project_removal_preserves_repository_and_timeline_audit(
    dashboard_server, isolated_db, tmp_path
):
    repo = tmp_path / "remove-http"
    repo.mkdir()
    state_path = repo / ".cortex" / "state.md"
    state_path.parent.mkdir()
    state_path.write_text("# Preserve this state\n", encoding="utf-8")
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Remove HTTP", repo_path=str(repo), record_activity=True
        )
        store.create_task(conn, project_id=project_id, title="Old local task")

    _, portfolio = request_json(dashboard_server, "/api/portfolio")
    headers = {"X-Cortex-Action-Token": portfolio["action_token"]}

    with pytest.raises(HTTPError) as missing_token:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}/removal-preview",
            method="POST",
            body={},
        )
    assert missing_token.value.code == 403

    status, payload = request_json(
        dashboard_server,
        f"/api/projects/{project_id}/removal-preview",
        method="POST",
        body={},
        headers=headers,
    )
    assert status == 200
    assert payload["preview"]["project_name"] == "Remove HTTP"
    assert payload["preview"]["deleted_counts"]["tasks"] == 1
    assert payload["preview"]["preserved"]["state_exists"] is True

    with pytest.raises(HTTPError) as wrong_name:
        request_json(
            dashboard_server,
            f"/api/projects/{project_id}",
            method="DELETE",
            body={"confirm_name": "remove http", "acknowledge_permanent": True},
            headers=headers,
        )
    assert wrong_name.value.code == 400

    status, removed = request_json(
        dashboard_server,
        f"/api/projects/{project_id}",
        method="DELETE",
        body={"confirm_name": "Remove HTTP", "acknowledge_permanent": True},
        headers=headers,
    )
    assert status == 200
    assert removed["removal"]["project_id"] == project_id
    assert state_path.read_text(encoding="utf-8") == "# Preserve this state\n"
    with db.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)
        ).fetchone() is None
        tombstone = conn.execute(
            "SELECT * FROM project_removals WHERE project_id = ?", (project_id,)
        ).fetchone()
        assert tombstone["actor_type"] == "human"
        assert tombstone["actor_name"] == "owner"

    _, refreshed = request_json(dashboard_server, "/api/portfolio")
    assert not any(project["project_id"] == project_id for project in refreshed["projects"])
    removal_event = next(
        event for event in refreshed["activity"]
        if event["action"] == "project.removed" and event["project_id"] == project_id
    )
    assert removal_event["actor_name"] == "owner"
    assert removal_event["evidence"]["preserved"]["state_exists"] is True


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
