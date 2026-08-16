"""Read-only GitHub Project inventory and pagination."""

from __future__ import annotations

import json
import subprocess

import pytest

from cortex import github_reader


def _field(name, field_id, data_type="TEXT", options=None):
    return {
        "id": field_id,
        "name": name,
        "dataType": data_type,
        "options": options or [],
    }


def _value(typename, field_name, **value):
    return {
        "__typename": typename,
        "field": {
            "id": f"field-{field_name}", "name": field_name, "dataType": "TEXT"
        },
        **value,
    }


class FakeTransport:
    def __init__(self):
        self.calls = []

    def graphql(self, query, variables):
        self.calls.append((query, dict(variables)))
        if "CortexProjectMeta" in query:
            return {
                "user": {"projectV2": {
                    "id": "PVT_1", "title": "Mirror", "closed": False,
                }},
                "organization": None,
            }
        if "CortexProjectFields" in query:
            cursor = variables.get("cursor")
            nodes = (
                [_field("Status", "F_status", "SINGLE_SELECT", [
                    {"id": "O_todo", "name": "Todo"},
                    {"id": "O_done", "name": "Done"},
                ])]
                if cursor is None else [_field("Worker", "F_worker")]
            )
            return {"user": {"projectV2": {
                "id": "PVT_1",
                "fields": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": cursor is None,
                        "endCursor": "fields-2" if cursor is None else None,
                    },
                },
            }}, "organization": None}
        if "CortexProjectItems" in query:
            cursor = variables.get("cursor")
            if cursor is None:
                nodes = [{
                    "id": "PVTI_1",
                    "type": "ISSUE",
                    "content": {
                        "__typename": "Issue", "id": "I_1", "number": 2,
                        "title": "Proof", "url": "https://example/2", "state": "OPEN",
                    },
                    "fieldValues": {
                        "nodes": [_value(
                            "ProjectV2ItemFieldSingleSelectValue", "Status",
                            name="Done", optionId="O_done",
                        )],
                        "pageInfo": {"hasNextPage": True, "endCursor": "values-2"},
                    },
                }]
            else:
                nodes = [{
                    "id": "PVTI_2", "type": "DRAFT_ISSUE",
                    "content": {"__typename": "DraftIssue", "id": "DI_1", "title": "Draft"},
                    "fieldValues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }]
            return {"user": {"projectV2": {
                "id": "PVT_1",
                "items": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": cursor is None,
                        "endCursor": "items-2" if cursor is None else None,
                    },
                },
            }}, "organization": None}
        if "CortexItemValues" in query:
            assert variables == {"item": "PVTI_1", "cursor": "values-2"}
            return {"node": {
                "id": "PVTI_1",
                "fieldValues": {
                    "nodes": [_value(
                        "ProjectV2ItemFieldTextValue", "Worker", text="codex"
                    )],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}
        raise AssertionError("unexpected query")


def test_reader_follows_project_and_per_item_pagination():
    transport = FakeTransport()
    snapshot = github_reader.read_project("owner", 1, transport=transport)

    assert snapshot["project_id"] == "PVT_1"
    assert snapshot["items_complete"] is True
    assert snapshot["field_schema_complete"] is True
    assert snapshot["field_schema"]["Status"]["options"]["Done"] == "O_done"
    assert snapshot["field_schema"]["Worker"]["id"] == "F_worker"
    assert len(snapshot["items"]) == 2
    assert snapshot["items"][0]["fields_complete"] is True
    assert snapshot["items"][0]["fields"] == {"Status": "Done", "Worker": "codex"}
    assert sum("CortexProjectFields" in query for query, _ in transport.calls) == 2
    assert sum("CortexProjectItems" in query for query, _ in transport.calls) == 2
    assert sum("CortexItemValues" in query for query, _ in transport.calls) == 1


def test_transport_uses_argument_vector_and_rejects_graphql_errors():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"data": {"ok": True}}), stderr=""
        )

    transport = github_reader.GitHubCLITransport(runner=runner)
    assert transport.graphql("query Test { viewer { login } }", {"number": 1}) == {"ok": True}
    argv, kwargs = calls[0]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert json.loads(kwargs["input"])["variables"] == {"number": 1}
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 45.0

    def error_runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"errors": [{"message": "denied"}]}), stderr=""
        )

    with pytest.raises(github_reader.GitHubProjectError, match="denied"):
        github_reader.GitHubCLITransport(runner=error_runner).graphql("query", {})


def test_explicit_issue_url_resolves_to_stable_identity():
    class IssueTransport:
        def graphql(self, query, variables):
            assert "CortexIssueIdentity" in query
            assert variables == {"url": "https://github.com/example/repo/issues/7"}
            return {"resource": {
                "id": "I_7", "number": 7, "title": "Existing issue",
                "url": variables["url"], "state": "OPEN",
                "repository": {"nameWithOwner": "example/repo"},
            }}

    issue = github_reader.read_issue(
        "https://github.com/example/repo/issues/7", transport=IssueTransport()
    )
    assert issue == {
        "id": "I_7",
        "number": 7,
        "url": "https://github.com/example/repo/issues/7",
        "title": "Existing issue",
        "state": "OPEN",
        "repository": "example/repo",
    }
