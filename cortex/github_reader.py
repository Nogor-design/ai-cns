"""Read-only GitHub ProjectV2 inventory through the authenticated ``gh`` CLI.

The reader owns pagination and normalization only. It never mutates GitHub or
Cortex, and it returns the strict snapshot consumed by ``github_projects``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from typing import Any


class GitHubProjectError(RuntimeError):
    pass


PROJECT_META_QUERY = r"""
query CortexProjectMeta($url: URI!, $number: Int!) {
  resource(url: $url) {
    ... on User { projectV2(number: $number) { id title closed } }
    ... on Organization { projectV2(number: $number) { id title closed } }
  }
}
"""

ISSUE_QUERY = r"""
query CortexIssueIdentity($url: URI!) {
  resource(url: $url) {
    ... on Issue {
      id number title url state
      repository { nameWithOwner }
    }
  }
}
"""

PROJECT_FIELDS_QUERY = r"""
query CortexProjectFields($url: URI!, $number: Int!, $cursor: String) {
  resource(url: $url) {
    ... on User {
      projectV2(number: $number) {
        id
        fields(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            ... on ProjectV2Field { id name dataType }
            ... on ProjectV2SingleSelectField { id name dataType options { id name } }
            ... on ProjectV2IterationField { id name dataType }
          }
        }
      }
    }
    ... on Organization {
      projectV2(number: $number) {
        id
        fields(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            ... on ProjectV2Field { id name dataType }
            ... on ProjectV2SingleSelectField { id name dataType options { id name } }
            ... on ProjectV2IterationField { id name dataType }
          }
        }
      }
    }
  }
}
"""

_FIELD_VALUE_NODES = r"""
nodes {
  __typename
  ... on ProjectV2ItemFieldTextValue {
    text
    field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
  ... on ProjectV2ItemFieldNumberValue {
    number
    field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
  ... on ProjectV2ItemFieldDateValue {
    date
    field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
  ... on ProjectV2ItemFieldSingleSelectValue {
    name optionId
    field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
  ... on ProjectV2ItemFieldIterationValue {
    title iterationId
    field {
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
}
"""

PROJECT_ITEMS_QUERY = rf"""
query CortexProjectItems($url: URI!, $number: Int!, $cursor: String) {{
  resource(url: $url) {{
    ... on User {{
      projectV2(number: $number) {{
        id
        items(first: 100, after: $cursor) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            id type
            content {{
              __typename
              ... on Issue {{ id number title url state }}
              ... on PullRequest {{ id number title url state }}
              ... on DraftIssue {{ id title }}
            }}
            fieldValues(first: 100) {{
              pageInfo {{ hasNextPage endCursor }}
              {_FIELD_VALUE_NODES}
            }}
          }}
        }}
      }}
    }}
    ... on Organization {{
      projectV2(number: $number) {{
        id
        items(first: 100, after: $cursor) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            id type
            content {{
              __typename
              ... on Issue {{ id number title url state }}
              ... on PullRequest {{ id number title url state }}
              ... on DraftIssue {{ id title }}
            }}
            fieldValues(first: 100) {{
              pageInfo {{ hasNextPage endCursor }}
              {_FIELD_VALUE_NODES}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

ITEM_VALUES_QUERY = rf"""
query CortexItemValues($item: ID!, $cursor: String) {{
  node(id: $item) {{
    ... on ProjectV2Item {{
      id
      fieldValues(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        {_FIELD_VALUE_NODES}
      }}
    }}
  }}
}}
"""


class GitHubCLITransport:
    """Bounded JSON GraphQL transport; never invokes a shell."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 45.0,
    ) -> None:
        self._runner = runner
        self._timeout = timeout

    def graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        argv = ["gh", "api", "graphql", "--input", "-"]
        request_body = json.dumps({"query": query, "variables": dict(variables)})
        try:
            result = self._runner(
                argv,
                input=request_body,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubProjectError(f"GitHub CLI request failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown gh error").strip()[:2000]
            raise GitHubProjectError(f"GitHub GraphQL request failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubProjectError("GitHub CLI returned invalid JSON") from exc
        if payload.get("errors"):
            raise GitHubProjectError(
                "GitHub GraphQL returned errors: "
                + json.dumps(payload["errors"], sort_keys=True)[:2000]
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubProjectError("GitHub GraphQL response has no data object")
        return data


def read_project(
    owner: str,
    number: int,
    *,
    transport: GitHubCLITransport | Any | None = None,
) -> dict[str, Any]:
    """Read every Project field, item, and per-item field-value page."""
    api = transport or GitHubCLITransport()
    variables = {"url": f"https://github.com/{owner}", "number": int(number)}
    meta = _project_from(api.graphql(PROJECT_META_QUERY, variables))
    project_id = str(meta["id"])

    schema: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        project = _project_from(api.graphql(
            PROJECT_FIELDS_QUERY, {**variables, "cursor": cursor}
        ))
        if project.get("id") != project_id:
            raise GitHubProjectError("Project identity changed during field pagination")
        page = project.get("fields") or {}
        for node in page.get("nodes") or []:
            if not node or not node.get("name") or not node.get("id"):
                continue
            kind = _field_kind(node.get("dataType"))
            schema[str(node["name"])] = {
                "id": str(node["id"]),
                "kind": kind,
                "options": {
                    str(option["name"]): str(option["id"])
                    for option in (node.get("options") or [])
                    if option.get("name") is not None and option.get("id")
                },
            }
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubProjectError("Project field pagination omitted its next cursor")

    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        project = _project_from(api.graphql(
            PROJECT_ITEMS_QUERY, {**variables, "cursor": cursor}
        ))
        if project.get("id") != project_id:
            raise GitHubProjectError("Project identity changed during item pagination")
        page = project.get("items") or {}
        for node in page.get("nodes") or []:
            if not node or not node.get("id"):
                continue
            values = node.get("fieldValues") or {}
            value_nodes = list(values.get("nodes") or [])
            value_cursor = (values.get("pageInfo") or {}).get("endCursor")
            has_more_values = bool((values.get("pageInfo") or {}).get("hasNextPage"))
            while has_more_values:
                if not value_cursor:
                    raise GitHubProjectError(
                        f"Project item {node['id']} field pagination omitted its next cursor"
                    )
                value_data = api.graphql(
                    ITEM_VALUES_QUERY,
                    {"item": node["id"], "cursor": value_cursor},
                )
                item_node = value_data.get("node") or {}
                if item_node.get("id") != node["id"]:
                    raise GitHubProjectError("Project item identity changed during field pagination")
                next_values = item_node.get("fieldValues") or {}
                value_nodes.extend(next_values.get("nodes") or [])
                value_info = next_values.get("pageInfo") or {}
                has_more_values = bool(value_info.get("hasNextPage"))
                value_cursor = value_info.get("endCursor")
            content = node.get("content") or {}
            items.append({
                "id": str(node["id"]),
                "content_id": str(content["id"]) if content.get("id") else None,
                "content_type": content.get("__typename"),
                "content": {
                    key: content.get(key)
                    for key in ("number", "title", "url", "state")
                    if content.get(key) is not None
                },
                "fields": _normalize_values(value_nodes),
                "fields_complete": True,
            })
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubProjectError("Project item pagination omitted its next cursor")

    return {
        "project_id": project_id,
        "project_owner": owner,
        "project_number": int(number),
        "project_title": meta.get("title"),
        "project_closed": bool(meta.get("closed")),
        "field_schema_complete": True,
        "field_schema": schema,
        "items_complete": True,
        "items": items,
    }


def read_issue(
    url: str,
    *,
    transport: GitHubCLITransport | Any | None = None,
) -> dict[str, Any]:
    """Resolve one explicit issue URL to its stable identity without mutation."""
    api = transport or GitHubCLITransport()
    resource = api.graphql(ISSUE_QUERY, {"url": url}).get("resource")
    if not isinstance(resource, Mapping) or not resource.get("id"):
        raise GitHubProjectError("URL did not resolve to a GitHub issue")
    repository = resource.get("repository") or {}
    return {
        "id": str(resource["id"]),
        "number": int(resource["number"]),
        "url": str(resource["url"]),
        "title": resource.get("title"),
        "state": resource.get("state"),
        "repository": repository.get("nameWithOwner"),
    }


def _project_from(data: Mapping[str, Any]) -> dict[str, Any]:
    resource = data.get("resource")
    if isinstance(resource, Mapping) and isinstance(resource.get("projectV2"), Mapping):
        return dict(resource["projectV2"])
    for owner_kind in ("user", "organization"):
        owner = data.get(owner_kind)
        if isinstance(owner, Mapping) and isinstance(owner.get("projectV2"), Mapping):
            return dict(owner["projectV2"])
    raise GitHubProjectError("GitHub Project was not found for the configured owner and number")


def _field_kind(data_type: Any) -> str:
    return {
        "TEXT": "text",
        "NUMBER": "number",
        "DATE": "date",
        "SINGLE_SELECT": "single_select",
        "ITERATION": "iteration",
    }.get(str(data_type), str(data_type or "unknown").lower())


def _normalize_values(nodes: list[Mapping[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for node in nodes:
        field = node.get("field") or {}
        name = field.get("name")
        if not name:
            continue
        typename = str(node.get("__typename") or "")
        if typename == "ProjectV2ItemFieldTextValue":
            value = node.get("text")
        elif typename == "ProjectV2ItemFieldNumberValue":
            value = node.get("number")
        elif typename == "ProjectV2ItemFieldDateValue":
            value = node.get("date")
        elif typename == "ProjectV2ItemFieldSingleSelectValue":
            value = node.get("name")
        elif typename == "ProjectV2ItemFieldIterationValue":
            value = node.get("title")
        else:
            continue
        fields[str(name)] = value
    return fields
