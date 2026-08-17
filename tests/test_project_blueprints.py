from __future__ import annotations

import json

import pytest

from cortex import dispatcher, project_blueprints, project_removal, store


def blueprint_markdown(title: str = "Demo") -> str:
    bodies = {
        "Purpose and intended users": f"{title} helps project owners.",
        "Problem and desired outcomes": "Make the current delivery intent explicit.",
        "Current baseline": "A registered Cortex project exists.",
        "Scope and non-goals": "Read-only phases; no task generation.",
        "Constraints and safeguards": "Preview before mutation.",
        "Success measures": "The approved phase rail is visible.",
        "Product or operational workflow": "Owner approves the blueprint, then Cortex projects phases.",
        "Architecture and key boundaries": "Markdown is portable; SQLite is operational.",
        "Delivery phases": "Phase 1 establishes the contract.",
        "Risks, assumptions, and open decisions": "The next phase remains intentionally inactive.",
        "Plan basis and review provenance": "Repository evidence and owner approval.",
    }
    return "# Project Blueprint\n\n" + "\n\n".join(
        f"## {heading}\n{body}" for heading, body in bodies.items()
    ) + "\n"


def phases():
    return [
        {
            "name": "Blueprint foundation",
            "status": "active",
            "outcome": "A trustworthy blueprint and read-only phase rail exist.",
            "entry_criteria": ["Project is registered"],
            "exit_criteria": ["Blueprint hash and revision are verified"],
        },
        {
            "name": "Task decomposition",
            "status": "planned",
            "outcome": "Approved phases can preview bounded suggestions.",
            "entry_criteria": ["Phase 1 accepted"],
            "exit_criteria": ["Owner-approved task preview works"],
        },
    ]


def test_preview_and_approval_create_versioned_blueprint_and_phase_projection(conn, tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    project_id = store.create_project(conn, name="Product", repo_path=str(repo))

    preview = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=phases(),
        plan_basis={"owner_answers": ["approved"]},
    )

    assert preview["writes"]["create_file"] is True
    assert preview["writes"]["create_tasks"] is False
    assert not (repo / ".cortex" / "blueprint.md").exists()

    result = project_blueprints.approve(
        conn,
        project_id,
        markdown=preview["markdown"],
        phases=preview["phases"],
        plan_basis=preview["plan_basis"],
        preview_fingerprint=preview["preview_fingerprint"],
        expected_current_hash=preview["expected_current_hash"],
    )

    path = repo / ".cortex" / "blueprint.md"
    assert path.read_text(encoding="utf-8") == blueprint_markdown()
    assert result["status"] == "approved"
    assert result["revision"]["ordinal"] == 1
    assert result["revision"]["markdown_content"] == blueprint_markdown()
    assert [phase["status"] for phase in result["phases"]] == ["active", "planned"]
    assert result["current_phase_id"] == result["phases"][0]["id"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=?", (project_id,)).fetchone()[0] == 0
    event = store.list_activity_events(conn, project_id)[0]
    assert event["action"] == "blueprint.approved"
    assert json.loads(event["evidence_json"])["preview_fingerprint"] == preview["preview_fingerprint"]


def test_existing_owner_blueprint_is_never_overwritten(conn, tmp_path):
    repo = tmp_path / "owner-design"
    path = repo / ".cortex" / "blueprint.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Owner document\n", encoding="utf-8")
    project_id = store.create_project(conn, name="Owner Design", repo_path=str(repo))

    with pytest.raises(ValueError, match="owner blueprint already exists"):
        project_blueprints.preview(
            conn, project_id, markdown=blueprint_markdown(), phases=phases()
        )
    assert path.read_text(encoding="utf-8") == "# Owner document\n"


def test_external_edit_marks_approved_blueprint_stale_without_rewriting_phases(conn, tmp_path):
    repo = tmp_path / "drift"
    repo.mkdir()
    project_id = store.create_project(conn, name="Drift", repo_path=str(repo))
    preview = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
    )
    project_blueprints.approve(
        conn,
        project_id,
        markdown=preview["markdown"],
        phases=preview["phases"],
        plan_basis=preview["plan_basis"],
        preview_fingerprint=preview["preview_fingerprint"],
        expected_current_hash=None,
    )
    path = repo / ".cortex" / "blueprint.md"
    path.write_text(blueprint_markdown("Changed"), encoding="utf-8")

    result = project_blueprints.projection(conn, project_id)

    assert result["status"] == "stale"
    assert len(result["phases"]) == 2
    assert path.read_text(encoding="utf-8") == blueprint_markdown("Changed")
    assert "blueprint.stale_detected" in {
        event["action"] for event in store.list_activity_events(conn, project_id)
    }


