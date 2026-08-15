"""Pure dry-run planning for a Cortex-to-GitHub Projects engineering mirror.

This module deliberately contains no network or subprocess calls.  Cortex is the
source of truth; callers must inspect a plan before a future adapter translates
its logical actions into GraphQL mutations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping


MIRROR_VERSION = 1
SYNC_FIELD = "Cortex Sync"

FIELD_OWNERSHIP: tuple[dict[str, str], ...] = (
    {"name": "Cortex ID", "kind": "text", "owner": "cortex"},
    {"name": "Cortex Project", "kind": "text", "owner": "cortex"},
    {"name": "Status", "kind": "single_select", "owner": "cortex"},
    {"name": "Priority", "kind": "single_select", "owner": "cortex"},
    {"name": "Progress", "kind": "number", "owner": "cortex"},
    {"name": "Start date", "kind": "date", "owner": "cortex"},
    {"name": "Target date", "kind": "date", "owner": "cortex"},
    {"name": "Risk", "kind": "single_select", "owner": "cortex"},
    {"name": "Worker", "kind": "text", "owner": "cortex"},
    {"name": "Cortex Milestone", "kind": "text", "owner": "cortex"},
    {"name": "Next action", "kind": "text", "owner": "cortex"},
    {"name": "Blocked reason", "kind": "text", "owner": "cortex"},
    {"name": SYNC_FIELD, "kind": "text", "owner": "cortex"},
)

STATUS_VALUES = {
    "open": "Todo",
    "assigned": "Todo",
    "in_progress": "In progress",
    "running": "In progress",
    "review": "Review",
    "blocked": "Blocked",
    "done": "Done",
    "abandoned": "Done",
}


@dataclass(frozen=True)
class MirrorAction:
    kind: str
    target_id: str
    field: str | None = None
    value: Any = None
    expected: Any = None
    reason: str = ""


@dataclass(frozen=True)
class MirrorConflict:
    code: str
    summary: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class MirrorPlan:
    task_id: str
    project_id: str | None
    issue_id: str | None
    project_item_id: str | None
    actions: tuple[MirrorAction, ...]
    conflicts: tuple[MirrorConflict, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "issue_id": self.issue_id,
            "project_item_id": self.project_item_id,
            "safe_to_apply": self.safe_to_apply,
            "actions": [asdict(action) for action in self.actions],
            "conflicts": [asdict(conflict) for conflict in self.conflicts],
        }


def desired_fields(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return the logical Project fields owned by Cortex for one task."""
    values: dict[str, Any] = {
        "Cortex ID": _value(task, "id"),
        "Cortex Project": _value(task, "project_id"),
        "Status": STATUS_VALUES.get(str(_value(task, "status", "open")), "Todo"),
        "Priority": f"P{max(1, min(5, int(_value(task, 'priority', 3))))}",
        "Progress": float(_value(task, "progress", 0)),
        "Start date": _date_only(_value(task, "start_at")),
        "Target date": _date_only(_value(task, "target_at") or _value(task, "due_at")),
        "Risk": str(_value(task, "risk", "auto")).replace("_", " ").title(),
        "Worker": _optional_text(_value(task, "assignee")),
        "Cortex Milestone": _optional_text(_value(task, "milestone")),
        "Next action": _optional_text(_value(task, "next_action")),
        "Blocked reason": _optional_text(_value(task, "blocked_reason")),
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    values[SYNC_FIELD] = f"v{MIRROR_VERSION}:{digest}"
    return values


def build_mirror_plan(
    task: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> MirrorPlan:
    """Reconcile one already-linked issue without performing any mutation.

    ``snapshot`` is a normalized ProjectV2 read model with ``project_id`` and
    ``items``. Each item contains ``id``, ``content_id``, and a logical ``fields``
    mapping. Stable node IDs, never titles, are the identity boundary.
    """
    task_id = str(_value(task, "id"))
    project_id = _optional_text(snapshot.get("project_id"))
    issue_id = _optional_text(_value(task, "github_issue_id"))
    stored_item_id = _optional_text(_value(task, "github_project_item_id"))
    conflicts: list[MirrorConflict] = []

    if not project_id:
        conflicts.append(MirrorConflict(
            "missing_project_id",
            "The GitHub Project node ID is missing from the read snapshot.",
            {},
        ))
    if not issue_id:
        conflicts.append(MirrorConflict(
            "missing_issue_link",
            "Cortex will not create or match an issue by title; link a GitHub issue node ID first.",
            {"task_id": task_id},
        ))
    if snapshot.get("items_complete") is not True:
        conflicts.append(MirrorConflict(
            "incomplete_project_snapshot",
            "All Project item pages must be read before Cortex can prove an item is absent.",
            {"items_complete": snapshot.get("items_complete")},
        ))
    if conflicts:
        return _plan(task_id, project_id, issue_id, stored_item_id, (), conflicts)

    items = [dict(item) for item in snapshot.get("items", [])]
    content_matches = [item for item in items if item.get("content_id") == issue_id]
    cortex_id_matches = [
        item for item in items
        if dict(item.get("fields") or {}).get("Cortex ID") == task_id
    ]
    if len(content_matches) > 1:
        conflicts.append(MirrorConflict(
            "duplicate_issue_items",
            "The linked issue appears more than once in the Project.",
            {"item_ids": [item.get("id") for item in content_matches]},
        ))
    collisions = [item for item in cortex_id_matches if item.get("content_id") != issue_id]
    if collisions:
        conflicts.append(MirrorConflict(
            "cortex_id_collision",
            "The Cortex ID is already attached to a different GitHub issue.",
            {
                "item_ids": [item.get("id") for item in collisions],
                "content_ids": [item.get("content_id") for item in collisions],
            },
        ))
    if conflicts:
        return _plan(task_id, project_id, issue_id, stored_item_id, (), conflicts)

    item = content_matches[0] if content_matches else None
    if item is None:
        if stored_item_id:
            stored = next(
                (candidate for candidate in items if candidate.get("id") == stored_item_id),
                None,
            )
            if stored:
                conflicts.append(MirrorConflict(
                    "stored_item_content_mismatch",
                    "The stored Project item points to a different GitHub content node.",
                    {
                        "stored_item_id": stored_item_id,
                        "observed_content_id": stored.get("content_id"),
                        "expected_content_id": issue_id,
                    },
                ))
                return _plan(task_id, project_id, issue_id, stored_item_id, (), conflicts)
            conflicts.append(MirrorConflict(
                "missing_remote_project_item",
                "Cortex stores a Project item ID that is absent from the current Project snapshot.",
                {"stored_item_id": stored_item_id},
            ))
            return _plan(task_id, project_id, issue_id, stored_item_id, (), conflicts)
        action = MirrorAction(
            "add_project_item",
            project_id,
            value={"content_id": issue_id},
            reason="Link the existing issue; re-read the Project before setting fields.",
        )
        return _plan(task_id, project_id, issue_id, None, (action,), ())

    item_id = _optional_text(item.get("id"))
    if not item_id:
        conflicts.append(MirrorConflict(
            "missing_project_item_id",
            "The matching Project item has no stable node ID.",
            {"content_id": issue_id},
        ))
        return _plan(task_id, project_id, issue_id, stored_item_id, (), conflicts)
    if stored_item_id and stored_item_id != item_id:
        conflicts.append(MirrorConflict(
            "stale_project_item_link",
            "The stored Project item ID disagrees with the item containing the linked issue.",
            {"stored_item_id": stored_item_id, "observed_item_id": item_id},
        ))
        return _plan(task_id, project_id, issue_id, item_id, (), conflicts)

    try:
        desired = desired_fields(task)
    except (TypeError, ValueError) as exc:
        conflicts.append(MirrorConflict(
            "invalid_cortex_task_data",
            "A Cortex-owned value cannot be normalized safely for GitHub.",
            {"error": str(exc)},
        ))
        return _plan(task_id, project_id, issue_id, item_id, (), conflicts)
    observed = dict(item.get("fields") or {})
    observed_marker = observed.get(SYNC_FIELD)
    try:
        last_sync = _parse_sync_state(_value(task, "sync_state"))
    except ValueError as exc:
        conflicts.append(MirrorConflict(
            "invalid_local_sync_state",
            "Cortex's stored GitHub sync evidence cannot be parsed safely.",
            {"error": str(exc)},
        ))
        return _plan(task_id, project_id, issue_id, item_id, (), conflicts)
    if last_sync:
        last_marker = last_sync.get("fingerprint")
        if observed_marker != last_marker:
            conflicts.append(MirrorConflict(
                "remote_sync_marker_changed",
                "The GitHub sync marker differs from Cortex's last verified write.",
                {"expected": last_marker, "observed": observed_marker},
            ))
            return _plan(task_id, project_id, issue_id, item_id, (), conflicts)
        changed_since_sync = [
            field for field, value in dict(last_sync.get("fields") or {}).items()
            if not _equal(field, observed.get(field), value)
        ]
        if changed_since_sync:
            conflicts.append(MirrorConflict(
                "remote_edit_to_cortex_owned_field",
                "A Cortex-owned field changed in GitHub after the last verified mirror.",
                {"fields": changed_since_sync, "sync": last_marker},
            ))
            return _plan(task_id, project_id, issue_id, item_id, (), conflicts)
    elif observed_marker:
        conflicts.append(MirrorConflict(
            "missing_local_sync_state",
            "GitHub contains a Cortex sync marker but Cortex has no matching verified state.",
            {"observed": observed_marker},
        ))
        return _plan(task_id, project_id, issue_id, item_id, (), conflicts)

    actions: list[MirrorAction] = []
    if not stored_item_id:
        actions.append(MirrorAction(
            "update_cortex_link",
            task_id,
            field="github_project_item_id",
            value=item_id,
            expected=None,
            reason="Persist the observed stable Project item node ID.",
        ))
    for field, value in desired.items():
        current = observed.get(field)
        if _equal(field, current, value):
            continue
        kind = "clear_project_field" if value is None else "set_project_field"
        actions.append(MirrorAction(
            kind,
            item_id,
            field=field,
            value=value,
            expected=current,
            reason="Cortex owns this mirrored Project field.",
        ))
    next_sync_state = _sync_state(desired)
    if _value(task, "sync_state") != next_sync_state:
        actions.append(MirrorAction(
            "update_cortex_sync_state",
            task_id,
            field="sync_state",
            value=next_sync_state,
            expected=_value(task, "sync_state"),
            reason="Persist only after a re-read verifies every remote field mutation.",
        ))
    return _plan(task_id, project_id, issue_id, item_id, actions, ())


def _plan(
    task_id: str,
    project_id: str | None,
    issue_id: str | None,
    item_id: str | None,
    actions: Any,
    conflicts: Any,
) -> MirrorPlan:
    return MirrorPlan(
        task_id,
        project_id,
        issue_id,
        item_id,
        tuple(actions),
        tuple(conflicts),
    )


def _value(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = mapping[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date_only(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid date value: {value!r}") from exc


def _equal(field: str, left: Any, right: Any) -> bool:
    if right is None:
        return left in (None, "")
    if field == "Progress":
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return left == right


def _sync_state(fields: Mapping[str, Any]) -> str:
    payload = {
        "version": MIRROR_VERSION,
        "fingerprint": fields[SYNC_FIELD],
        "fields": {key: value for key, value in fields.items() if key != SYNC_FIELD},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _parse_sync_state(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError("sync_state is not valid JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("fields"), dict):
        raise ValueError("sync_state must contain a fields object")
    return parsed
