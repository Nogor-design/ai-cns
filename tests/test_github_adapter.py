"""Guarded GitHub mirror application, replay, recovery, and local atomicity."""

from __future__ import annotations

from typing import Any

import pytest

from cortex import db, github_adapter, github_projects, store


KIND_TO_DATA = {
    "text": "TEXT",
    "number": "NUMBER",
    "date": "DATE",
    "single_select": "SINGLE_SELECT",
}


class StatefulGitHub:
    def __init__(self):
        self.project_id = "PVT_1"
        self.issue_id = "I_1"
        self.item_id = "PVTI_1"
        self.item_exists = True
        self.fields: dict[str, Any] = {}
        self.schema = {}
        for index, owned in enumerate(github_projects.FIELD_OWNERSHIP, start=1):
            options = {}
            if owned["name"] == "Status":
                options = {name: f"status-{i}" for i, name in enumerate(
                    ("Todo", "In progress", "Review", "Blocked", "Done"), start=1
                )}
            elif owned["name"] == "Priority":
                options = {f"P{i}": f"priority-{i}" for i in range(1, 6)}
            elif owned["name"] == "Risk":
                options = {name: f"risk-{i}" for i, name in enumerate(
                    ("Auto", "Low", "Medium", "High"), start=1
                )}
            self.schema[owned["name"]] = {
                "id": f"F_{index}", "kind": owned["kind"], "options": options,
            }
        self.mutations = []
        self.item_reads = 0
        self.drift_on_item_read = None
        self.fail_after_mutation = None
        self._failed_once = False

    def graphql(self, query, variables):
        if "CortexProjectMeta" in query:
            return {"user": {"projectV2": {
                "id": self.project_id, "title": "Mirror", "closed": False,
            }}, "organization": None}
        if "CortexProjectFields" in query:
            nodes = []
            for name, descriptor in self.schema.items():
                nodes.append({
                    "id": descriptor["id"],
                    "name": name,
                    "dataType": KIND_TO_DATA[descriptor["kind"]],
                    "options": [
                        {"name": option, "id": option_id}
                        for option, option_id in descriptor["options"].items()
                    ],
                })
            return {"user": {"projectV2": {
                "id": self.project_id,
                "fields": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}, "organization": None}
        if "CortexProjectItems" in query:
            self.item_reads += 1
            if self.item_reads == self.drift_on_item_read:
                self.fields["Cortex ID"] = "external-change"
            nodes = []
            if self.item_exists:
                nodes.append({
                    "id": self.item_id,
                    "type": "ISSUE",
                    "content": {
                        "__typename": "Issue", "id": self.issue_id, "number": 2,
                        "title": "Proof", "url": "https://example/2", "state": "OPEN",
                    },
                    "fieldValues": {
                        "nodes": [self._value_node(name, value)
                                  for name, value in self.fields.items()],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                })
            return {"user": {"projectV2": {
                "id": self.project_id,
                "items": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}, "organization": None}
        if "CortexItemValues" in query:
            raise AssertionError("test fixture does not paginate item values")
        if "CortexAddProjectItem" in query:
            self.item_exists = True
            return self._mutation_result(query, variables)
        if "CortexSetProjectField" in query:
            name = self._field_name(variables["field"])
            value = variables["value"]
            if "text" in value:
                logical = value["text"]
            elif "number" in value:
                logical = value["number"]
            elif "date" in value:
                logical = value["date"]
            else:
                logical = next(
                    option for option, option_id in self.schema[name]["options"].items()
                    if option_id == value["singleSelectOptionId"]
                )
            self.fields[name] = logical
            return self._mutation_result(query, variables)
        if "CortexClearProjectField" in query:
            self.fields.pop(self._field_name(variables["field"]), None)
            return self._mutation_result(query, variables)
        raise AssertionError("unexpected GraphQL operation")

    def _mutation_result(self, query, variables):
        self.mutations.append((query, dict(variables)))
        if (
            self.fail_after_mutation == len(self.mutations)
            and not self._failed_once
        ):
            self._failed_once = True
            raise RuntimeError("injected transport interruption")
        return {"ok": True}

    def _field_name(self, field_id):
        return next(name for name, value in self.schema.items() if value["id"] == field_id)

    def _value_node(self, name, value):
        descriptor = self.schema[name]
        base = {
            "field": {
                "id": descriptor["id"], "name": name,
                "dataType": KIND_TO_DATA[descriptor["kind"]],
            },
        }
        if descriptor["kind"] == "text":
            return {"__typename": "ProjectV2ItemFieldTextValue", "text": value, **base}
        if descriptor["kind"] == "number":
            return {"__typename": "ProjectV2ItemFieldNumberValue", "number": value, **base}
        if descriptor["kind"] == "date":
            return {"__typename": "ProjectV2ItemFieldDateValue", "date": value, **base}
        return {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": value,
            "optionId": descriptor["options"][value],
            **base,
        }


def _task(isolated_db):
    with db.connect(isolated_db) as conn:
        project_id = store.create_project(
            conn, name="Mirror Test", repo_path="D:/mirror-test"
        )
        store.update_project(
            conn,
            project_id,
            github_project_owner="owner",
            github_project_number=1,
            github_project_id="PVT_1",
        )
        task_id = store.create_task(
            conn,
            project_id=project_id,
            title="Mirror one task",
            risk="high",
            priority=2,
            assignee="codex",
            progress=40,
            next_action="Verify the adapter",
            github_issue_id="I_1",
        )
        store.update_task(conn, task_id, status="running")
    return task_id


def _plan(isolated_db, remote, task_id):
    with db.connect(isolated_db) as conn:
        task = store.get_task(conn, task_id)
        return github_adapter.prepare(task, "owner", 1, transport=remote)[0]


def test_apply_verifies_atomic_local_state_and_replay_is_read_free(isolated_db):
    task_id = _task(isolated_db)
    remote = StatefulGitHub()
    plan = _plan(isolated_db, remote, task_id)
    operation_id = github_adapter.suggested_operation_id(plan.fingerprint)

    with db.connect(isolated_db) as conn:
        result = github_adapter.apply(
            conn, task_id, "owner", 1,
            approved_fingerprint=plan.fingerprint,
            operation_id=operation_id,
            transport=remote,
        )
        task = store.get_task(conn, task_id)
        operation = store.get_github_mirror_operation(conn, operation_id)
        events = store.list_activity_events(conn, task_id=task_id)

    assert result["status"] == "verified"
    assert result["remote_writes"] == sum(
        action.kind == "set_project_field" for action in plan.actions
    )
    assert task["github_project_item_id"] == "PVTI_1"
    assert task["sync_state"]
    assert operation["status"] == "verified"
    assert any(event["action"] == "github.mirror_applied" for event in events)

    mutation_count = len(remote.mutations)
    with db.connect(isolated_db) as conn:
        replay = github_adapter.apply(
            conn, task_id, "owner", 1,
            approved_fingerprint=plan.fingerprint,
            operation_id=operation_id,
            transport=remote,
        )
        final_task = store.get_task(conn, task_id)
        final_plan = github_adapter.prepare(
            final_task, "owner", 1, transport=remote
        )[0]
    assert replay["status"] == "already_verified"
    assert replay["remote_writes"] == 0
    assert len(remote.mutations) == mutation_count
    assert final_plan.actions == ()
    assert final_plan.conflicts == ()


def test_precondition_drift_refuses_before_first_write(isolated_db):
    task_id = _task(isolated_db)
    remote = StatefulGitHub()
    plan = _plan(isolated_db, remote, task_id)
    # Dry-run read is #1, apply's fingerprint re-read is #2, and the immediate
    # first-action precondition read is #3.
    remote.drift_on_item_read = 3
    operation_id = github_adapter.suggested_operation_id(plan.fingerprint)

    with db.connect(isolated_db) as conn:
        with pytest.raises(github_adapter.MirrorApplyRefused, match="precondition changed"):
            github_adapter.apply(
                conn, task_id, "owner", 1,
                approved_fingerprint=plan.fingerprint,
                operation_id=operation_id,
                transport=remote,
            )
        operation = store.get_github_mirror_operation(conn, operation_id)
        task = store.get_task(conn, task_id)
    assert operation["status"] == "refused"
    assert len(remote.mutations) == 0
    assert task["sync_state"] is None

    # A refused plan cannot be replayed under a fresh operation id.
    remote.fields.clear()
    remote.drift_on_item_read = None
    with db.connect(isolated_db) as conn:
        with pytest.raises(github_adapter.MirrorApplyRefused, match="terminal operation"):
            github_adapter.apply(
                conn, task_id, "owner", 1,
                approved_fingerprint=plan.fingerprint,
                operation_id="ghm-different-replay",
                transport=remote,
            )
    assert len(remote.mutations) == 0


def test_interrupted_write_resumes_same_operation_without_duplicate_mutation(isolated_db):
    task_id = _task(isolated_db)
    remote = StatefulGitHub()
    plan = _plan(isolated_db, remote, task_id)
    operation_id = github_adapter.suggested_operation_id(plan.fingerprint)
    remote.fail_after_mutation = 1

    with db.connect(isolated_db) as conn:
        with pytest.raises(github_adapter.MirrorApplyError, match="injected"):
            github_adapter.apply(
                conn, task_id, "owner", 1,
                approved_fingerprint=plan.fingerprint,
                operation_id=operation_id,
                transport=remote,
            )
        assert store.get_github_mirror_operation(conn, operation_id)["status"] == "interrupted"
        assert store.get_task(conn, task_id)["sync_state"] is None

        resumed = github_adapter.apply(
            conn, task_id, "owner", 1,
            approved_fingerprint=plan.fingerprint,
            operation_id=operation_id,
            transport=remote,
        )
        assert resumed["status"] == "verified"
        assert store.get_task(conn, task_id)["sync_state"]

    # The first remote write succeeded before the injected response failure;
    # resume observes the desired value and does not send it a second time.
    first_field_id = remote.mutations[0][1]["field"]
    assert sum(call[1].get("field") == first_field_id for call in remote.mutations) == 1