def test_guided_draft_is_resumable_and_blocks_new_execution(conn, tmp_path):
    repo = tmp_path / "guided"
    repo.mkdir()
    (repo / "README.md").write_text("# Guided product\n", encoding="utf-8")
    project_id = store.create_project(conn, name="Guided", repo_path=str(repo))
    task_id = store.create_task(conn, project_id=project_id, title="Too early")

    draft = project_blueprints.begin_draft(
        conn, project_id, answers={"intended_users": "operators"}, stage="questions"
    )

    assert draft["stage"] == "questions"
    assert draft["planning_task_id"]
    assert draft["pm_session_id"]
    planning_task = store.get_task(conn, draft["planning_task_id"])
    assert planning_task["title"] == "Build project blueprint"
    assert planning_task["status"] == "in_progress"
    resumed = project_blueprints.begin_draft(
        conn, project_id, answers={"desired_outcome": "Verified handoff"}, stage="planning"
    )
    assert resumed["planning_task_id"] == draft["planning_task_id"]
    assert resumed["answers"]["desired_outcome"] == "Verified handoff"
    assert project_blueprints.projection(conn, project_id)["execution_ready"] is False
    task = store.get_task(conn, task_id)
    with pytest.raises(dispatcher.DispatchError, match="not execution-ready"):
        dispatcher.preview(conn, task)


def test_saved_interview_compiles_deterministic_preview_and_closes_pm_session(
    conn, tmp_path
):
    repo = tmp_path / "interview"
    repo.mkdir()
    project_id = store.create_project(conn, name="Interview", repo_path=str(repo))
    answers = {
        "users_and_problem": "Operators need one trusted delivery view.",
        "desired_outcome": "The owner can verify the current gate from one screen.",
        "non_goals": "Do not create or dispatch tasks automatically.",
        "constraints": "All discovery remains local until the allowlist is confirmed.",
        "open_decision": "No open product decision currently.",
        "phase_name": "Trustworthy onboarding",
        "phase_outcome": "One project has an approved blueprint and active phase.",
        "phase_exit_criteria": "Exact preview is approved\nPhase rail is visible",
    }
    draft = project_blueprints.begin_draft(
        conn, project_id, answers=answers, stage="planning"
    )

    prepared = project_blueprints.draft_preview(conn, project_id)

    assert prepared["plan_basis"]["provider_contacted"] is False
    assert prepared["phases"][0]["exit_criteria"] == [
        "Exact preview is approved", "Phase rail is visible"
    ]
    assert "No specialist provider was contacted" in prepared["markdown"]
    reviewed = project_blueprints.draft_detail(conn, project_id)
    assert reviewed["stage"] == "review"
    assert reviewed["preview"] == prepared
    stored = conn.execute(
        """SELECT preview_fingerprint, preview_json, previewed_at
           FROM project_blueprint_drafts WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    assert stored["preview_fingerprint"] == prepared["preview_fingerprint"]
    assert json.loads(stored["preview_json"])["markdown"] == prepared["markdown"]
    assert stored["previewed_at"]

    approved = project_blueprints.approve_draft(
        conn,
        project_id,
        preview_fingerprint=prepared["preview_fingerprint"],
    )

    assert approved["status"] == "approved"
    planning_task = store.get_task(conn, draft["planning_task_id"])
    assert planning_task["status"] == "done"
    assert planning_task["progress"] == 100
    assert conn.execute(
        "SELECT 1 FROM project_blueprint_drafts WHERE project_id=?", (project_id,)
    ).fetchone() is None


def test_editing_saved_answers_invalidates_exact_blueprint_preview(conn, tmp_path):
    repo = tmp_path / "preview-invalidation"
    repo.mkdir()
    project_id = store.create_project(conn, name="Preview invalidation", repo_path=str(repo))
    answers = {
        "users_and_problem": "Operators need one trusted delivery view.",
        "desired_outcome": "The owner can verify the current gate from one screen.",
        "non_goals": "Do not execute automatically.",
        "constraints": "All discovery remains local.",
        "open_decision": "No open decision currently.",
        "phase_name": "Trustworthy onboarding",
        "phase_outcome": "One exact preview is restart-safe.",
        "phase_exit_criteria": "Preview survives restart",
    }
    project_blueprints.begin_draft(conn, project_id, answers=answers, stage="planning")
    prepared = project_blueprints.draft_preview(conn, project_id)

    changed = project_blueprints.begin_draft(
        conn,
        project_id,
        answers={"phase_outcome": "The revised outcome requires a fresh preview."},
        stage="planning",
    )

    assert changed["stage"] == "planning"
    assert changed["preview"] is None
    row = conn.execute(
        """SELECT preview_json, preview_fingerprint, previewed_at
           FROM project_blueprint_drafts WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None)
    with pytest.raises(ValueError, match="no saved blueprint approval preview"):
        project_blueprints.approve_draft(
            conn,
            project_id,
            preview_fingerprint=prepared["preview_fingerprint"],
        )


