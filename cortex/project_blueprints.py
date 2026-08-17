"""Versioned project blueprints and a read-only delivery-phase projection.

The Markdown artifact is portable repository intent. SQLite stores immutable
approved revisions and the normalized phase records Cortex needs to display
and govern work without treating the database as a second design document.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import evidence, gitutil, ids, store


BLUEPRINT_STATUSES = {"missing", "draft", "review", "approved", "stale"}
PHASE_STATUSES = {
    "proposed", "planned", "active", "review", "complete", "paused", "cancelled"
}
MAX_BLUEPRINT_BYTES = 200_000
REQUIRED_SECTIONS = (
    "Purpose and intended users",
    "Problem and desired outcomes",
    "Current baseline",
    "Scope and non-goals",
    "Constraints and safeguards",
    "Success measures",
    "Product or operational workflow",
    "Architecture and key boundaries",
    "Delivery phases",
    "Risks, assumptions, and open decisions",
    "Plan basis and review provenance",
)
ONBOARDING_QUESTIONS = (
    {
        "key": "users_and_problem",
        "label": "Who is this for, and what problem should become easier?",
        "help": "Name the intended users and the concrete friction they experience.",
    },
    {
        "key": "desired_outcome",
        "label": "What observable outcome would make this project worthwhile?",
        "help": "Use evidence someone can verify, not a model confidence score.",
    },
    {
        "key": "non_goals",
        "label": "What must this project not do?",
        "help": "Record the most important boundary or prohibited behavior.",
    },
    {
        "key": "constraints",
        "label": "What deadline, dependency, or business constraint matters?",
        "help": "Say none currently when there is no material constraint.",
    },
    {
        "key": "open_decision",
        "label": "What decision is still genuinely open?",
        "help": "Say none currently when the direction is settled.",
    },
)
PHASE_QUESTIONS = (
    {"key": "phase_name", "label": "Current phase name"},
    {"key": "phase_outcome", "label": "Current phase outcome"},
    {"key": "phase_exit_criteria", "label": "Observable exit criteria, one per line"},
)


def blueprint_path(project: sqlite3.Row) -> Path:
    configured = str(project["blueprint_path"] or "").strip()
    if configured:
        return Path(configured)
    return Path(project["repo_path"]) / ".cortex" / "blueprint.md"


def content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _json_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 1_000:
            raise ValueError(f"{label} entries must be 1000 characters or fewer")
        result.append(text)
    return result


def _normalized_depends_on(raw: Any, index: int) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"phase {index} depends_on must be a list of ordinals")
    cleaned: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            raise ValueError(f"phase {index} depends_on ordinals must be integers")
        if value <= 0:
            raise ValueError(f"phase {index} depends_on ordinals must be positive")
        if value >= index:
            raise ValueError(
                f"phase {index} dependencies must reference earlier phases only"
            )
        if value in cleaned:
            raise ValueError(f"phase {index} has duplicate dependency: {value}")
        cleaned.append(value)
    return cleaned


def validate_markdown(markdown: Any) -> str:
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("blueprint Markdown is required")
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_BLUEPRINT_BYTES:
        raise ValueError("blueprint Markdown must be 200 KB or smaller")
    headings = {
        match.group(1).strip().casefold()
        for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", markdown)
    }
    missing = [section for section in REQUIRED_SECTIONS if section.casefold() not in headings]
    if missing:
        raise ValueError("blueprint is missing required sections: " + ", ".join(missing))
    if re.search(r"(?is)<\s*(script|iframe|object|embed|link|meta)\b", markdown):
        raise ValueError("blueprint contains executable or remote-loading HTML")
    return markdown.rstrip() + "\n"


def normalize_phases(raw_phases: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_phases, list) or not raw_phases:
        raise ValueError("at least one delivery phase is required")
    phases: list[dict[str, Any]] = []
    active_count = 0
    for index, raw in enumerate(raw_phases, start=1):
        if not isinstance(raw, dict):
            raise ValueError("each delivery phase must be an object")
        name = str(raw.get("name") or "").strip()
        outcome = str(raw.get("outcome") or "").strip()
        status = str(raw.get("status") or ("active" if index == 1 else "planned")).lower()
        if not name or len(name) > 120:
            raise ValueError(f"phase {index} needs a name of 120 characters or fewer")
        if not outcome or len(outcome) > 2_000:
            raise ValueError(f"phase {index} needs an outcome of 2000 characters or fewer")
        if status not in PHASE_STATUSES:
            raise ValueError(f"invalid phase status: {status}")
        if status == "active":
            active_count += 1
        phases.append({
            "ordinal": index,
            "name": name,
            "status": status,
            "outcome": outcome,
            "non_goals": str(raw.get("non_goals") or "").strip() or None,
            "entry_criteria": _json_list(raw.get("entry_criteria"), "entry criteria"),
            "exit_criteria": _json_list(raw.get("exit_criteria"), "exit criteria"),
            "start_at": str(raw.get("start_at") or "").strip() or None,
            "target_at": str(raw.get("target_at") or "").strip() or None,
            "depends_on_ordinals": _normalized_depends_on(
                raw.get("depends_on_ordinals"), index,
            ),
        })
    if active_count != 1:
        raise ValueError("an approved blueprint must have exactly one active phase")
    return phases


def _canonical_plan_basis(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("plan basis must be an object")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 100_000:
        raise ValueError("plan basis must be 100 KB or smaller")
    return value


def _fingerprint(
    project_id: str,
    markdown: str,
    phases: list[dict[str, Any]],
    plan_basis: dict[str, Any],
    expected_current_hash: str | None,
) -> str:
    canonical = json.dumps(
        {
            "project_id": project_id,
            "content_hash": content_hash(markdown),
            "phases": phases,
            "plan_basis": plan_basis,
            "expected_current_hash": expected_current_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin_draft(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    answers: dict[str, Any] | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    """Persist resumable local discovery without contacting any provider."""
    project = store.get_project(conn, project_id)
    if stage is not None and str(stage) not in {"questions", "planning", "review"}:
        raise ValueError("blueprint draft stage must be questions, planning, or review")
    existing = conn.execute(
        "SELECT * FROM project_blueprint_drafts WHERE project_id=?", (project["id"],)
    ).fetchone()
    if existing:
        discovered_payload = json.loads(existing["discovery_json"] or "{}")
        discovered = str(discovered_payload.get("bounded_repository_evidence") or "")
        digest = str(existing["discovery_hash"])
        saved_answers = json.loads(existing["answers_json"] or "{}")
    else:
        discovered = evidence.bundle(project["repo_path"])
        digest = hashlib.sha256(discovered.encode("utf-8")).hexdigest()
        saved_answers = {}
    effective_stage = str(stage or (existing["stage"] if existing else "questions"))
    preview_invalidated = answers is not None or (
        stage is not None and str(stage) != "review"
    )
    if existing:
        planning_task_id = existing["planning_task_id"]
        pm_session_id = existing["pm_session_id"]
        saved_preview_json = None if preview_invalidated else existing["preview_json"]
        saved_preview_fingerprint = (
            None if preview_invalidated else existing["preview_fingerprint"]
        )
        saved_previewed_at = None if preview_invalidated else existing["previewed_at"]
    else:
        saved_preview_json = None
        saved_preview_fingerprint = None
        saved_previewed_at = None
        pm_session_id = ids.short_id()
        planning_task_id = store.create_task(
            conn,
            project_id=project["id"],
            title="Build project blueprint",
            type="planning",
            acceptance=(
                "Owner approves a minimum blueprint and one active phase from an exact preview."
            ),
            assignee="owner",
            next_action="Answer the guided product-intent and current-phase questions.",
            pm_session_id=pm_session_id,
            actor_type="system",
            actor_name="cortex",
            source="cortex-blueprint",
            commit=False,
        )
        store.update_task(
            conn,
            planning_task_id,
            status="in_progress",
            actor_type="system",
            actor_name="cortex",
            source="cortex-blueprint",
            commit=False,
        )
        store.create_activity_event(
            conn,
            project_id=project["id"],
            task_id=planning_task_id,
            actor_type="system",
            actor_name="cortex",
            action="pm.session_started",
            summary="PM session started: Build project blueprint",
            source="cortex-blueprint",
            source_ref=planning_task_id,
            session_id=pm_session_id,
            commit=False,
        )
    now = ids.now()
    if answers is not None:
        if not isinstance(answers, dict):
            raise ValueError("blueprint answers must be an object")
        saved_answers.update({
            str(key): str(value or "").strip()
            for key, value in answers.items()
            if str(key) in {
                *(question["key"] for question in ONBOARDING_QUESTIONS),
                *(question["key"] for question in PHASE_QUESTIONS),
            }
        })
    conn.execute(
        """INSERT INTO project_blueprint_drafts
           (project_id, stage, answers_json, discovery_hash, discovery_json,
            preview_json, preview_fingerprint, previewed_at,
            planning_task_id, pm_session_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET
             stage=excluded.stage, answers_json=excluded.answers_json,
             discovery_hash=excluded.discovery_hash,
             discovery_json=excluded.discovery_json,
             preview_json=excluded.preview_json,
             preview_fingerprint=excluded.preview_fingerprint,
             previewed_at=excluded.previewed_at,
             updated_at=excluded.updated_at""",
        (
            project["id"], effective_stage, json.dumps(saved_answers, sort_keys=True), digest,
            json.dumps({"bounded_repository_evidence": discovered}, sort_keys=True),
            saved_preview_json, saved_preview_fingerprint, saved_previewed_at,
            planning_task_id, pm_session_id, now, now,
        ),
    )
    conn.execute(
        """UPDATE projects SET blueprint_status='draft', updated_at=? WHERE id=?""",
        (now, project["id"]),
    )
    store.create_activity_event(
        conn,
        project_id=project["id"],
        actor_type="agent",
        actor_name="codex",
        action="blueprint.draft_saved",
        summary="Saved resumable local blueprint discovery",
        source="cortex-blueprint",
        source_ref=digest,
        evidence={
            "stage": effective_stage,
            "discovery_hash": digest,
            "preview_invalidated": preview_invalidated,
        },
        commit=False,
    )
    conn.commit()
    payload = {
        "project_id": project["id"],
        "stage": effective_stage,
        "answers": saved_answers,
        "discovery_hash": digest,
        "planning_task_id": planning_task_id,
        "pm_session_id": pm_session_id,
        "questions": list(ONBOARDING_QUESTIONS),
        "phase_questions": list(PHASE_QUESTIONS),
        "discovery": {
            "repo_path": project["repo_path"],
            "stack": project["stack"],
            "current_goal": project["current_goal"],
            "privacy": project["privacy"],
            "bounded_characters": len(discovered),
        },
    }
    if saved_preview_json:
        payload["preview"] = _stored_preview(
            saved_preview_json, saved_preview_fingerprint
        )
    else:
        payload["preview"] = None
    return payload


def _stored_preview(
    preview_json: Any, preview_fingerprint: Any,
) -> dict[str, Any]:
    try:
        payload = json.loads(str(preview_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("saved blueprint approval preview is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("saved blueprint approval preview must be an object")
    fingerprint = str(payload.get("preview_fingerprint") or "")
    if not fingerprint or fingerprint != str(preview_fingerprint or ""):
        raise ValueError("saved blueprint approval preview fingerprint does not match")
    return payload


def _persist_review_preview(
    conn: sqlite3.Connection,
    project_id: str,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    project = store.get_project(conn, project_id)
    draft = conn.execute(
        "SELECT * FROM project_blueprint_drafts WHERE project_id=?", (project["id"],)
    ).fetchone()
    if draft is None:
        begin_draft(conn, project["id"])
        draft = conn.execute(
            "SELECT * FROM project_blueprint_drafts WHERE project_id=?", (project["id"],)
        ).fetchone()
    now = ids.now()
    encoded = json.dumps(prepared, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """UPDATE project_blueprint_drafts
           SET stage='review', preview_json=?, preview_fingerprint=?,
               previewed_at=?, updated_at=?
           WHERE project_id=?""",
        (
            encoded, prepared["preview_fingerprint"], now, now, project["id"],
        ),
    )
    conn.execute(
        "UPDATE projects SET blueprint_status='review', updated_at=? WHERE id=?",
        (now, project["id"]),
    )
    if draft and draft["planning_task_id"]:
        store.update_task(
            conn,
            draft["planning_task_id"],
            progress=90,
            next_action=(
                "Owner reviews and approves or rejects the exact saved blueprint preview."
            ),
            actor_type="system",
            actor_name="cortex",
            source="cortex-blueprint",
            commit=False,
        )
    store.create_activity_event(
        conn,
        project_id=project["id"],
        task_id=draft["planning_task_id"] if draft else None,
        actor_type="agent",
        actor_name="codex",
        action="blueprint.approval_preview_prepared",
        summary=(
            "Saved an exact restart-safe blueprint approval preview; no file, task, "
            "provider, or phase mutation was applied"
        ),
        source="cortex-blueprint",
        source_ref=prepared["preview_fingerprint"],
        session_id=draft["pm_session_id"] if draft else None,
        evidence={
            "preview_fingerprint": prepared["preview_fingerprint"],
            "content_hash": prepared["content_hash"],
            "expected_current_hash": prepared["expected_current_hash"],
            "phase_count": len(prepared["phases"]),
            "writes": prepared["writes"],
        },
        commit=False,
    )
    conn.commit()
    return prepared


def draft_detail(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = store.get_project(conn, project_id)
    row = conn.execute(
        "SELECT * FROM project_blueprint_drafts WHERE project_id=?", (project["id"],)
    ).fetchone()
    if row is None:
        return begin_draft(conn, project["id"])
    discovery = json.loads(row["discovery_json"] or "{}")
    return {
        "project_id": project["id"],
        "stage": row["stage"],
        "answers": json.loads(row["answers_json"] or "{}"),
        "discovery_hash": row["discovery_hash"],
        "planning_task_id": row["planning_task_id"],
        "pm_session_id": row["pm_session_id"],
        "preview": (
            _stored_preview(row["preview_json"], row["preview_fingerprint"])
            if row["preview_json"] else None
        ),
        "questions": list(ONBOARDING_QUESTIONS),
        "phase_questions": list(PHASE_QUESTIONS),
        "discovery": {
            "repo_path": project["repo_path"],
            "stack": project["stack"],
            "current_goal": project["current_goal"],
            "privacy": project["privacy"],
            "bounded_characters": len(
                str(discovery.get("bounded_repository_evidence") or "")
            ),
        },
    }


def _required_answer(answers: dict[str, Any], key: str, label: str) -> str:
    value = str(answers.get(key) or "").strip()
    if not value:
        raise ValueError(f"answer required: {label}")
    if len(value) > 4_000:
        raise ValueError(f"{label} must be 4000 characters or fewer")
    return value


def draft_preview(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Compile saved owner answers into a deterministic approval preview."""
    project = store.get_project(conn, project_id)
    draft = draft_detail(conn, project["id"])
    answers = draft["answers"]
    users_problem = _required_answer(
        answers, "users_and_problem", ONBOARDING_QUESTIONS[0]["label"]
    )
    desired_outcome = _required_answer(
        answers, "desired_outcome", ONBOARDING_QUESTIONS[1]["label"]
    )
    non_goals = _required_answer(
        answers, "non_goals", ONBOARDING_QUESTIONS[2]["label"]
    )
    constraints = _required_answer(
        answers, "constraints", ONBOARDING_QUESTIONS[3]["label"]
    )
    open_decision = _required_answer(
        answers, "open_decision", ONBOARDING_QUESTIONS[4]["label"]
    )
    phase_name = _required_answer(answers, "phase_name", "current phase name")
    phase_outcome = _required_answer(answers, "phase_outcome", "current phase outcome")
    exit_text = _required_answer(
        answers, "phase_exit_criteria", "current phase exit criteria"
    )
    exit_criteria = [line.strip(" -\t") for line in exit_text.splitlines() if line.strip(" -\t")]
    if not exit_criteria:
        raise ValueError("current phase needs at least one observable exit criterion")

    phase_rows = [{
        "name": phase_name,
        "status": "active",
        "outcome": phase_outcome,
        "non_goals": non_goals,
        "entry_criteria": ["Owner approved this blueprint revision"],
        "exit_criteria": exit_criteria,
    }]
    stack = str(project["stack"] or "Not yet recorded")
    markdown = f"""# Project Blueprint

## Purpose and intended users
{users_problem}

## Problem and desired outcomes
{desired_outcome}

## Current baseline
Cortex tracks `{project['name']}` at `{project['repo_path']}`. Recorded stack: {stack}.

## Scope and non-goals
{non_goals}

## Constraints and safeguards
{constraints}

## Success measures
{desired_outcome}

## Product or operational workflow
The owner approves this blueprint and its current phase before guided execution begins. Cortex then shows phase gates and verified evidence without automatically creating tasks or completing phases.

## Architecture and key boundaries
The Git-tracked Markdown blueprint is the portable design artifact. Cortex SQLite records immutable revisions, operational phases, approvals, and evidence. Repository files remain owner-controlled.

## Delivery phases
1. **{phase_name}** — {phase_outcome}

## Risks, assumptions, and open decisions
{open_decision}

## Plan basis and review provenance
Bounded local repository discovery: `{draft['discovery_hash']}`. Owner answers were saved locally through the guided onboarding interview. No specialist provider was contacted for this deterministic draft.
"""
    basis = {
        "discovery_hash": draft["discovery_hash"],
        "owner_answers": answers,
        "specialist_reviews": [],
        "generator": "deterministic-guided-onboarding",
        "provider_contacted": False,
    }
    prepared = preview(
        conn,
        project["id"],
        markdown=markdown,
        phases=phase_rows,
        plan_basis=basis,
    )
    return _persist_review_preview(conn, project["id"], prepared)


def preview(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    markdown: Any,
    phases: Any,
    plan_basis: Any = None,
    persist_review: bool = False,
) -> dict[str, Any]:
    project = store.get_project(conn, project_id)
    normalized_markdown = validate_markdown(markdown)
    normalized_phases = normalize_phases(phases)
    normalized_basis = _canonical_plan_basis(plan_basis)
    path = blueprint_path(project)
    actual_hash = file_hash(path)
    managed_hash = project["blueprint_hash"]
    if actual_hash is not None and not project["blueprint_path"]:
        raise ValueError(
            "an owner blueprint already exists at .cortex/blueprint.md; import or link it instead of overwriting it"
        )
    if managed_hash and actual_hash != managed_hash:
        raise ValueError("the managed blueprint changed; reconcile drift before approving a revision")
    fingerprint = _fingerprint(
        project["id"], normalized_markdown, normalized_phases, normalized_basis, actual_hash
    )
    prepared = {
        "project_id": project["id"],
        "path": str(path),
        "content_hash": content_hash(normalized_markdown),
        "expected_current_hash": actual_hash,
        "preview_fingerprint": fingerprint,
        "blueprint_status": "review",
        "markdown": normalized_markdown,
        "phases": normalized_phases,
        "plan_basis": normalized_basis,
        "writes": {
            "create_file": actual_hash is None,
            "create_revision": True,
            "replace_phase_projection": True,
            "create_tasks": False,
            "contact_provider": False,
        },
    }
    if persist_review:
        return _persist_review_preview(conn, project["id"], prepared)
    return prepared


def approve_draft(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    preview_fingerprint: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    """Approve only the exact preview persisted for this project's draft."""
    project = store.get_project(conn, project_id)
    row = conn.execute(
        "SELECT * FROM project_blueprint_drafts WHERE project_id=?", (project["id"],)
    ).fetchone()
    if row is None or row["stage"] != "review" or not row["preview_json"]:
        raise ValueError("no saved blueprint approval preview is ready")
    prepared = _stored_preview(row["preview_json"], row["preview_fingerprint"])
    if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
        raise ValueError("blueprint preview changed; review the saved preview again")
    return approve(
        conn,
        project["id"],
        markdown=prepared["markdown"],
        phases=prepared["phases"],
        plan_basis=prepared["plan_basis"],
        preview_fingerprint=prepared["preview_fingerprint"],
        expected_current_hash=prepared["expected_current_hash"],
        approving_actor=approving_actor,
        pm_session_id=row["pm_session_id"],
    )


def _write_blueprint(path: Path, markdown: str, expected_hash: str | None) -> tuple[bytes | None, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    actual_hash = file_hash(path)
    if actual_hash != expected_hash:
        raise ValueError("blueprint changed after preview; create a fresh preview")
    previous = path.read_bytes() if path.is_file() else None
    if expected_hash is None:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        return previous, True
    fd, temp_name = tempfile.mkstemp(prefix=".blueprint-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        os.replace(temp_name, path)
    finally:
        temp_path = Path(temp_name)
        if temp_path.exists():
            temp_path.unlink()
    return previous, False


def approve(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    markdown: Any,
    phases: Any,
    plan_basis: Any,
    preview_fingerprint: Any,
    expected_current_hash: Any,
    approving_actor: str = "owner",
    pm_session_id: str | None = None,
) -> dict[str, Any]:
    project = store.get_project(conn, project_id)
    prepared = preview(
        conn, project["id"], markdown=markdown, phases=phases, plan_basis=plan_basis
    )
    if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
        raise ValueError("blueprint approval does not match the reviewed preview")
    supplied_expected = str(expected_current_hash) if expected_current_hash else None
    if supplied_expected != prepared["expected_current_hash"]:
        raise ValueError("blueprint approval has a stale expected file hash")

    path = Path(prepared["path"])
    previous_bytes: bytes | None = None
    created_file = False
    try:
        previous_bytes, created_file = _write_blueprint(
            path, prepared["markdown"], prepared["expected_current_hash"]
        )
        conn.execute("BEGIN IMMEDIATE")
        ordinal = int(conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM project_blueprint_revisions WHERE project_id=?",
            (project["id"],),
        ).fetchone()[0])
        revision_id = ids.short_id()
        now = ids.now()
        source_commit = gitutil.head(project["repo_path"]) if gitutil.is_repo(project["repo_path"]) else None
        conn.execute(
            """INSERT INTO project_blueprint_revisions
               (id, project_id, ordinal, path, content_hash, markdown_content,
                status, pm_session_id, approving_actor, approved_at,
                source_git_commit, plan_basis_json, created_at)
               VALUES (?,?,?,?,?,?,'approved',?,?,?,?,?,?)""",
            (
                revision_id, project["id"], ordinal, str(path), prepared["content_hash"],
                prepared["markdown"], pm_session_id, approving_actor, now,
                source_commit, json.dumps(prepared["plan_basis"], sort_keys=True), now,
            ),
        )
        phase_ids: list[str] = []
        phase_rows: list[dict[str, Any]] = []
        phase_ids_by_ordinal: dict[int, str] = {}
        current_phase_id: str | None = None
        for phase in prepared["phases"]:
            phase_id = ids.short_id()
            phase_ids.append(phase_id)
            phase_ids_by_ordinal[phase["ordinal"]] = phase_id
            if phase["status"] == "active":
                current_phase_id = phase_id
            conn.execute(
                """INSERT INTO project_phases
                   (id, project_id, blueprint_revision_id, ordinal, name, status,
                    outcome, non_goals, entry_criteria_json, exit_criteria_json,
                    start_at, target_at, pm_session_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    phase_id, project["id"], revision_id, phase["ordinal"], phase["name"],
                    phase["status"], phase["outcome"], phase["non_goals"],
                    json.dumps(phase["entry_criteria"]), json.dumps(phase["exit_criteria"]),
                    phase["start_at"], phase["target_at"], pm_session_id, now, now,
                ),
            )
            phase_rows.append({
                "id": phase_id,
                "depends_on_ordinals": phase["depends_on_ordinals"],
            })
        dependency_time = ids.now()
        for phase in phase_rows:
            for depends_on_ordinal in phase["depends_on_ordinals"]:
                depends_on_id = phase_ids_by_ordinal.get(depends_on_ordinal)
                if depends_on_id is None:
                    raise ValueError(
                        f"unknown phase dependency ordinal: {depends_on_ordinal}"
                    )
                conn.execute(
                    """INSERT INTO phase_dependencies
                       (phase_id, depends_on_phase_id, created_at)
                       VALUES (?,?,?)""",
                    (phase["id"], depends_on_id, dependency_time),
                )
        conn.execute(
            """UPDATE projects SET blueprint_path=?, blueprint_hash=?,
               blueprint_status='approved', current_blueprint_revision_id=?,
               current_phase_id=?, updated_at=? WHERE id=?""",
            (
                str(path), prepared["content_hash"], revision_id,
                current_phase_id, now, project["id"],
            ),
        )
        draft_row = conn.execute(
            "SELECT planning_task_id, pm_session_id FROM project_blueprint_drafts WHERE project_id=?",
            (project["id"],),
        ).fetchone()
        if draft_row and draft_row["planning_task_id"]:
            store.update_task(
                conn,
                draft_row["planning_task_id"],
                status="done",
                progress=100,
                next_action="Use the approved current phase as the project delivery context.",
                actor_type="human",
                actor_name=approving_actor,
                source="cortex-blueprint",
                commit=False,
            )
            store.create_activity_event(
                conn,
                project_id=project["id"],
                task_id=draft_row["planning_task_id"],
                actor_type="human" if approving_actor == "owner" else "agent",
                actor_name=approving_actor,
                action="pm.session_closed",
                summary="Approved project blueprint and activated its first phase",
                source="cortex-blueprint",
                source_ref=revision_id,
                session_id=draft_row["pm_session_id"],
                commit=False,
            )
        conn.execute("DELETE FROM project_blueprint_drafts WHERE project_id=?", (project["id"],))
        store.create_activity_event(
            conn,
            project_id=project["id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="blueprint.approved",
            summary=f"Approved project blueprint revision {ordinal}",
            source="cortex-blueprint",
            source_ref=revision_id,
            session_id=pm_session_id,
            evidence={
                "revision": ordinal,
                "content_hash": prepared["content_hash"],
                "phase_ids": phase_ids,
                "preview_fingerprint": prepared["preview_fingerprint"],
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        if created_file and path.is_file():
            path.unlink()
        elif previous_bytes is not None:
            path.write_bytes(previous_bytes)
        raise

    return projection(conn, project["id"], refresh_staleness=False)


def refresh_staleness(conn: sqlite3.Connection, project_id: str) -> bool:
    project = store.get_project(conn, project_id)
    if project["blueprint_status"] != "approved" or not project["blueprint_hash"]:
        return False
    actual = file_hash(blueprint_path(project))
    if actual == project["blueprint_hash"]:
        return False
    now = ids.now()
    conn.execute(
        "UPDATE projects SET blueprint_status='stale', updated_at=? WHERE id=?",
        (now, project["id"]),
    )
    store.create_activity_event(
        conn,
        project_id=project["id"],
        actor_type="system",
        actor_name="cortex",
        action="blueprint.stale_detected",
        summary="Approved blueprint changed outside Cortex; reconciliation is required",
        source="cortex-blueprint",
        source_ref=str(blueprint_path(project)),
        evidence={"expected_hash": project["blueprint_hash"], "actual_hash": actual},
        commit=False,
    )
    conn.commit()
    return True


def _current_phase_context(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    refresh: bool = True,
    allowed_statuses: set[str],
) -> tuple[sqlite3.Row, sqlite3.Row, list[str]]:
    if refresh:
        refresh_staleness(conn, project_id)
    project = store.get_project(conn, project_id)
    if project["blueprint_status"] != "approved":
        raise ValueError(
            "current-phase decomposition requires an approved, non-stale blueprint"
        )
    if not project["current_phase_id"] or not project["current_blueprint_revision_id"]:
        raise ValueError("the project has no approved current phase")
    phase = conn.execute(
        """SELECT * FROM project_phases
           WHERE id=? AND project_id=? AND blueprint_revision_id=?""",
        (
            project["current_phase_id"], project["id"],
            project["current_blueprint_revision_id"],
        ),
    ).fetchone()
    if phase is None or phase["status"] not in allowed_statuses:
        if len(allowed_statuses) == 1:
            allowed = next(iter(allowed_statuses))
            raise ValueError(f"the approved current phase is not {allowed}")
        raise ValueError("the approved current phase is not one of the expected statuses")
    criteria = json.loads(phase["exit_criteria_json"] or "[]")
    criteria = [str(item).strip() for item in criteria if str(item).strip()]
    if not criteria:
        raise ValueError("the active phase needs at least one exit criterion")
    return project, phase, criteria


def _phase_decomposition_context(
    conn: sqlite3.Connection, project_id: str, *, refresh: bool = True
) -> tuple[sqlite3.Row, sqlite3.Row, list[str]]:
    """Return the approved active phase and its observable exit criteria."""
    return _current_phase_context(
        conn, project_id, refresh=refresh, allowed_statuses={"active"}
    )


def _phase_review_context(
    conn: sqlite3.Connection, project_id: str, *, refresh: bool = True
) -> tuple[sqlite3.Row, sqlite3.Row, list[str]]:
    return _current_phase_context(
        conn, project_id, refresh=refresh, allowed_statuses={"review"}
    )


def _phase_transition_context(
    conn: sqlite3.Connection, project_id: str, *, refresh: bool = True
) -> tuple[sqlite3.Row, sqlite3.Row, list[str]]:
    try:
        return _phase_review_context(
            conn, project_id, refresh=refresh
        )
    except ValueError as exc:
        if str(exc) != "the approved current phase is not review":
            raise
        return _phase_decomposition_context(
            conn, project_id, refresh=refresh
        )


def _phase_dependency_rows(
    conn: sqlite3.Connection, project_id: str, revision_id: str | None
) -> dict[str, list[dict[str, Any]]]:
    if not revision_id:
        return {}
    rows = conn.execute(
        """SELECT
               dep.phase_id,
               dep.depends_on_phase_id,
               dep.type,
               depends_on_phases.ordinal AS depends_on_ordinal,
               depends_on_phases.name AS depends_on_name,
               depends_on_phases.status AS depends_on_status
           FROM phase_dependencies AS dep
           JOIN project_phases AS phase
             ON phase.id = dep.phase_id
           JOIN project_phases AS depends_on_phases
             ON depends_on_phases.id = dep.depends_on_phase_id
           WHERE phase.project_id = ? AND phase.blueprint_revision_id = ?
           ORDER BY phase.ordinal, depends_on_phases.ordinal""",
        (project_id, revision_id),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["phase_id"], []).append({
            "phase_id": row["phase_id"],
            "depends_on_phase_id": row["depends_on_phase_id"],
            "type": row["type"],
            "depends_on_ordinal": row["depends_on_ordinal"],
            "depends_on_name": row["depends_on_name"],
            "depends_on_status": row["depends_on_status"],
        })
    return grouped


def _next_planned_phase(
    conn: sqlite3.Connection, project_id: str, revision_id: str, current_ordinal: int
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM project_phases
           WHERE project_id=? AND blueprint_revision_id=? AND ordinal > ? AND status='planned'
           ORDER BY ordinal ASC LIMIT 1""",
        (project_id, revision_id, current_ordinal),
    ).fetchone()


def _incomplete_dependency_blockers(
    conn: sqlite3.Connection,
    review_phase_id: str,
    next_phase: sqlite3.Row | None,
) -> list[str]:
    if next_phase is None:
        return []
    blockers = []
    rows = conn.execute(
        """SELECT
               dep.depends_on_phase_id,
               depends_on_phases.ordinal AS depends_on_ordinal,
               depends_on_phases.name AS depends_on_name,
               depends_on_phases.status AS depends_on_status
           FROM phase_dependencies AS dep
           JOIN project_phases AS depends_on_phases
             ON depends_on_phases.id = dep.depends_on_phase_id
           WHERE dep.phase_id = ?""",
        (next_phase["id"],),
    ).fetchall()
    for row in rows:
        if row["depends_on_phase_id"] == review_phase_id:
            continue
        if row["depends_on_status"] != "complete":
            blockers.append(
                f"phase {row['depends_on_ordinal']} ({row['depends_on_name']}) is not complete"
            )
    return blockers


def _exit_criterion_ref(index: int) -> str:
    return f"exit-{index}"


def _covered_exit_criteria(
    conn: sqlite3.Connection, phase_id: str
) -> set[str]:
    rows = conn.execute(
        """SELECT exit_criterion_ref FROM suggestions
           WHERE phase_id=? AND status IN ('proposed', 'converted')
           UNION
           SELECT exit_criterion_ref FROM tasks
           WHERE phase_id=? AND status != 'abandoned'""",
        (phase_id, phase_id),
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _phase_suggestion(
    phase: sqlite3.Row, criterion: str, criterion_ref: str, priority: int
) -> dict[str, Any]:
    title = criterion if len(criterion) <= 120 else criterion[:117].rstrip() + "..."
    non_goals = str(phase["non_goals"] or "").strip()
    brief_parts = [
        f"Deliver the smallest bounded outcome that satisfies this exit criterion: {criterion}",
        f"Phase outcome: {phase['outcome']}",
    ]
    if non_goals:
        brief_parts.append(f"Preserve these non-goals: {non_goals}")
    return {
        "title": title,
        "type": "planning",
        "why": f"Advances {phase['name']} criterion {criterion_ref}.",
        "brief": "\n\n".join(brief_parts),
        "risk": "auto",
        "complexity": None,
        "acceptance": criterion,
        "allowed_paths": None,
        "budget": "medium",
        "effort": "medium",
        "priority": priority,
        "recommended_worker": "codex",
        "recommended_model": None,
        "action": "plan",
        "reviewer": "owner",
        "requires_approval": True,
        "source_worker": "deterministic",
        "source_model": None,
        "exit_criterion_ref": criterion_ref,
    }


def phase_decomposition_preview(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    _refresh_staleness: bool = True,
) -> dict[str, Any]:
    """Preview criterion-linked suggestions without creating work records."""
    project, phase, criteria = _phase_decomposition_context(
        conn, project_id, refresh=_refresh_staleness
    )
    covered = _covered_exit_criteria(conn, phase["id"])
    suggestions = [
        _phase_suggestion(phase, criterion, criterion_ref, min(index, 5))
        for index, criterion in enumerate(criteria, start=1)
        if (criterion_ref := _exit_criterion_ref(index)) not in covered
    ]
    canonical = json.dumps(
        {
            "project_id": project["id"],
            "blueprint_revision_id": project["current_blueprint_revision_id"],
            "phase_id": phase["id"],
            "phase_updated_at": phase["updated_at"],
            "covered_exit_criteria": sorted(covered),
            "suggestions": suggestions,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "project_id": project["id"],
        "blueprint_revision_id": project["current_blueprint_revision_id"],
        "phase": {
            "id": phase["id"],
            "name": phase["name"],
            "outcome": phase["outcome"],
            "status": phase["status"],
            "exit_criteria": criteria,
        },
        "covered_exit_criteria": sorted(covered),
        "suggestions": suggestions,
        "preview_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "writes": {
            "create_suggestions": len(suggestions),
            "create_tasks": False,
            "start_tasks": False,
            "contact_provider": False,
        },
    }


def approve_phase_decomposition(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    preview_fingerprint: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    """Create ordinary suggestions from one exact owner-reviewed preview."""
    suggestion_ids: list[str] = []
    try:
        refresh_staleness(conn, project_id)
        conn.execute("BEGIN IMMEDIATE")
        prepared = phase_decomposition_preview(conn, project_id, _refresh_staleness=False)
        if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
            raise ValueError("phase decomposition approval does not match the reviewed preview")
        if not prepared["suggestions"]:
            raise ValueError("all active-phase exit criteria already have linked work")
        for suggestion in prepared["suggestions"]:
            suggestion_ids.append(store.create_suggestion(
                conn,
                project_id=prepared["project_id"],
                blueprint_revision_id=prepared["blueprint_revision_id"],
                phase_id=prepared["phase"]["id"],
                commit=False,
                **suggestion,
            ))
        store.create_activity_event(
            conn,
            project_id=prepared["project_id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="phase.decomposition_approved",
            summary=(
                f"Approved {len(suggestion_ids)} bounded suggestion"
                f"{'s' if len(suggestion_ids) != 1 else ''} for "
                f"{prepared['phase']['name']}"
            ),
            source="cortex-blueprint",
            source_ref=prepared["phase"]["id"],
            evidence={
                "blueprint_revision_id": prepared["blueprint_revision_id"],
                "phase_id": prepared["phase"]["id"],
                "preview_fingerprint": prepared["preview_fingerprint"],
                "suggestion_ids": suggestion_ids,
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "project_id": prepared["project_id"],
        "blueprint_revision_id": prepared["blueprint_revision_id"],
        "phase_id": prepared["phase"]["id"],
        "preview_fingerprint": prepared["preview_fingerprint"],
        "suggestion_ids": suggestion_ids,
        "created_tasks": False,
        "started_tasks": False,
    }


def _criterion_for_ref(criteria: list[str], criterion_ref: str) -> str:
    match = re.fullmatch(r"exit-(\d+)", str(criterion_ref or ""))
    index = int(match.group(1)) if match else 0
    if index < 1 or index > len(criteria):
        raise ValueError(f"unknown exit criterion: {criterion_ref}")
    return criteria[index - 1]


def _latest_accepted_evidence(
    conn: sqlite3.Connection, phase_id: str
) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """SELECT * FROM phase_criterion_evidence
           WHERE phase_id=? AND status='accepted'
           ORDER BY accepted_at DESC, id DESC""",
        (phase_id,),
    ).fetchall()
    accepted: dict[str, sqlite3.Row] = {}
    for row in rows:
        accepted.setdefault(str(row["exit_criterion_ref"]), row)
    return accepted


def _phase_progress(
    conn: sqlite3.Connection, phase: sqlite3.Row, criteria: list[str]
) -> dict[str, Any]:
    accepted = _latest_accepted_evidence(conn, phase["id"])
    criterion_rows = []
    for index, text in enumerate(criteria, start=1):
        criterion_ref = _exit_criterion_ref(index)
        row = accepted.get(criterion_ref)
        criterion_rows.append({
            "ref": criterion_ref,
            "text": text,
            "accepted": row is not None,
            "evidence_id": row["id"] if row else None,
            "accepted_at": row["accepted_at"] if row else None,
            "approving_actor": row["approving_actor"] if row else None,
            "evidence": json.loads(row["evidence_json"]) if row else [],
        })
    accepted_count = sum(1 for item in criterion_rows if item["accepted"])
    total = len(criterion_rows)
    return {
        "accepted": accepted_count,
        "total": total,
        "percent": (accepted_count * 100 // total) if total else 0,
        "criteria": criterion_rows,
        "basis": "owner-accepted exit criteria",
    }


def _normalized_criterion(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _phase_quality_summary(phases: list[dict[str, Any]]) -> dict[str, Any]:
    total_criteria = 0
    accepted_criteria = 0
    gap_counter = Counter()
    gap_phase_map: dict[str, set[str]] = {}
    gap_text_map: dict[str, str] = {}
    stale_phase_rows = []
    dependency_blockers: list[str] = []
    for phase in phases:
        progress = phase.get("progress") or {}
        total = int(progress.get("total") or 0)
        accepted = int(progress.get("accepted") or 0)
        total_criteria += total
        accepted_criteria += accepted
        if phase.get("is_stale"):
            stale_phase_rows.append({
                "id": phase["id"],
                "name": phase["name"],
                "status": phase["status"],
                "ordinal": phase["ordinal"],
            })
        for criterion in progress.get("criteria", []):
            if criterion.get("accepted"):
                continue
            text = str(criterion.get("text") or "")
            normalized = _normalized_criterion(text)
            if not normalized:
                continue
            gap_counter[normalized] += 1
            gap_phase_map.setdefault(normalized, set()).add(phase["id"])
            gap_text_map.setdefault(normalized, text)
        for dependency in phase.get("depends_on") or []:
            if dependency.get("depends_on_status") != "complete":
                dependency_blockers.append(
                    (
                        f'phase {dependency.get("depends_on_ordinal")} '
                        f'({dependency.get("depends_on_name")})'
                    )
                )
    recurring = []
    for key, count in gap_counter.items():
        if count < 2:
            continue
        recurring.append({
            "criterion": gap_text_map[key],
            "occurrences": count,
            "phase_count": len(gap_phase_map[key]),
            "phase_ids": sorted(gap_phase_map[key]),
        })
    recurring.sort(key=lambda item: (-(item["occurrences"]), item["criterion"]))
    return {
        "coverage": {
            "accepted": accepted_criteria,
            "total": total_criteria,
            "percent": (accepted_criteria * 100 // total_criteria)
            if total_criteria else 0,
        },
        "stale_phase_count": len(stale_phase_rows),
        "stale_phases": stale_phase_rows,
        "recurring_evidence_gaps": recurring[:10],
        "dependency_blockers": sorted(set(dependency_blockers)),
    }


def criterion_evidence_preview(
    conn: sqlite3.Connection,
    project_id: str,
    criterion_ref: str,
    *,
    _refresh_staleness: bool = True,
) -> dict[str, Any]:
    """Assemble recorded task/run pointers for one owner acceptance preview."""
    project, phase, criteria = _phase_transition_context(
        conn, project_id, refresh=_refresh_staleness
    )
    criterion_text = _criterion_for_ref(criteria, criterion_ref)
    accepted = _latest_accepted_evidence(conn, phase["id"]).get(criterion_ref)
    if accepted is not None:
        return {
            "project_id": project["id"],
            "blueprint_revision_id": project["current_blueprint_revision_id"],
            "phase_id": phase["id"],
            "exit_criterion_ref": criterion_ref,
            "criterion_text": criterion_text,
            "accepted": True,
            "acceptance_id": accepted["id"],
            "accepted_at": accepted["accepted_at"],
            "approving_actor": accepted["approving_actor"],
            "evidence": json.loads(accepted["evidence_json"]),
            "blockers": [],
            "can_accept": False,
            "preview_fingerprint": accepted["preview_fingerprint"],
            "writes": {"create_evidence_acceptance": False, "update_phase": False},
        }

    task_rows = conn.execute(
        """SELECT * FROM tasks
           WHERE phase_id=? AND exit_criterion_ref=? AND status!='abandoned'
           ORDER BY created_at, id""",
        (phase["id"], criterion_ref),
    ).fetchall()
    proposed_suggestions = int(conn.execute(
        """SELECT COUNT(*) FROM suggestions
           WHERE phase_id=? AND exit_criterion_ref=? AND status='proposed'""",
        (phase["id"], criterion_ref),
    ).fetchone()[0])
    incomplete = [
        row for row in task_rows if row["status"] not in {"review", "done"}
    ]
    eligible = [row for row in task_rows if row["status"] in {"review", "done"}]
    evidence: list[dict[str, Any]] = []
    for task in eligible:
        evidence.append({
            "kind": "task",
            "id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "acceptance": task["acceptance"],
            "completed_at": task["completed_at"],
        })
        run = conn.execute(
            """SELECT * FROM runs
               WHERE task_id=? AND ended_at IS NOT NULL
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (task["id"],),
        ).fetchone()
        if run is not None:
            evidence.append({
                "kind": "run",
                "id": run["id"],
                "task_id": task["id"],
                "model": run["model"],
                "ended_at": run["ended_at"],
                "tests_passed": run["tests_passed"],
                "outcome": run["outcome"],
                "git_after": run["git_after"],
            })
    blockers: list[str] = []
    if not task_rows:
        blockers.append("No task is linked to this exit criterion.")
    if proposed_suggestions:
        blockers.append(
            f"{proposed_suggestions} linked suggestion"
            f"{'s are' if proposed_suggestions != 1 else ' is'} still awaiting task approval."
        )
    if incomplete:
        blockers.append(
            "Linked tasks must reach review or done: "
            + ", ".join(f"{row['title']} ({row['status']})" for row in incomplete)
        )
    if not eligible and task_rows:
        blockers.append("No linked task has reached review or done.")
    canonical = json.dumps(
        {
            "project_id": project["id"],
            "blueprint_revision_id": project["current_blueprint_revision_id"],
            "phase_id": phase["id"],
            "phase_updated_at": phase["updated_at"],
            "exit_criterion_ref": criterion_ref,
            "criterion_text": criterion_text,
            "evidence": evidence,
            "blockers": blockers,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    can_accept = bool(evidence) and not blockers
    return {
        "project_id": project["id"],
        "blueprint_revision_id": project["current_blueprint_revision_id"],
        "phase_id": phase["id"],
        "exit_criterion_ref": criterion_ref,
        "criterion_text": criterion_text,
        "accepted": False,
        "acceptance_id": None,
        "accepted_at": None,
        "approving_actor": None,
        "evidence": evidence,
        "blockers": blockers,
        "can_accept": can_accept,
        "preview_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "writes": {"create_evidence_acceptance": can_accept, "update_phase": False},
    }


def approve_criterion_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    criterion_ref: str,
    preview_fingerprint: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    """Persist one immutable owner acceptance over exact recorded evidence."""
    try:
        refresh_staleness(conn, project_id)
        conn.execute("BEGIN IMMEDIATE")
        prepared = criterion_evidence_preview(
            conn, project_id, criterion_ref, _refresh_staleness=False
        )
        if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
            raise ValueError("criterion evidence approval does not match the reviewed preview")
        if prepared["accepted"]:
            conn.commit()
            return {
                "acceptance_id": prepared["acceptance_id"],
                "created": False,
                "phase_id": prepared["phase_id"],
                "exit_criterion_ref": criterion_ref,
                "preview_fingerprint": prepared["preview_fingerprint"],
            }
        if not prepared["can_accept"]:
            raise ValueError("criterion evidence is not ready for owner acceptance")
        evidence_id = ids.short_id()
        accepted_at = ids.now()
        conn.execute(
            """INSERT INTO phase_criterion_evidence
               (id, project_id, phase_id, blueprint_revision_id,
                exit_criterion_ref, criterion_text, status, evidence_json,
                preview_fingerprint, approving_actor, accepted_at)
               VALUES (?,?,?,?,?,?,'accepted',?,?,?,?)""",
            (
                evidence_id, prepared["project_id"], prepared["phase_id"],
                prepared["blueprint_revision_id"], criterion_ref,
                prepared["criterion_text"], json.dumps(prepared["evidence"], sort_keys=True),
                prepared["preview_fingerprint"], approving_actor, accepted_at,
            ),
        )
        store.create_activity_event(
            conn,
            project_id=prepared["project_id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="phase.criterion_evidence_accepted",
            summary=f"Accepted evidence for {criterion_ref}: {prepared['criterion_text']}",
            source="cortex-blueprint",
            source_ref=evidence_id,
            evidence={
                "phase_id": prepared["phase_id"],
                "exit_criterion_ref": criterion_ref,
                "preview_fingerprint": prepared["preview_fingerprint"],
                "evidence": prepared["evidence"],
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "acceptance_id": evidence_id,
        "created": True,
        "phase_id": prepared["phase_id"],
        "exit_criterion_ref": criterion_ref,
        "preview_fingerprint": prepared["preview_fingerprint"],
    }


def phase_evidence_overview(
    conn: sqlite3.Connection, project_id: str
) -> dict[str, Any]:
    project, phase, criteria = _phase_transition_context(conn, project_id, refresh=True)

    criterion_previews = [
        criterion_evidence_preview(conn, project["id"], _exit_criterion_ref(index))
        for index in range(1, len(criteria) + 1)
    ]
    progress = _phase_progress(conn, phase, criteria)
    return {
        "project_id": project["id"],
        "blueprint_revision_id": project["current_blueprint_revision_id"],
        "phase": {
            "id": phase["id"], "name": phase["name"], "outcome": phase["outcome"],
            "status": phase["status"],
        },
        "progress": progress,
        "criteria": criterion_previews,
        "transition": (
            phase_completion_preview(conn, project["id"])
            if phase["status"] == "review"
            else phase_review_preview(conn, project["id"])
        ),
    }


def phase_review_preview(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    _refresh_staleness: bool = True,
) -> dict[str, Any]:
    """Preview active-to-review transition from accepted criterion evidence."""
    project, phase, criteria = _phase_decomposition_context(
        conn, project_id, refresh=_refresh_staleness
    )
    progress = _phase_progress(conn, phase, criteria)
    missing = [item["ref"] for item in progress["criteria"] if not item["accepted"]]
    blockers = [
        "Every exit criterion needs owner-accepted evidence: " + ", ".join(missing)
    ] if missing else []
    evidence_refs = [
        {
            "exit_criterion_ref": item["ref"],
            "evidence_id": item["evidence_id"],
            "accepted_at": item["accepted_at"],
        }
        for item in progress["criteria"] if item["accepted"]
    ]
    canonical = json.dumps(
        {
            "project_id": project["id"],
            "blueprint_revision_id": project["current_blueprint_revision_id"],
            "phase_id": phase["id"],
            "phase_updated_at": phase["updated_at"],
            "from_status": phase["status"],
            "to_status": "review",
            "evidence_refs": evidence_refs,
            "blockers": blockers,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "project_id": project["id"],
        "blueprint_revision_id": project["current_blueprint_revision_id"],
        "phase_id": phase["id"],
        "phase_name": phase["name"],
        "from_status": phase["status"],
        "to_status": "review",
        "progress": progress,
        "evidence_refs": evidence_refs,
        "blockers": blockers,
        "can_transition": not blockers,
        "preview_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "writes": {"update_phase_status": not blockers, "complete_phase": False},
    }


def phase_completion_preview(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    _refresh_staleness: bool = True,
) -> dict[str, Any]:
    """Preview review-to-complete transition and optional next-phase activation."""
    project, phase, criteria = _phase_review_context(conn, project_id, refresh=_refresh_staleness)
    progress = _phase_progress(conn, phase, criteria)
    missing = [item["ref"] for item in progress["criteria"] if not item["accepted"]]
    blockers = [
        "Every exit criterion needs owner-accepted evidence: " + ", ".join(missing)
    ] if missing else []
    next_phase = _next_planned_phase(
        conn, project["id"], project["current_blueprint_revision_id"], phase["ordinal"]
    )
    dependency_blockers = _incomplete_dependency_blockers(
        conn, phase["id"], next_phase
    )
    blockers.extend(dependency_blockers)
    evidence_refs = [
        {
            "exit_criterion_ref": item["ref"],
            "evidence_id": item["evidence_id"],
            "accepted_at": item["accepted_at"],
        }
        for item in progress["criteria"] if item["accepted"]
    ]
    canonical = json.dumps(
        {
            "project_id": project["id"],
            "blueprint_revision_id": project["current_blueprint_revision_id"],
            "phase_id": phase["id"],
            "phase_updated_at": phase["updated_at"],
            "from_status": phase["status"],
            "to_status": "complete",
            "next_phase_id": next_phase["id"] if next_phase else None,
            "next_phase_ordinal": next_phase["ordinal"] if next_phase else None,
            "evidence_refs": evidence_refs,
            "dependency_blockers": dependency_blockers,
            "blockers": blockers,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "project_id": project["id"],
        "blueprint_revision_id": project["current_blueprint_revision_id"],
        "phase_id": phase["id"],
        "phase_name": phase["name"],
        "from_status": phase["status"],
        "to_status": "complete",
        "next_phase": (
            {
                "id": next_phase["id"],
                "name": next_phase["name"],
                "ordinal": next_phase["ordinal"],
            }
            if next_phase
            else None
        ),
        "progress": progress,
        "evidence_refs": evidence_refs,
        "blockers": blockers,
        "can_transition": not blockers,
        "preview_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "writes": {
            "update_phase_status": not blockers,
            "complete_phase": True,
            "activate_next_phase": bool(next_phase and not dependency_blockers),
        },
    }


def approve_phase_completion(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    preview_fingerprint: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    try:
        refresh_staleness(conn, project_id)
        conn.execute("BEGIN IMMEDIATE")
        prepared = phase_completion_preview(conn, project_id, _refresh_staleness=False)
        if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
            raise ValueError("phase completion approval does not match the reviewed preview")
        if not prepared["can_transition"]:
            raise ValueError(
                "phase cannot move to complete until all blockers are addressed"
            )
        now = ids.now()
        updated = conn.execute(
            """UPDATE project_phases SET status='complete', completed_at=?, updated_at=?
               WHERE id=? AND project_id=? AND status='review'""",
            (now, now, prepared["phase_id"], prepared["project_id"]),
        ).rowcount
        if updated != 1:
            raise ValueError("phase changed after preview; create a fresh completion preview")
        next_phase = prepared["next_phase"]
        next_phase_id = next_phase["id"] if next_phase else None
        if next_phase_id and prepared["writes"]["activate_next_phase"]:
            conn.execute(
                """UPDATE project_phases SET status='active', updated_at=?
                   WHERE id=? AND project_id=?""",
                (now, next_phase_id, prepared["project_id"]),
            )
        conn.execute(
            """UPDATE projects SET current_phase_id=?
               WHERE id=?""",
            (
                next_phase_id
                if next_phase_id and prepared["writes"]["activate_next_phase"]
                else None,
                prepared["project_id"],
            ),
        )
        project_id_for_projection = prepared["project_id"]
        store.create_activity_event(
            conn,
            project_id=prepared["project_id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="phase.completed",
            summary=f"Marked {prepared['phase_name']} as complete",
            source="cortex-blueprint",
            source_ref=prepared["phase_id"],
            evidence={
                "preview_fingerprint": prepared["preview_fingerprint"],
                "progress": prepared["progress"],
                "next_phase_id": next_phase_id,
                "activated_next_phase": prepared["writes"]["activate_next_phase"],
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return projection(conn, project_id_for_projection, refresh_staleness=False)


def approve_phase_review(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    preview_fingerprint: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    try:
        refresh_staleness(conn, project_id)
        conn.execute("BEGIN IMMEDIATE")
        prepared = phase_review_preview(
            conn, project_id, _refresh_staleness=False
        )
        if str(preview_fingerprint or "") != prepared["preview_fingerprint"]:
            raise ValueError("phase review approval does not match the reviewed preview")
        if not prepared["can_transition"]:
            raise ValueError("phase cannot move to review until every criterion is accepted")
        now = ids.now()
        updated = conn.execute(
            """UPDATE project_phases SET status='review', updated_at=?
               WHERE id=? AND project_id=? AND status='active'""",
            (now, prepared["phase_id"], prepared["project_id"]),
        ).rowcount
        if updated != 1:
            raise ValueError("phase changed after preview; create a fresh review preview")
        store.create_activity_event(
            conn,
            project_id=prepared["project_id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="phase.review_requested",
            summary=f"Moved {prepared['phase_name']} from active to review",
            source="cortex-blueprint",
            source_ref=prepared["phase_id"],
            evidence={
                "preview_fingerprint": prepared["preview_fingerprint"],
                "evidence_refs": prepared["evidence_refs"],
                "progress": prepared["progress"],
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return projection(conn, prepared["project_id"], refresh_staleness=False)


def update_phase_dependencies(
    conn: sqlite3.Connection,
    project_id: str,
    phase_id: str,
    *,
    depends_on_ordinals: Any,
    approving_actor: str = "owner",
) -> dict[str, Any]:
    """Replace dependency edges for one phase in the current approved revision."""
    try:
        refresh_staleness(conn, project_id)
        project = store.get_project(conn, project_id)
        if project["blueprint_status"] != "approved" or not project["current_blueprint_revision_id"]:
            raise ValueError(
                "phase dependency editing requires an approved blueprint with an active revision"
            )
        conn.execute("BEGIN IMMEDIATE")
        phase = conn.execute(
            """SELECT * FROM project_phases
               WHERE id=? AND project_id=? AND blueprint_revision_id=?""",
            (phase_id, project["id"], project["current_blueprint_revision_id"]),
        ).fetchone()
        if phase is None:
            raise ValueError("phase not found in the current approved blueprint revision")
        dependency_rows = conn.execute(
            """SELECT id, ordinal FROM project_phases
               WHERE project_id=? AND blueprint_revision_id=? ORDER BY ordinal""",
            (project["id"], project["current_blueprint_revision_id"]),
        ).fetchall()
        phase_ids_by_ordinal = {row["ordinal"]: row["id"] for row in dependency_rows}
        depends = _normalized_depends_on(depends_on_ordinals, phase["ordinal"])
        for depends_on_ordinal in depends:
            if depends_on_ordinal not in phase_ids_by_ordinal:
                raise ValueError(f"unknown phase dependency ordinal: {depends_on_ordinal}")
        now = ids.now()
        conn.execute("DELETE FROM phase_dependencies WHERE phase_id=?", (phase["id"],))
        for depends_on_ordinal in depends:
            conn.execute(
                """INSERT INTO phase_dependencies
                   (phase_id, depends_on_phase_id, type, created_at)
                   VALUES (?,?, 'blocks', ?)""",
                (phase["id"], phase_ids_by_ordinal[depends_on_ordinal], now),
            )
        conn.execute(
            """UPDATE project_phases
               SET updated_at=?
             WHERE id=? AND project_id=? AND blueprint_revision_id=?""",
            (now, phase["id"], project["id"], project["current_blueprint_revision_id"]),
        )
        store.create_activity_event(
            conn,
            project_id=project["id"],
            actor_type="human" if approving_actor == "owner" else "agent",
            actor_name=approving_actor,
            action="phase.dependencies_updated",
            summary=f"Updated dependency edges for {phase['name']}",
            source="cortex-blueprint",
            source_ref=phase["id"],
            evidence={
                "phase_ordinal": phase["ordinal"],
                "depends_on_ordinals": depends,
                "blueprint_revision_id": project["current_blueprint_revision_id"],
            },
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return projection(conn, project["id"], refresh_staleness=False)


def projection(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    refresh_staleness: bool = True,
) -> dict[str, Any]:
    if refresh_staleness:
        refresh_staleness_fn = globals()["refresh_staleness"]
        refresh_staleness_fn(conn, project_id)
    project = store.get_project(conn, project_id)
    revision = None
    phases: list[dict[str, Any]] = []
    revision_id = project["current_blueprint_revision_id"]
    if revision_id:
        row = conn.execute(
            "SELECT * FROM project_blueprint_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if row:
            revision = {key: row[key] for key in row.keys()}
            revision["plan_basis"] = json.loads(revision.pop("plan_basis_json") or "{}")
            phase_rows = conn.execute(
                """SELECT * FROM project_phases
                   WHERE blueprint_revision_id=? ORDER BY ordinal""",
                (revision_id,),
            ).fetchall()
            dependencies = _phase_dependency_rows(conn, project["id"], revision_id)
            for phase_row in phase_rows:
                phase = {key: phase_row[key] for key in phase_row.keys()}
                phase["entry_criteria"] = json.loads(
                    phase.pop("entry_criteria_json") or "[]"
                )
                phase["exit_criteria"] = json.loads(
                    phase.pop("exit_criteria_json") or "[]"
                )
                phase["depends_on"] = dependencies.get(phase["id"], [])
                phase["progress"] = _phase_progress(
                    conn, phase_row, phase["exit_criteria"]
                )
                phase["is_stale"] = (
                    project["blueprint_status"] == "stale" or any(
                        dep["depends_on_status"] != "complete"
                        for dep in phase["depends_on"]
                    )
                )
                phases.append(phase)
    phase_quality = _phase_quality_summary(phases) if phases else None
    draft = conn.execute(
        "SELECT stage, discovery_hash, updated_at FROM project_blueprint_drafts WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    return {
        "status": project["blueprint_status"],
        "path": project["blueprint_path"] or str(blueprint_path(project)),
        "content_hash": project["blueprint_hash"],
        "revision": revision,
        "phases": phases,
        "phase_quality": phase_quality,
        "current_phase_id": project["current_phase_id"],
        "draft": {key: draft[key] for key in draft.keys()} if draft else None,
        "execution_ready": project["blueprint_status"] not in {"draft", "review"},
    }


def assert_execution_ready(project: sqlite3.Row) -> None:
    if project["blueprint_status"] in {"draft", "review"}:
        raise ValueError(
            "guided onboarding is not execution-ready until its blueprint and first phase are approved"
        )
