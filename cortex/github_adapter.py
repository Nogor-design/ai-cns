"""Guarded one-task application of approved GitHub Project mirror plans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from . import github_projects, github_reader, store


ADD_ITEM_MUTATION = r"""
mutation CortexAddProjectItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

SET_FIELD_MUTATION = r"""
mutation CortexSetProjectField(
  $project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: $value
  }) { projectV2Item { id } }
}
"""

CLEAR_FIELD_MUTATION = r"""
mutation CortexClearProjectField($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field
  }) { projectV2Item { id } }
}
"""

LOCAL_ACTIONS = {"update_cortex_link", "update_cortex_sync_state"}
REMOTE_ACTIONS = {"add_project_item", "set_project_field", "clear_project_field"}
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,79}$")


class MirrorApplyError(RuntimeError):
    pass


class MirrorApplyRefused(MirrorApplyError):
    pass


def suggested_operation_id(fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"ghm-{digest}"


def prepare(
    task: Mapping[str, Any],
    owner: str,
    number: int,
    *,
    transport: github_reader.GitHubCLITransport | Any | None = None,
) -> tuple[github_projects.MirrorPlan, dict[str, Any]]:
    snapshot = github_reader.read_project(owner, number, transport=transport)
    plan = github_projects.build_mirror_plan(task, snapshot, require_schema=True)
    return plan, snapshot


def apply(
    conn: sqlite3.Connection,
    task_id: str,
    owner: str,
    number: int,
    *,
    approved_fingerprint: str,
    operation_id: str,
    actor: str = "codex",
    transport: github_reader.GitHubCLITransport | Any | None = None,
) -> dict[str, Any]:
    """Apply or resume one approved plan and verify it before local commit."""
    if not _OPERATION_ID.fullmatch(operation_id):
        raise MirrorApplyRefused(
            "operation id must be 6-80 safe characters beginning with a letter or digit"
        )
    api = transport or github_reader.GitHubCLITransport()
    task = store.get_task(conn, task_id)
    project = store.get_project(conn, task["project_id"])

    try:
        existing = store.get_github_mirror_operation(conn, operation_id)
    except store.NotFound:
        existing = None
    if existing is not None:
        if existing["task_id"] != task_id:
            raise MirrorApplyRefused("operation id belongs to a different Cortex task")
        if existing["plan_fingerprint"] != approved_fingerprint:
            raise MirrorApplyRefused("approved fingerprint does not match the reserved operation")
        if existing["status"] == "verified":
            return {
                "status": "already_verified",
                "operation_id": existing["operation_id"],
                "plan_fingerprint": existing["plan_fingerprint"],
                "remote_writes": 0,
            }
        if existing["status"] in {"refused", "failed"}:
            raise MirrorApplyRefused(
                f"operation is terminal with status {existing['status']}; inspect its evidence"
            )
        bound_actions = _load_json(existing["actions_json"], "operation actions")
        completed = _load_json(
            existing["completed_actions_json"], "completed operation actions"
        )
        store.update_github_mirror_operation(
            conn,
            existing["operation_id"],
            status="applying",
            evidence={"resumed": True, "previous_status": existing["status"]},
        )
        operation_id = existing["operation_id"]
        try:
            snapshot = github_reader.read_project(owner, number, transport=api)
            _validate_reserved_target(existing, task, snapshot)
        except Exception as exc:
            _finish_operation(
                conn,
                operation_id,
                task,
                actor,
                status="interrupted",
                summary=f"Interrupted GitHub mirror recovery {operation_id}: {exc}",
                evidence={"remote_writes": 0, "recoverable": True},
                error=str(exc),
            )
            if isinstance(exc, MirrorApplyError):
                raise
            raise MirrorApplyError(str(exc)) from exc
    else:
        plan, snapshot = prepare(task, owner, number, transport=api)
        if not plan.safe_to_apply:
            raise MirrorApplyRefused(
                "plan has conflicts: "
                + ", ".join(conflict.code for conflict in plan.conflicts)
            )
        if plan.fingerprint != approved_fingerprint:
            raise MirrorApplyRefused(
                "approved fingerprint is stale; run the dry-run plan again"
            )
        if not plan.actions:
            return {
                "status": "already_converged",
                "operation_id": None,
                "plan_fingerprint": plan.fingerprint,
                "remote_writes": 0,
            }
        _validate_configured_project(project, snapshot)
        _preflight_local_actions(task, plan.actions)
        bound_actions = _bind_actions(plan.actions, snapshot)
        existing, created = store.claim_github_mirror_operation(
            conn,
            operation_id=operation_id,
            task_id=task_id,
            project_id=task["project_id"],
            plan_fingerprint=plan.fingerprint,
            github_project_id=plan.project_id or "",
            github_issue_id=plan.issue_id,
            github_project_item_id=plan.project_item_id,
            actor=actor,
            session_id=task["pm_session_id"],
            actions=bound_actions,
        )
        if not created:
            operation_id = existing["operation_id"]
            if existing["status"] == "verified":
                return {
                    "status": "already_verified",
                    "operation_id": operation_id,
                    "plan_fingerprint": existing["plan_fingerprint"],
                    "remote_writes": 0,
                }
            if existing["status"] in {"refused", "failed"}:
                raise MirrorApplyRefused(
                    f"approved plan was already claimed by terminal operation {operation_id}"
                )
            bound_actions = _load_json(existing["actions_json"], "operation actions")
            completed = _load_json(
                existing["completed_actions_json"], "completed operation actions"
            )
            store.update_github_mirror_operation(
                conn,
                operation_id,
                status="applying",
                evidence={"resumed": True, "previous_status": existing["status"]},
            )
        else:
            completed = []

    remote_writes = 0
    try:
        for index, action in enumerate(bound_actions):
            if action["kind"] not in REMOTE_ACTIONS:
                continue
            # Re-read immediately before every mutation. The approved action's
            # bound IDs and expected value are checked against this fresh page.
            snapshot = github_reader.read_project(owner, number, transport=api)
            snapshot, wrote = _apply_remote_action(
                api, owner, number, snapshot, action
            )
            remote_writes += int(wrote)
            if not any(record.get("index") == index for record in completed):
                completed.append({
                    "index": index,
                    "kind": action["kind"],
                    "field": action.get("field"),
                    "verified_value": action.get("value"),
                })
            item = _find_issue_item(snapshot, task["github_issue_id"])
            store.update_github_mirror_operation(
                conn,
                operation_id,
                completed_actions=completed,
                github_project_item_id=item.get("id") if item else None,
                evidence={"last_verified_action": index, "remote_writes": remote_writes},
            )

        final_snapshot = github_reader.read_project(owner, number, transport=api)
        final_item = _find_issue_item(final_snapshot, task["github_issue_id"])
        if any(action["kind"] == "add_project_item" for action in bound_actions):
            if final_item is None:
                raise MirrorApplyError("added issue was not present in the verification read")
            _finish_operation(
                conn,
                operation_id,
                task,
                actor,
                status="verified",
                summary="Verified the approved GitHub Project item-add operation",
                evidence={
                    "project_item_id": final_item["id"],
                    "remote_writes": remote_writes,
                    "requires_replan": True,
                },
                project_item_id=final_item["id"],
            )
            return {
                "status": "verified_replan_required",
                "operation_id": operation_id,
                "plan_fingerprint": approved_fingerprint,
                "project_item_id": final_item["id"],
                "remote_writes": remote_writes,
            }

        if final_item is None:
            raise MirrorApplyError("linked issue disappeared before verification")
        desired = github_projects.desired_fields(task)
        mismatches = [
            name for name, value in desired.items()
            if not github_projects.field_values_equal(
                name, (final_item.get("fields") or {}).get(name), value
            )
        ]
        if mismatches:
            raise MirrorApplyError(
                "verification read disagrees on fields: " + ", ".join(mismatches)
            )
        local_fields = {
            action["field"]: action.get("value")
            for action in bound_actions
            if action["kind"] in LOCAL_ACTIONS
        }
        conn.execute("BEGIN IMMEDIATE")
        current_task = store.get_task(conn, task_id)
        _preflight_bound_local_actions(current_task, bound_actions)
        if local_fields:
            store.update_task(
                conn,
                task_id,
                actor_type="agent" if actor != "owner" else "human",
                actor_name=actor,
                source="github-mirror",
                commit=False,
                **local_fields,
            )
        store.update_github_mirror_operation(
            conn,
            operation_id,
            status="verified",
            completed_actions=completed,
            github_project_item_id=final_item["id"],
            evidence={
                "verified_fields": sorted(desired),
                "remote_writes": remote_writes,
            },
            commit=False,
        )
        store.create_activity_event(
            conn,
            project_id=task["project_id"],
            task_id=task_id,
            actor_type="agent" if actor != "owner" else "human",
            actor_name=actor,
            action="github.mirror_applied",
            summary=f"Verified approved GitHub mirror operation {operation_id}",
            source="github-mirror",
            source_ref=operation_id,
            session_id=task["pm_session_id"],
            evidence={
                "plan_fingerprint": approved_fingerprint,
                "project_item_id": final_item["id"],
                "remote_writes": remote_writes,
            },
            commit=False,
        )
        conn.commit()
        return {
            "status": "verified",
            "operation_id": operation_id,
            "plan_fingerprint": approved_fingerprint,
            "project_item_id": final_item["id"],
            "remote_writes": remote_writes,
        }
    except MirrorApplyRefused as exc:
        conn.rollback()
        outcome = "interrupted" if remote_writes else "refused"
        _finish_operation(
            conn, operation_id, task, actor, status=outcome,
            summary=f"Refused GitHub mirror operation {operation_id}: {exc}",
            evidence={
                "remote_writes": remote_writes,
                "recoverable": bool(remote_writes),
            },
            error=str(exc),
        )
        raise
    except Exception as exc:
        conn.rollback()
        _finish_operation(
            conn, operation_id, task, actor, status="interrupted",
            summary=f"Interrupted GitHub mirror operation {operation_id}: {exc}",
            evidence={"remote_writes": remote_writes, "recoverable": True},
            error=str(exc),
        )
        if isinstance(exc, MirrorApplyError):
            raise
        raise MirrorApplyError(str(exc)) from exc


def _bind_actions(actions: Any, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    schema = dict(snapshot.get("field_schema") or {})
    bound: list[dict[str, Any]] = []
    for action in actions:
        record = {
            "kind": action.kind,
            "target_id": action.target_id,
            "field": action.field,
            "value": action.value,
            "expected": action.expected,
            "reason": action.reason,
        }
        if action.kind in {"set_project_field", "clear_project_field"}:
            descriptor = dict(schema[action.field])
            record["field_id"] = descriptor["id"]
            record["field_kind"] = descriptor["kind"]
            if descriptor["kind"] == "single_select" and action.value is not None:
                record["option_id"] = dict(descriptor.get("options") or {})[action.value]
        bound.append(record)
    return bound


def _apply_remote_action(api, owner, number, snapshot, action):
    project_id = snapshot.get("project_id")
    if action["kind"] == "add_project_item":
        content_id = action["value"]["content_id"]
        if _find_issue_item(snapshot, content_id):
            return snapshot, False
        api.graphql(
            ADD_ITEM_MUTATION, {"project": project_id, "content": content_id}
        )
        refreshed = github_reader.read_project(owner, number, transport=api)
        if not _find_issue_item(refreshed, content_id):
            raise MirrorApplyError("GitHub did not return the added issue in the verification read")
        return refreshed, True

    item = next(
        (candidate for candidate in snapshot.get("items") or []
         if candidate.get("id") == action["target_id"]),
        None,
    )
    if item is None:
        raise MirrorApplyRefused("target Project item is absent")
    descriptor = dict((snapshot.get("field_schema") or {}).get(action["field"]) or {})
    if descriptor.get("id") != action.get("field_id"):
        raise MirrorApplyRefused(f"field identity changed: {action['field']}")
    if action.get("option_id"):
        observed_option = dict(descriptor.get("options") or {}).get(action.get("value"))
        if observed_option != action["option_id"]:
            raise MirrorApplyRefused(f"select option identity changed: {action['field']}")
    current = dict(item.get("fields") or {}).get(action["field"])
    if github_projects.field_values_equal(action["field"], current, action.get("value")):
        return snapshot, False
    if not github_projects.field_values_equal(
        action["field"], current, action.get("expected")
    ):
        raise MirrorApplyRefused(
            f"precondition changed for {action['field']}: expected {action.get('expected')!r}, "
            f"observed {current!r}"
        )
    variables = {
        "project": project_id,
        "item": action["target_id"],
        "field": action["field_id"],
    }
    if action["kind"] == "clear_project_field":
        api.graphql(CLEAR_FIELD_MUTATION, variables)
    else:
        variables["value"] = _mutation_value(action)
        api.graphql(SET_FIELD_MUTATION, variables)
    refreshed = github_reader.read_project(owner, number, transport=api)
    refreshed_item = next(
        (candidate for candidate in refreshed.get("items") or []
         if candidate.get("id") == action["target_id"]),
        None,
    )
    observed = (
        dict(refreshed_item.get("fields") or {}).get(action["field"])
        if refreshed_item else None
    )
    if not github_projects.field_values_equal(
        action["field"], observed, action.get("value")
    ):
        raise MirrorApplyError(f"post-write verification failed for {action['field']}")
    return refreshed, True


def _mutation_value(action: Mapping[str, Any]) -> dict[str, Any]:
    kind = action.get("field_kind")
    if kind == "text":
        return {"text": str(action.get("value"))}
    if kind == "number":
        return {"number": float(action.get("value"))}
    if kind == "date":
        return {"date": str(action.get("value"))}
    if kind == "single_select":
        return {"singleSelectOptionId": action["option_id"]}
    raise MirrorApplyRefused(f"unsupported Project field kind: {kind}")


def _find_issue_item(snapshot: Mapping[str, Any], issue_id: Any):
    return next(
        (item for item in snapshot.get("items") or []
         if item.get("content_id") == issue_id),
        None,
    )


def _validate_configured_project(project, snapshot):
    configured = project["github_project_id"]
    if configured and configured != snapshot.get("project_id"):
        raise MirrorApplyRefused("configured GitHub Project node ID changed")


def _validate_reserved_target(operation, task, snapshot):
    if operation["github_project_id"] != snapshot.get("project_id"):
        raise MirrorApplyRefused("reserved GitHub Project identity changed")
    if operation["github_issue_id"] != task["github_issue_id"]:
        raise MirrorApplyRefused("linked GitHub issue identity changed")


def _preflight_local_actions(task, actions):
    for action in actions:
        if action.kind in LOCAL_ACTIONS and not github_projects.field_values_equal(
            action.field or "", task[action.field], action.expected
        ):
            raise MirrorApplyRefused(f"local precondition changed for {action.field}")


def _preflight_bound_local_actions(task, actions):
    for action in actions:
        if action["kind"] in LOCAL_ACTIONS and not github_projects.field_values_equal(
            action["field"], task[action["field"]], action.get("expected")
        ):
            raise MirrorApplyRefused(f"local precondition changed for {action['field']}")


def _finish_operation(
    conn,
    operation_id,
    task,
    actor,
    *,
    status,
    summary,
    evidence,
    error=None,
    project_item_id=None,
):
    store.update_github_mirror_operation(
        conn,
        operation_id,
        status=status,
        github_project_item_id=project_item_id,
        evidence=evidence,
        error=error,
        commit=False,
    )
    store.create_activity_event(
        conn,
        project_id=task["project_id"],
        task_id=task["id"],
        actor_type="agent" if actor != "owner" else "human",
        actor_name=actor,
        action=f"github.mirror_{status}",
        summary=summary,
        source="github-mirror",
        source_ref=operation_id,
        session_id=task["pm_session_id"],
        evidence=evidence,
        commit=False,
    )
    conn.commit()


def _load_json(value: Any, label: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise MirrorApplyRefused(f"stored {label} are invalid JSON") from exc
    if not isinstance(parsed, list):
        raise MirrorApplyRefused(f"stored {label} must be a list")
    return [dict(item) for item in parsed]