def test_process_crash_after_blueprint_file_write_fails_closed(
    conn, tmp_path, monkeypatch
):
    repo = tmp_path / "crash-after-write"
    repo.mkdir()
    project_id = store.create_project(conn, name="Crash after write", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
    )
    original_write = project_blueprints._write_blueprint

    def write_then_crash(path, markdown, expected_hash):
        original_write(path, markdown, expected_hash)
        raise SystemExit("simulated process death")

    monkeypatch.setattr(project_blueprints, "_write_blueprint", write_then_crash)
    with pytest.raises(SystemExit, match="simulated process death"):
        project_blueprints.approve(
            conn,
            project_id,
            markdown=prepared["markdown"],
            phases=prepared["phases"],
            plan_basis=prepared["plan_basis"],
            preview_fingerprint=prepared["preview_fingerprint"],
            expected_current_hash=prepared["expected_current_hash"],
        )

    assert (repo / ".cortex" / "blueprint.md").exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM project_blueprint_revisions WHERE project_id=?",
        (project_id,),
    ).fetchone()[0] == 0
    with pytest.raises(ValueError, match="owner blueprint already exists"):
        project_blueprints.preview(
            conn, project_id, markdown=prepared["markdown"], phases=prepared["phases"]
        )


def test_active_phase_blocks_project_removal_and_blueprint_rows_are_counted(conn, tmp_path):
    repo = tmp_path / "protected"
    repo.mkdir()
    project_id = store.create_project(conn, name="Protected", repo_path=str(repo))
    preview = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
    )
    project_blueprints.approve(
        conn,
        project_id,
        markdown=preview["markdown"],
        phases=preview["phases"],
        plan_basis=preview["plan_basis"],
        preview_fingerprint=preview["preview_fingerprint"],
        expected_current_hash=None,
    )

    result = project_removal.preview(conn, project_id)

    assert result["blocked"] is True
    assert result["deleted_counts"]["project_blueprint_revisions"] == 1
    assert result["deleted_counts"]["project_phases"] == 2
    assert "project phase" in result["blockers"][0]


def test_phase_quality_summary_tracks_stale_phases_and_recurring_evidence_gaps(
    conn, tmp_path
):
    repo = tmp_path / "phase-quality"
    repo.mkdir()
    project_id = store.create_project(conn, name="Phase quality", repo_path=str(repo))

    prepared = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=[
            {
                "name": "Foundation",
                "status": "active",
                "outcome": "The phase quality contract is visible.",
                "entry_criteria": ["Project is prepared"],
                "exit_criteria": ["Gate criterion is accepted"],
            },
            {
                "name": "Execution",
                "status": "planned",
                "outcome": "Execution starts after baseline.",
                "entry_criteria": ["Foundation is ready"],
                "exit_criteria": ["Gate criterion is accepted"],
            },
        ],
    )
    project_blueprints.approve(
        conn,
        project_id,
        markdown=prepared["markdown"],
        phases=prepared["phases"],
        plan_basis=prepared["plan_basis"],
        preview_fingerprint=prepared["preview_fingerprint"],
        expected_current_hash=None,
    )
    (repo / ".cortex" / "blueprint.md").write_text(
        blueprint_markdown("Externally edited"), encoding="utf-8"
    )

    projection = project_blueprints.projection(conn, project_id)
    quality = projection["phase_quality"]

    assert quality["coverage"]["accepted"] == 0
    assert quality["coverage"]["total"] == 2
    assert quality["coverage"]["percent"] == 0
    assert quality["stale_phase_count"] == 2
    assert len(quality["stale_phases"]) == 2
    recurring = quality["recurring_evidence_gaps"][0]
    assert recurring["criterion"] == "Gate criterion is accepted"
    assert recurring["occurrences"] == 2
    assert recurring["phase_count"] == 2


