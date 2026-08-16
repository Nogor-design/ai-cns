"""Offline GitHub Projects mirror planning."""

from __future__ import annotations

from copy import deepcopy

from cortex import github_projects


def task(**overrides):
    values = {
        "id": "task-123",
        "project_id": "cortex-control-plane",
        "status": "in_progress",
        "priority": 1,
        "progress": 40,
        "start_at": "2026-08-15T09:00:00Z",
        "target_at": "2026-08-22",
        "due_at": None,
        "risk": "high",
        "assignee": "codex",
        "milestone": "Mirror v1",
        "next_action": "Review the dry run",
        "blocked_reason": None,
        "github_issue_id": "I_issue_1",
        "github_project_item_id": None,
        "sync_state": None,
    }
    values.update(overrides)
    return values


def snapshot(*items):
    return {
        "project_id": "PVT_project_1",
        "items_complete": True,
        "items": list(items),
    }


def project_item(*, item_id="PVTI_item_1", content_id="I_issue_1", fields=None):
    return {"id": item_id, "content_id": content_id, "fields": fields or {}}


def field_schema():
    options = {
        "Status": {value: f"status-{index}" for index, value in enumerate(
            ("Todo", "In progress", "Review", "Blocked", "Done"), start=1
        )},
        "Priority": {f"P{index}": f"priority-{index}" for index in range(1, 6)},
        "Risk": {value: f"risk-{index}" for index, value in enumerate(
            ("Auto", "Low", "Medium", "High"), start=1
        )},
    }
    return {
        field["name"]: {
            "id": f"field-{index}",
            "kind": field["kind"],
            "options": options.get(field["name"], {}),
        }
        for index, field in enumerate(github_projects.FIELD_OWNERSHIP, start=1)
    }


def strict_snapshot(*items):
    value = snapshot(*items)
    value["field_schema_complete"] = True
    value["field_schema"] = field_schema()
    for item in value["items"]:
        item["fields_complete"] = True
    return value


def apply_plan(task_record, project_snapshot, plan):
    next_task = deepcopy(task_record)
    next_snapshot = deepcopy(project_snapshot)
    item = next(
        candidate for candidate in next_snapshot["items"]
        if candidate["id"] == plan.project_item_id
    )
    for action in plan.actions:
        if action.kind == "update_cortex_link":
            next_task[action.field] = action.value
        elif action.kind == "update_cortex_sync_state":
            next_task[action.field] = action.value
        elif action.kind == "set_project_field":
            item["fields"][action.field] = action.value
        elif action.kind == "clear_project_field":
            item["fields"].pop(action.field, None)
    return next_task, next_snapshot


def test_linked_issue_converges_without_a_second_write():
    task_record = task()
    project_snapshot = snapshot(project_item(fields={"Status": "Todo"}))

    first = github_projects.build_mirror_plan(task_record, project_snapshot)

    assert first.safe_to_apply is True
    assert first.project_item_id == "PVTI_item_1"
    assert any(action.kind == "update_cortex_link" for action in first.actions)
    assert any(
        action.kind == "set_project_field"
        and action.field == "Status"
        and action.value == "In progress"
        for action in first.actions
    )

    linked_task, applied_snapshot = apply_plan(task_record, project_snapshot, first)
    second = github_projects.build_mirror_plan(linked_task, applied_snapshot)
    assert second.safe_to_apply is True
    assert second.actions == ()
    assert second.conflicts == ()


def test_missing_project_item_plans_link_only_then_requires_reread():
    plan = github_projects.build_mirror_plan(task(), snapshot())
    assert plan.safe_to_apply is True
    assert len(plan.actions) == 1
    assert plan.actions[0].kind == "add_project_item"
    assert plan.actions[0].value == {"content_id": "I_issue_1"}


def test_incomplete_paginated_read_never_proposes_an_add():
    project_snapshot = snapshot()
    project_snapshot["items_complete"] = False
    plan = github_projects.build_mirror_plan(task(), project_snapshot)
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "incomplete_project_snapshot"


def test_strict_plan_requires_complete_field_schema_and_item_values():
    project_snapshot = snapshot(project_item())
    plan = github_projects.build_mirror_plan(
        task(), project_snapshot, require_schema=True
    )
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "incomplete_project_field_schema"

    project_snapshot["field_schema_complete"] = True
    project_snapshot["field_schema"] = field_schema()
    plan = github_projects.build_mirror_plan(
        task(), project_snapshot, require_schema=True
    )
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "incomplete_item_fields"


def test_strict_plan_refuses_missing_fields_and_select_options():
    project_snapshot = strict_snapshot(project_item())
    project_snapshot["field_schema"].pop("Worker")
    plan = github_projects.build_mirror_plan(
        task(), project_snapshot, require_schema=True
    )
    assert any(conflict.code == "unknown_project_field" for conflict in plan.conflicts)
    assert plan.actions == ()

    project_snapshot = strict_snapshot(project_item())
    project_snapshot["field_schema"]["Status"]["options"].pop("In progress")
    plan = github_projects.build_mirror_plan(
        task(), project_snapshot, require_schema=True
    )
    assert any(conflict.code == "unknown_select_option" for conflict in plan.conflicts)
    assert plan.actions == ()