def test_current_phase_decomposition_is_exact_preview_first_and_task_conversion_keeps_links(
    conn, tmp_path
):
    repo = tmp_path / "decomposition"
    repo.mkdir()
    project_id = store.create_project(conn, name="Decomposition", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
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
    phase = blueprint["phases"][0]

    preview = project_blueprints.phase_decomposition_preview(conn, project_id)

    assert preview["phase"]["id"] == phase["id"]
    assert preview["writes"] == {
        "create_suggestions": 1,
        "create_tasks": False,
        "start_tasks": False,
        "contact_provider": False,
    }
    assert preview["suggestions"][0]["exit_criterion_ref"] == "exit-1"
    assert store.list_suggestions(conn, project_id) == []
    assert store.list_tasks(conn, project_id) == []
    with pytest.raises(ValueError, match="reviewed preview"):
        project_blueprints.approve_phase_decomposition(
            conn, project_id, preview_fingerprint="wrong"
        )

    approved = project_blueprints.approve_phase_decomposition(
        conn, project_id, preview_fingerprint=preview["preview_fingerprint"]
    )

    assert approved["created_tasks"] is False
    assert len(approved["suggestion_ids"]) == 1
    suggestion = store.get_suggestion(conn, approved["suggestion_ids"][0])
    assert suggestion["blueprint_revision_id"] == blueprint["revision"]["id"]
    assert suggestion["phase_id"] == phase["id"]
    assert suggestion["exit_criterion_ref"] == "exit-1"
    assert project_blueprints.phase_decomposition_preview(conn, project_id)["suggestions"] == []

    task_id = store.convert_suggestion(conn, suggestion["id"])
    task = store.get_task(conn, task_id)
    assert task["status"] == "assigned"
    assert task["phase_id"] == phase["id"]
    assert task["exit_criterion_ref"] == "exit-1"
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0


def test_phase_decomposition_refuses_stale_blueprint(conn, tmp_path):
    repo = tmp_path / "stale-decomposition"
    repo.mkdir()
    project_id = store.create_project(conn, name="Stale decomposition", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
    )
    project_blueprints.approve(
        conn,
        project_id,
        markdown=prepared["markdown"],
        phases=prepared["phases"],
        plan_basis=prepared["plan_basis"],
        preview_fingerprint=prepared["preview_fingerprint"],
        expected_current_hash=None,
    )
    (repo / ".cortex" / "blueprint.md").write_text(
        blueprint_markdown("Externally changed"), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="approved, non-stale"):
        project_blueprints.phase_decomposition_preview(conn, project_id)


def test_phase_progress_counts_only_owner_accepted_criterion_evidence(conn, tmp_path):
    repo = tmp_path / "criterion-evidence"
    repo.mkdir()
    project_id = store.create_project(conn, name="Criterion evidence", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
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
    phase_id = blueprint["current_phase_id"]

    blocked = project_blueprints.criterion_evidence_preview(conn, project_id, "exit-1")
    assert blocked["can_accept"] is False
    assert blocked["blockers"] == ["No task is linked to this exit criterion."]

    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Verify the blueprint hash",
        acceptance="Blueprint hash and revision are verified",
        phase_id=phase_id,
        exit_criterion_ref="exit-1",
        progress=100,
    )
    store.update_task(conn, task_id, status="review")
    run_id = store.create_run(
        conn,
        task_id=task_id,
        project_id=project_id,
        model="codex:default",
        execution_mode="headless_cli",
    )
    store.update_run(
        conn,
        run_id,
        ended_at="2026-08-17T01:00:00Z",
        exit_code=0,
        tests_passed=1,
        outcome="survived",
        git_after="abc123",
    )

    before_acceptance = project_blueprints.projection(conn, project_id)
    assert before_acceptance["phases"][0]["progress"]["percent"] == 0
    evidence_preview = project_blueprints.criterion_evidence_preview(
        conn, project_id, "exit-1"
    )
    assert evidence_preview["can_accept"] is True
    assert [item["kind"] for item in evidence_preview["evidence"]] == ["task", "run"]
    with pytest.raises(ValueError, match="reviewed preview"):
        project_blueprints.approve_criterion_evidence(
            conn,
            project_id,
            criterion_ref="exit-1",
            preview_fingerprint="wrong",
        )

    accepted = project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )

    assert accepted["created"] is True
    repeated = project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )
    assert repeated["created"] is False
    projection = project_blueprints.projection(conn, project_id)
    progress = projection["phases"][0]["progress"]
    assert progress["basis"] == "owner-accepted exit criteria"
    assert progress["accepted"] == 1
    assert progress["total"] == 1
    assert progress["percent"] == 100
    assert progress["criteria"][0]["evidence_id"] == accepted["acceptance_id"]


def test_active_phase_moves_to_review_only_after_exact_accepted_evidence(conn, tmp_path):
    repo = tmp_path / "phase-review"
    repo.mkdir()
    project_id = store.create_project(conn, name="Phase review", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
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
    phase_id = blueprint["current_phase_id"]

    blocked = project_blueprints.phase_review_preview(conn, project_id)
    assert blocked["can_transition"] is False
    assert blocked["writes"]["complete_phase"] is False
    with pytest.raises(ValueError, match="every criterion"):
        project_blueprints.approve_phase_review(
            conn, project_id, preview_fingerprint=blocked["preview_fingerprint"]
        )

    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Present verified evidence",
        phase_id=phase_id,
        exit_criterion_ref="exit-1",
    )
    store.update_task(conn, task_id, status="review")
    evidence_preview = project_blueprints.criterion_evidence_preview(
        conn, project_id, "exit-1"
    )
    project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )
    review_preview = project_blueprints.phase_review_preview(conn, project_id)
    assert review_preview["can_transition"] is True
    assert review_preview["writes"] == {
        "update_phase_status": True,
        "complete_phase": False,
    }
    with pytest.raises(ValueError, match="reviewed preview"):
        project_blueprints.approve_phase_review(
            conn, project_id, preview_fingerprint="wrong"
        )

    reviewed = project_blueprints.approve_phase_review(
        conn, project_id, preview_fingerprint=review_preview["preview_fingerprint"]
    )

    assert reviewed["phases"][0]["status"] == "review"
    assert reviewed["phases"][0]["completed_at"] is None
    assert reviewed["current_phase_id"] == phase_id
    assert "phase.review_requested" in {
        event["action"] for event in store.list_activity_events(conn, project_id)
    }
    with pytest.raises(ValueError, match="not active"):
        project_blueprints.phase_decomposition_preview(conn, project_id)