def test_plan_fingerprint_binds_snapshot_identity_and_actions():
    add_plan = github_projects.build_mirror_plan(task(), strict_snapshot(), require_schema=True)
    update_snapshot = strict_snapshot(project_item(fields={"Status": "Todo"}))
    update_plan = github_projects.build_mirror_plan(
        task(), update_snapshot, require_schema=True
    )
    assert add_plan.fingerprint != update_plan.fingerprint
    assert add_plan.snapshot_digest != update_plan.snapshot_digest

    changed_snapshot = deepcopy(update_snapshot)
    changed_snapshot["field_schema"]["Worker"]["id"] = "field-worker-replaced"
    changed_plan = github_projects.build_mirror_plan(
        task(), changed_snapshot, require_schema=True
    )
    assert changed_plan.fingerprint != update_plan.fingerprint


def test_duplicate_issue_items_are_reported_and_never_mutated():
    plan = github_projects.build_mirror_plan(
        task(),
        snapshot(project_item(item_id="one"), project_item(item_id="two")),
    )
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "duplicate_issue_items"


def test_cortex_id_collision_is_reported():
    plan = github_projects.build_mirror_plan(
        task(),
        snapshot(project_item(content_id="I_other", fields={"Cortex ID": "task-123"})),
    )
    assert plan.safe_to_apply is False
    assert plan.conflicts[0].code == "cortex_id_collision"


def test_stale_stored_item_id_is_reported():
    plan = github_projects.build_mirror_plan(
        task(github_project_item_id="PVTI_stale"),
        snapshot(project_item(item_id="PVTI_observed")),
    )
    assert plan.safe_to_apply is False
    assert plan.conflicts[0].code == "stale_project_item_link"


def test_remote_edit_after_matching_sync_marker_is_a_conflict():
    task_record = task(github_project_item_id="PVTI_item_1")
    project_snapshot = snapshot(project_item())
    initial = github_projects.build_mirror_plan(task_record, project_snapshot)
    synced_task, synced_snapshot = apply_plan(task_record, project_snapshot, initial)
    synced_snapshot["items"][0]["fields"]["Status"] = "Done"
    plan = github_projects.build_mirror_plan(synced_task, synced_snapshot)
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "remote_edit_to_cortex_owned_field"


def test_remote_edit_still_conflicts_when_cortex_also_changed():
    task_record = task(github_project_item_id="PVTI_item_1")
    project_snapshot = snapshot(project_item())
    initial = github_projects.build_mirror_plan(task_record, project_snapshot)
    synced_task, synced_snapshot = apply_plan(task_record, project_snapshot, initial)
    synced_task["progress"] = 60
    synced_snapshot["items"][0]["fields"]["Status"] = "Done"
    plan = github_projects.build_mirror_plan(synced_task, synced_snapshot)
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "remote_edit_to_cortex_owned_field"


def test_missing_issue_node_id_never_matches_by_title():
    plan = github_projects.build_mirror_plan(
        task(github_issue_id=None),
        snapshot(project_item(fields={"Title": "A similar title"})),
    )
    assert plan.safe_to_apply is False
    assert plan.actions == ()
    assert plan.conflicts[0].code == "missing_issue_link"


def test_normalized_empty_and_numeric_values_do_not_loop():
    task_record = task(
        github_project_item_id="PVTI_item_1",
        assignee=None,
        milestone=None,
        next_action=None,
    )
    fields = github_projects.desired_fields(task_record)
    fields.update({"Progress": "40", "Worker": "", "Milestone": "", "Next action": ""})
    fields.pop("Cortex Sync")
    project_snapshot = snapshot(project_item(fields=fields))
    first = github_projects.build_mirror_plan(task_record, project_snapshot)
    synced_task, synced_snapshot = apply_plan(task_record, project_snapshot, first)
    second = github_projects.build_mirror_plan(synced_task, synced_snapshot)
    assert second.actions == ()
    assert second.conflicts == ()


def test_field_ownership_and_planner_fields_cannot_drift():
    owned = {field["name"] for field in github_projects.FIELD_OWNERSHIP}
    assert owned == set(github_projects.desired_fields(task()))
    assert "Cortex Milestone" in owned
    assert "Milestone" not in owned


def test_stored_item_with_different_content_is_reported_precisely():
    plan = github_projects.build_mirror_plan(
        task(github_project_item_id="PVTI_item_1"),
        snapshot(project_item(content_id="I_other")),
    )
    assert plan.safe_to_apply is False
    assert plan.conflicts[0].code == "stored_item_content_mismatch"


def test_invalid_task_value_becomes_a_conflict_instead_of_an_exception():
    plan = github_projects.build_mirror_plan(
        task(progress="not-a-number"),
        snapshot(project_item()),
    )
    assert plan.safe_to_apply is False
    assert plan.conflicts[0].code == "invalid_cortex_task_data"