def test_phase_dependencies_are_authorized_and_projected_in_phase_rows(conn, tmp_path):
    repo = tmp_path / "phase-deps"
    repo.mkdir()
    project_id = store.create_project(conn, name="Phase dependencies", repo_path=str(repo))

    prepared = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=[
            {
                "name": "Foundation",
                "status": "active",
                "outcome": "The first phase is active.",
                "entry_criteria": ["Project is registered"],
                "exit_criteria": ["Foundation is accepted"],
            },
            {
                "name": "Execution",
                "status": "planned",
                "outcome": "Execution starts after foundation.",
                "entry_criteria": ["Foundation complete"],
                "exit_criteria": ["Execution completed"],
                "depends_on_ordinals": [1],
            },
            {
                "name": "Finish",
                "status": "planned",
                "outcome": "Delivery completes the work.",
                "entry_criteria": ["Execution complete"],
                "exit_criteria": ["Finish completed"],
                "depends_on_ordinals": [2],
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
        expected_current_hash=None,
    )

    dependency_count = conn.execute(
        "SELECT COUNT(*) FROM phase_dependencies WHERE phase_id=?", (blueprint["phases"][1]["id"],)
    ).fetchone()[0]
    assert dependency_count == 1

    projection = project_blueprints.projection(conn, project_id)
    execution = next(phase for phase in projection["phases"] if phase["name"] == "Execution")
    finish = next(phase for phase in projection["phases"] if phase["name"] == "Finish")
    assert execution["depends_on"][0]["depends_on_ordinal"] == 1
    assert execution["depends_on"][0]["depends_on_name"] == "Foundation"
    assert finish["depends_on"][0]["depends_on_phase_id"] == blueprint["phases"][1]["id"]


def test_phase_dependencies_can_be_updated_after_approval(conn, tmp_path):
    repo = tmp_path / "phase-dependency-update"
    repo.mkdir()
    project_id = store.create_project(conn, name="Updated phase dependencies", repo_path=str(repo))

    prepared = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=[
            {
                "name": "Foundation",
                "status": "active",
                "outcome": "The first phase is active.",
                "entry_criteria": ["Project is registered"],
                "exit_criteria": ["Foundation is accepted"],
            },
            {
                "name": "Execution",
                "status": "planned",
                "outcome": "Execution starts after foundation.",
                "entry_criteria": ["Foundation complete"],
                "exit_criteria": ["Execution completed"],
                "depends_on_ordinals": [1],
            },
            {
                "name": "Delivery",
                "status": "planned",
                "outcome": "Delivery follows execution.",
                "entry_criteria": ["Execution complete"],
                "exit_criteria": ["Delivery completed"],
                "depends_on_ordinals": [1, 2],
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
        expected_current_hash=None,
    )

    updated = project_blueprints.update_phase_dependencies(
        conn, project_id, blueprint["phases"][2]["id"], depends_on_ordinals=[1]
    )

    delivery = next(phase for phase in updated["phases"] if phase["name"] == "Delivery")
    assert [dep["depends_on_ordinal"] for dep in delivery["depends_on"]] == [1]
    assert any(
        event["action"] == "phase.dependencies_updated"
        for event in store.list_activity_events(conn, project_id)
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM phase_dependencies WHERE phase_id=?",
        (blueprint["phases"][2]["id"],),
    ).fetchone()[0] == 1


def test_phase_dependency_authoring_rejects_invalid_dependency_ordinals(conn, tmp_path):
    repo = tmp_path / "phase-dependency-invalid"
    repo.mkdir()
    project_id = store.create_project(conn, name="Invalid phase dependency", repo_path=str(repo))

    prepared = project_blueprints.preview(
        conn, project_id, markdown=blueprint_markdown(), phases=phases()
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

    with pytest.raises(ValueError, match="earlier phases"):
        project_blueprints.update_phase_dependencies(
            conn, project_id, blueprint["phases"][0]["id"], depends_on_ordinals=[1]
        )


def test_reviewed_phase_moves_to_complete_and_activates_next_planned_phase(conn, tmp_path):
    repo = tmp_path / "phase-complete"
    repo.mkdir()
    project_id = store.create_project(conn, name="Phase complete", repo_path=str(repo))

    prepared = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=[
            {
                "name": "Foundation",
                "status": "active",
                "outcome": "The foundation phase is verified.",
                "entry_criteria": ["Project is available"],
                "exit_criteria": ["Foundation is verified"],
            },
            {
                "name": "Execution",
                "status": "planned",
                "outcome": "Execution begins from baseline.",
                "entry_criteria": ["Foundation complete"],
                "exit_criteria": ["Execution is ready"],
                "depends_on_ordinals": [1],
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
        expected_current_hash=None,
    )
    phase1_id = blueprint["phases"][0]["id"]
    phase2_id = blueprint["phases"][1]["id"]

    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Verify foundation",
        phase_id=phase1_id,
        exit_criterion_ref="exit-1",
    )
    store.update_task(conn, task_id, status="review")
    evidence_preview = project_blueprints.criterion_evidence_preview(
        conn, project_id, "exit-1"
    )
    project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )
    review_preview = project_blueprints.phase_review_preview(conn, project_id)
    assert review_preview["can_transition"] is True
    project_blueprints.approve_phase_review(
        conn, project_id, preview_fingerprint=review_preview["preview_fingerprint"]
    )

    completion_preview = project_blueprints.phase_completion_preview(conn, project_id)
    assert completion_preview["next_phase"]["id"] == phase2_id
    assert completion_preview["writes"]["activate_next_phase"] is True
    completed = project_blueprints.approve_phase_completion(
        conn,
        project_id,
        preview_fingerprint=completion_preview["preview_fingerprint"],
    )
    assert completed["phases"][0]["status"] == "complete"
    assert completed["phases"][0]["completed_at"] is not None
    assert completed["phases"][1]["status"] == "active"
    assert completed["current_phase_id"] == phase2_id
    assert "phase.completed" in {
        event["action"] for event in store.list_activity_events(conn, project_id)
    }


def test_phase_completion_preview_blocks_activation_when_next_phase_dependencies_are_incomplete(
    conn, tmp_path
):
    repo = tmp_path / "blocked-completion"
    repo.mkdir()
    project_id = store.create_project(conn, name="Blocked phase completion", repo_path=str(repo))
    prepared = project_blueprints.preview(
        conn,
        project_id,
        markdown=blueprint_markdown(),
        phases=[
            {
                "name": "Foundation",
                "status": "active",
                "outcome": "The foundation phase is verified.",
                "entry_criteria": ["Project is available"],
                "exit_criteria": ["Foundation is verified"],
            },
            {
                "name": "Execution",
                "status": "planned",
                "outcome": "Execution begins from baseline.",
                "entry_criteria": ["Foundation complete"],
                "exit_criteria": ["Execution is ready"],
                "depends_on_ordinals": [1],
            },
            {
                "name": "Delivery",
                "status": "planned",
                "outcome": "Delivery depends on foundation and execution.",
                "entry_criteria": ["Execution complete"],
                "exit_criteria": ["Delivery is ready"],
                "depends_on_ordinals": [1, 2],
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
        expected_current_hash=None,
    )

    # Move phase 1 to review and complete it to activate phase 2.
    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Verify foundation",
        phase_id=blueprint["phases"][0]["id"],
        exit_criterion_ref="exit-1",
    )
    store.update_task(conn, task_id, status="review")
    evidence_preview = project_blueprints.criterion_evidence_preview(
        conn, project_id, "exit-1"
    )
    project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )
    review_preview = project_blueprints.phase_review_preview(conn, project_id)
    project_blueprints.approve_phase_review(
        conn, project_id, preview_fingerprint=review_preview["preview_fingerprint"]
    )
    completion_preview = project_blueprints.phase_completion_preview(conn, project_id)
    project_blueprints.approve_phase_completion(
        conn,
        project_id,
        preview_fingerprint=completion_preview["preview_fingerprint"],
    )

    # Simulate an incomplete dependency by rolling back phase 1 state.
    conn.execute(
        "UPDATE project_phases SET status='active', completed_at=NULL WHERE id=?",
        (blueprint["phases"][0]["id"],),
    )
    conn.commit()

    # Move phase 2 to review.
    task_id = store.create_task(
        conn,
        project_id=project_id,
        title="Verify execution",
        phase_id=blueprint["phases"][1]["id"],
        exit_criterion_ref="exit-1",
    )
    store.update_task(conn, task_id, status="review")
    evidence_preview = project_blueprints.criterion_evidence_preview(
        conn, project_id, "exit-1"
    )
    project_blueprints.approve_criterion_evidence(
        conn,
        project_id,
        criterion_ref="exit-1",
        preview_fingerprint=evidence_preview["preview_fingerprint"],
    )
    review_preview = project_blueprints.phase_review_preview(conn, project_id)
    project_blueprints.approve_phase_review(
        conn, project_id, preview_fingerprint=review_preview["preview_fingerprint"]
    )

    completion_preview = project_blueprints.phase_completion_preview(conn, project_id)
    assert completion_preview["next_phase"]["name"] == "Delivery"
    assert completion_preview["can_transition"] is False
    assert any("not complete" in blocker for blocker in completion_preview["blockers"])
    with pytest.raises(ValueError, match="blockers are addressed"):
        project_blueprints.approve_phase_completion(
            conn, project_id, preview_fingerprint=completion_preview["preview_fingerprint"]
        )
